from __future__ import annotations

from dataclasses import dataclass

from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
    ScenarioType,
    TraceCapability,
)
from trace_agent.tools import ToolRegistry


@dataclass(frozen=True, slots=True)
class SubmissionRejection:
    error: str
    instruction: str


class EvidenceSubmissionPolicy:
    """Provider-neutral evidence requirements for a final Agent result."""

    def __init__(
        self,
        *,
        request: AnalyzeRequest,
        registry: ToolRegistry,
    ) -> None:
        self._request = request
        self._registry = registry

    def validate_collected_evidence(self) -> SubmissionRejection | None:
        request = self._request
        registry = self._registry
        tool_counts = registry.budget_snapshot().get("tool_counts", {})

        if (
            request.scenario_type is ScenarioType.COMPLETION_LATENCY
            and tool_counts.get("inspect_completion_latency_candidates", 0)
            == 0
        ):
            return SubmissionRejection(
                error=(
                    "完成时延场景尚未调用 "
                    "inspect_completion_latency_candidates"
                ),
                instruction=(
                    "先发现并选择应用进程，再用其非零 target_ipid "
                    "调用完成时延候选工具，然后重新提交。"
                ),
            )

        if request.scenario_type is ScenarioType.COMPLETION_LATENCY:
            candidate_result = registry.last_result(
                "inspect_completion_latency_candidates"
            )
            candidate_data = (
                candidate_result.get("data")
                if isinstance(candidate_result, dict)
                else None
            )
            if (
                not isinstance(candidate_data, dict)
                or not isinstance(candidate_data.get("target_ipid"), int)
                or candidate_data.get("target_ipid", 0) <= 0
            ):
                return SubmissionRejection(
                    error=(
                        "最终完成时延候选取证没有限定非零应用 target_ipid"
                    ),
                    instruction=(
                        "选择应用进程后，以其非零 target_ipid 再调用一次 "
                        "inspect_completion_latency_candidates。"
                    ),
                )

        if (
            TraceCapability.PERF_SAMPLES in registry.capabilities
            and tool_counts.get("inspect_perf_profile", 0) == 0
        ):
            return SubmissionRejection(
                error=(
                    "Trace 含 perf-samples，但尚未调用 "
                    "inspect_perf_profile；Perf 有独立保留预算"
                ),
                instruction=(
                    "先调用 inspect_perf_profile，再基于其 Evidence "
                    "补全 perf 后重新提交。"
                ),
            )

        if TraceCapability.PERF_SAMPLES not in registry.capabilities:
            return None

        profile_result = registry.last_result("inspect_perf_profile")
        profile_data = (
            profile_result.get("data")
            if isinstance(profile_result, dict)
            else None
        )
        requested_threads = (
            profile_data.get("requested_thread_ids")
            if isinstance(profile_data, dict)
            else None
        )
        if not requested_threads:
            return SubmissionRejection(
                error=(
                    "最终 Perf Profile 未限定关键 OS TID；"
                    "进程级结果只能用于线程发现"
                ),
                instruction=(
                    "从进程级线程分布中选择 Trace 关键路径相关线程，"
                    "再调用 inspect_perf_profile；最终 perf 只引用这次"
                    "线程级 Evidence。"
                ),
            )

        if request.scenario_type is not ScenarioType.COMPLETION_LATENCY:
            return None
        phase_result = registry.last_result(
            "inspect_completion_latency_phases"
        )
        phase_data = (
            phase_result.get("data")
            if isinstance(phase_result, dict)
            else None
        )
        duration_window = self._authoritative_duration_window()
        if duration_window is not None:
            expected_start = duration_window["start_ns"]
            expected_end = duration_window["end_ns"]
            if isinstance(phase_data, dict) and (
                phase_data.get("input_ns") != expected_start
                or phase_data.get("completion_ns") != expected_end
            ):
                return SubmissionRejection(
                    error=(
                        "完成时延阶段取证未使用用户 duration 的精确边界"
                    ),
                    instruction=(
                        "重新调用 inspect_completion_latency_phases："
                        f"input_ns={expected_start}、"
                        f"completion_ns={expected_end}；response_ns 保留"
                        "已证明的首次有效响应。随后按新阶段建议范围重做 Perf。"
                    ),
                )
            if isinstance(profile_data, dict) and (
                profile_data.get("interval_start_ns") != expected_start
                or profile_data.get("interval_end_ns") != expected_end
            ):
                return SubmissionRejection(
                    error="Perf 未覆盖用户定义的完整完成时延窗口",
                    instruction=(
                        "重新调用 inspect_perf_profile："
                        f"interval_start_ns={expected_start}、"
                        f"interval_end_ns={expected_end}，thread_ids 使用"
                        "最终阶段 Evidence 的 recommended_perf_scope。"
                    ),
                )
        recommended_scope = (
            phase_data.get("recommended_perf_scope")
            if isinstance(phase_data, dict)
            else None
        )
        recommended_threads = (
            recommended_scope.get("thread_ids")
            if isinstance(recommended_scope, dict)
            else None
        )
        if recommended_threads and set(requested_threads) != set(
            recommended_threads
        ):
            return SubmissionRejection(
                error=(
                    "最终 Perf TID 范围未使用完成时延阶段工具给出的 "
                    "recommended_perf_scope.thread_ids"
                ),
                instruction=(
                    "仅使用 recommended_perf_scope.thread_ids 重新调用 "
                    "inspect_perf_profile，不要扩大到全进程，也不要只保留"
                    "主线程。"
                ),
            )
        return None

    def validate_analysis(
        self,
        analysis: AnalysisResult,
    ) -> SubmissionRejection | None:
        if self._request.scenario_type is ScenarioType.COMPLETION_LATENCY:
            completion = analysis.completion_latency
            duration_window = self._authoritative_duration_window()
            if completion is not None and duration_window is not None:
                completion_ns = (
                    completion.completion_boundary.timestamp_ns
                    if completion.completion_boundary is not None
                    else None
                )
                if (
                    completion.input_boundary.timestamp_ns
                    != duration_window["start_ns"]
                    or completion_ns != duration_window["end_ns"]
                ):
                    return SubmissionRejection(
                        error=(
                            "最终完成边界与用户 --problem-duration-ms "
                            "定义不一致"
                        ),
                        instruction=(
                            "将 input_boundary 固定为 duration_window.start_ns="
                            f"{duration_window['start_ns']}，将 "
                            "completion_boundary 固定为 duration_window.end_ns="
                            f"{duration_window['end_ns']}；同步修正总耗时、"
                            "post-response 阶段和问题区间后重新提交。"
                        ),
                    )

        if (
            self._request.scenario_type
            is ScenarioType.COMPLETION_LATENCY
            and TraceCapability.CPU_SCHEDULING
            in self._registry.capabilities
            and analysis.completion_latency is not None
            and analysis.completion_latency.phases
            and self._registry.budget_snapshot()
            .get("tool_counts", {})
            .get("inspect_completion_latency_phases", 0)
            == 0
        ):
            return SubmissionRejection(
                error=(
                    "完成时延已输出阶段，但尚未调用 "
                    "inspect_completion_latency_phases"
                ),
                instruction=(
                    "使用已确认的 input/response/completion 精确时间点调用"
                    "阶段取证工具；从其 thread_profiles 选择关键线程，并使用"
                    "recommended_perf_scope 限定 Perf TID 后重新提交。"
                ),
            )
        if (
            self._request.scenario_type is ScenarioType.COMPLETION_LATENCY
            and analysis.completion_latency is not None
            and analysis.completion_latency.phases
        ):
            completion = analysis.completion_latency
            phase_result = self._registry.last_result(
                "inspect_completion_latency_phases"
            )
            phase_data = (
                phase_result.get("data")
                if isinstance(phase_result, dict)
                else None
            )
            expected_response = (
                completion.response_boundary.timestamp_ns
                if completion.response_boundary is not None
                else None
            )
            expected_completion = (
                completion.completion_boundary.timestamp_ns
                if completion.completion_boundary is not None
                and completion.completion_proven
                else None
            )
            target_process = (
                phase_data.get("target_process")
                if isinstance(phase_data, dict)
                else None
            )
            if (
                not isinstance(phase_data, dict)
                or not isinstance(target_process, dict)
                or target_process.get("ipid")
                != completion.resolved_process.ipid
                or phase_data.get("input_ns")
                != completion.input_boundary.timestamp_ns
                or phase_data.get("response_ns") != expected_response
                or phase_data.get("completion_ns") != expected_completion
            ):
                return SubmissionRejection(
                    error="最终完成时延边界缺少精确阶段 Evidence",
                    instruction=(
                        "使用最终 target_ipid、input_ns、response_ns 和 "
                        "completion_ns 重新调用 "
                        "inspect_completion_latency_phases，再按其建议范围"
                        "重做 Perf 后提交。"
                    ),
                )
        return None

    def _authoritative_duration_window(self) -> dict[str, int] | None:
        request = self._request
        if (
            request.scenario_type is not ScenarioType.COMPLETION_LATENCY
            or request.problem_duration_ms is None
            or request.time_range
            or request.operation_marker
            or request.start_marker
            or request.end_marker
            or request.completion_marker
        ):
            return None
        candidate_result = self._registry.last_result(
            "inspect_completion_latency_candidates"
        )
        candidate_data = (
            candidate_result.get("data")
            if isinstance(candidate_result, dict)
            else None
        )
        duration_window = (
            candidate_data.get("duration_window")
            if isinstance(candidate_data, dict)
            else None
        )
        if (
            not isinstance(duration_window, dict)
            or duration_window.get("metric_definition_from_user") is not True
            or not isinstance(duration_window.get("start_ns"), int)
            or not isinstance(duration_window.get("end_ns"), int)
        ):
            return None
        return {
            "start_ns": duration_window["start_ns"],
            "end_ns": duration_window["end_ns"],
        }
