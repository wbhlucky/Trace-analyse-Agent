from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from trace_agent.database import SQLiteTraceRepository, TraceQueryError
from trace_agent.time_range import parse_time_range
from trace_agent.evidence import EvidenceIndex
from trace_agent.models import (
    AgentKind,
    AnalysisResult,
    AnalyzeRequest,
    ColdStartAnalysis,
    CompletionLatencyAnalysis,
    EvidenceRecord,
    FindingStatus,
    PerfAnalysis,
    ProblemInterval,
    ScenarioType,
    SchedulingDiagnosis,
    ThreadExecutionAnalysis,
    TraceCapability,
    TraceHandle,
    ValidationIssue,
    ValidationReport,
)


@dataclass(frozen=True, slots=True)
class TraceBounds:
    start_ns: int
    end_ns: int


class AnalysisValidationError(RuntimeError):
    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        codes = ", ".join(issue.code for issue in report.errors)
        super().__init__(f"分析结果确定性校验失败：{codes}")


class _Issues:
    def __init__(self) -> None:
        self.errors: list[ValidationIssue] = []
        self.warnings: list[ValidationIssue] = []

    def error(self, path: str, code: str, message: str) -> None:
        self.errors.append(
            ValidationIssue(path=path, code=code, message=message)
        )

    def warning(self, path: str, code: str, message: str) -> None:
        self.warnings.append(
            ValidationIssue(path=path, code=code, message=message)
        )

    def report(self) -> ValidationReport:
        return ValidationReport(
            valid=not self.errors,
            errors=self.errors,
            warnings=self.warnings,
        )


class ThreadExecutionValidator:
    """Validate reusable CPU, scheduling, and wakeup-chain output."""

    def __init__(
        self,
        *,
        duration_tolerance_ms: float = 1.0,
        share_tolerance: float = 0.02,
        wakeup_tolerance_ms: float = 5.0,
    ) -> None:
        self._duration_tolerance_ms = duration_tolerance_ms
        self._share_tolerance = share_tolerance
        self._wakeup_tolerance_ns = int(
            wakeup_tolerance_ms * 1_000_000
        )

    def validate(
        self,
        analysis: ThreadExecutionAnalysis,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        path: str,
        known_evidence_ids: set[str],
        issues: _Issues,
    ) -> None:
        interval_ms = max(
            0.0,
            (interval_end_ns - interval_start_ns) / 1_000_000.0,
        )
        states = analysis.state_breakdown
        state_total_ms = sum(
            (
                states.running_ms,
                states.runnable_ms,
                states.sleeping_ms,
                states.uninterruptible_io_ms,
                states.uninterruptible_other_ms,
                states.other_ms,
            )
        )
        if state_total_ms > interval_ms + self._duration_tolerance_ms:
            issues.error(
                f"{path}.state_breakdown",
                "state_duration_exceeds_interval",
                "线程状态总时长超过分析区间",
            )

        self._check_longest(
            analysis.longest_running_ms,
            states.running_ms,
            f"{path}.longest_running_ms",
            "longest_running_exceeds_total",
            issues,
        )
        self._check_longest(
            analysis.longest_runnable_ms,
            states.runnable_ms,
            f"{path}.longest_runnable_ms",
            "longest_runnable_exceeds_total",
            issues,
        )
        self._check_longest(
            analysis.longest_sleep_ms,
            states.sleeping_ms,
            f"{path}.longest_sleep_ms",
            "longest_sleep_exceeds_total",
            issues,
        )

        cpu_running_ms = sum(
            item.running_ms for item in analysis.cpu_distribution
        )
        if cpu_running_ms > interval_ms + self._duration_tolerance_ms:
            issues.error(
                f"{path}.cpu_distribution",
                "cpu_running_exceeds_interval",
                "CPU 分布中的 Running 总时长超过分析区间",
            )
        elif (
            analysis.cpu_distribution
            and abs(cpu_running_ms - states.running_ms)
            > self._duration_tolerance_ms
        ):
            issues.warning(
                f"{path}.cpu_distribution",
                "cpu_running_mismatch",
                "sched_slice 与 thread_state 的 Running 时长不一致",
            )

        if analysis.cpu_distribution:
            share_total = sum(
                item.share for item in analysis.cpu_distribution
            )
            if abs(share_total - 1.0) > self._share_tolerance:
                issues.warning(
                    f"{path}.cpu_distribution",
                    "cpu_share_not_normalized",
                    "CPU 分布 share 总和不接近 1",
                )
            distributed_slices = sum(
                item.schedule_slices
                for item in analysis.cpu_distribution
            )
            if distributed_slices > analysis.schedule_slices:
                issues.error(
                    f"{path}.cpu_distribution",
                    "cpu_slice_count_exceeds_total",
                    "按 CPU 汇总的调度 Slice 数超过线程总数",
                )
            elif distributed_slices != analysis.schedule_slices:
                issues.warning(
                    f"{path}.cpu_distribution",
                    "cpu_slice_count_mismatch",
                    "按 CPU 汇总的调度 Slice 数与线程总数不一致",
                )

        max_migrations = max(analysis.schedule_slices - 1, 0)
        if analysis.cpu_migrations > max_migrations:
            issues.error(
                f"{path}.cpu_migrations",
                "migration_count_impossible",
                "CPU 迁移次数超过相邻调度 Slice 可产生的最大值",
            )

        for index, contention in enumerate(
            analysis.contention_intervals
        ):
            item_path = f"{path}.contention_intervals[{index}]"
            self._check_interval(
                start_ns=contention.start_ns,
                end_ns=contention.end_ns,
                duration_ms=contention.duration_ms,
                outer_start_ns=interval_start_ns,
                outer_end_ns=interval_end_ns,
                path=item_path,
                issues=issues,
            )
            self._check_evidence(
                contention.evidence_ids,
                f"{item_path}.evidence_ids",
                known_evidence_ids,
                issues,
            )

        self._validate_wakeup_chain(
            analysis,
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
            path=path,
            known_evidence_ids=known_evidence_ids,
            issues=issues,
        )
        self._check_evidence(
            analysis.evidence_ids,
            f"{path}.evidence_ids",
            known_evidence_ids,
            issues,
        )
        if not analysis.evidence_ids:
            issues.warning(
                f"{path}.evidence_ids",
                "thread_analysis_without_evidence",
                "线程执行分析没有直接 Evidence",
            )
        if (
            analysis.diagnosis is SchedulingDiagnosis.CPU_CONTENTION
            and not analysis.contention_intervals
        ):
            issues.warning(
                f"{path}.diagnosis",
                "contention_without_intervals",
                "CPU contention 诊断没有竞争区间",
            )
        if (
            analysis.diagnosis is SchedulingDiagnosis.BLOCKED_WAIT
            and not analysis.wakeup_chain
        ):
            issues.warning(
                f"{path}.diagnosis",
                "blocked_wait_without_wakeup",
                "blocked-wait 诊断没有唤醒链",
            )

    def _validate_wakeup_chain(
        self,
        analysis: ThreadExecutionAnalysis,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        path: str,
        known_evidence_ids: set[str],
        issues: _Issues,
    ) -> None:
        visited: set[int] = set()
        previous_waker: int | None = None
        expected_depth = 1
        for index, hop in enumerate(analysis.wakeup_chain):
            item_path = f"{path}.wakeup_chain[{index}]"
            if hop.depth != expected_depth:
                issues.error(
                    f"{item_path}.depth",
                    "wakeup_depth_not_contiguous",
                    "Wakeup Chain depth 必须从 1 连续递增",
                )
            expected_depth += 1
            if (
                previous_waker is not None
                and hop.waiting_itid != previous_waker
            ):
                issues.error(
                    f"{item_path}.waiting_itid",
                    "wakeup_chain_not_connected",
                    "当前 hop 的等待线程不是上一 hop 的唤醒者",
                )
            if hop.waiting_itid in visited:
                issues.error(
                    f"{item_path}.waiting_itid",
                    "wakeup_chain_cycle",
                    "Wakeup Chain 出现重复线程",
                )
            visited.add(hop.waiting_itid)
            if hop.waker_itid is not None:
                if hop.waker_itid in visited:
                    issues.error(
                        f"{item_path}.waker_itid",
                        "wakeup_chain_cycle",
                        "Wakeup Chain 出现循环",
                    )
                previous_waker = hop.waker_itid
            else:
                previous_waker = None

            self._check_interval(
                start_ns=hop.sleep_start_ns,
                end_ns=hop.sleep_end_ns,
                duration_ms=(
                    hop.sleep_end_ns - hop.sleep_start_ns
                )
                / 1_000_000.0,
                outer_start_ns=interval_start_ns,
                outer_end_ns=interval_end_ns,
                path=item_path,
                issues=issues,
            )
            if hop.wakeup_ns is not None:
                delta = abs(hop.wakeup_ns - hop.sleep_end_ns)
                if delta > self._wakeup_tolerance_ns:
                    issues.warning(
                        f"{item_path}.wakeup_ns",
                        "wakeup_not_adjacent_to_sleep_end",
                        "唤醒时间与 Sleep 结束时间不相邻",
                    )
            else:
                issues.warning(
                    f"{item_path}.wakeup_ns",
                    "wakeup_event_missing",
                    "Sleep 分析没有匹配到明确唤醒事件",
                )
            self._check_evidence(
                hop.evidence_ids,
                f"{item_path}.evidence_ids",
                known_evidence_ids,
                issues,
            )

    def _check_longest(
        self,
        longest_ms: float,
        total_ms: float,
        path: str,
        code: str,
        issues: _Issues,
    ) -> None:
        if longest_ms > total_ms + self._duration_tolerance_ms:
            issues.error(path, code, "最长连续区间超过对应状态总时长")

    def _check_interval(
        self,
        *,
        start_ns: int,
        end_ns: int,
        duration_ms: float,
        outer_start_ns: int,
        outer_end_ns: int,
        path: str,
        issues: _Issues,
    ) -> None:
        if end_ns <= start_ns:
            issues.error(
                path,
                "invalid_interval",
                "区间结束时间必须晚于开始时间",
            )
            return
        if start_ns < outer_start_ns or end_ns > outer_end_ns:
            issues.error(
                path,
                "interval_outside_parent",
                "区间超出父分析窗口",
            )
        expected_ms = (end_ns - start_ns) / 1_000_000.0
        if (
            abs(duration_ms - expected_ms)
            > self._duration_tolerance_ms
        ):
            issues.error(
                f"{path}.duration_ms",
                "duration_mismatch",
                "区间 duration 与起止时间不一致",
            )

    @staticmethod
    def _check_evidence(
        evidence_ids: Iterable[str],
        path: str,
        known_evidence_ids: set[str],
        issues: _Issues,
    ) -> None:
        values = list(evidence_ids)
        if len(values) != len(set(values)):
            issues.warning(
                path,
                "duplicate_evidence_id",
                "Evidence ID 存在重复",
            )
        for evidence_id in values:
            if evidence_id not in known_evidence_ids:
                issues.error(
                    path,
                    "unknown_evidence_id",
                    f"引用了不存在的 Evidence：{evidence_id}",
                )


class ColdStartValidator:
    """Validate cold-start scope, boundaries, stages, and execution data."""

    def __init__(
        self,
        thread_validator: ThreadExecutionValidator | None = None,
        *,
        duration_tolerance_ms: float = 1.0,
        reliable_boundary_confidence: float = 0.8,
    ) -> None:
        self._thread_validator = (
            thread_validator or ThreadExecutionValidator()
        )
        self._duration_tolerance_ms = duration_tolerance_ms
        self._reliable_boundary_confidence = (
            reliable_boundary_confidence
        )

    def validate(
        self,
        cold_start: ColdStartAnalysis,
        *,
        findings_confirmed: bool,
        known_evidence_ids: set[str],
        trace_bounds: TraceBounds | None,
        has_scheduling: bool,
        timeline_data: dict[str, Any] | None,
        issues: _Issues,
    ) -> None:
        self._check_evidence_required(
            cold_start.resolved_process.evidence_ids,
            "cold_start.resolved_process.evidence_ids",
            known_evidence_ids,
            issues,
        )
        self._check_evidence_required(
            cold_start.start_boundary.evidence_ids,
            "cold_start.start_boundary.evidence_ids",
            known_evidence_ids,
            issues,
        )
        self._check_evidence_required(
            cold_start.end_boundary.evidence_ids,
            "cold_start.end_boundary.evidence_ids",
            known_evidence_ids,
            issues,
        )
        if cold_start.presentation_boundary is not None:
            self._check_evidence_required(
                cold_start.presentation_boundary.evidence_ids,
                "cold_start.presentation_boundary.evidence_ids",
                known_evidence_ids,
                issues,
            )
        self._check_evidence_required(
            cold_start.evidence_ids,
            "cold_start.evidence_ids",
            known_evidence_ids,
            issues,
        )

        start_ns = cold_start.start_boundary.timestamp_ns
        end_ns = cold_start.end_boundary.timestamp_ns
        application_defined_completion = (
            cold_start.end_boundary.kind
            in {
                "application-defined-completion",
                "application-marker-end",
                "stable-home-frame",
            }
        )
        if end_ns <= start_ns:
            issues.error(
                "cold_start.end_boundary.timestamp_ns",
                "cold_start_boundary_order",
                "冷启动终点必须晚于起点",
            )
            return

        expected_total_ms = (end_ns - start_ns) / 1_000_000.0
        if (
            abs(cold_start.total_duration_ms - expected_total_ms)
            > self._duration_tolerance_ms
        ):
            issues.error(
                "cold_start.total_duration_ms",
                "cold_start_duration_mismatch",
                "冷启动总耗时与边界时间戳不一致",
            )

        if cold_start.presentation_boundary is not None:
            presentation_ns = cold_start.presentation_boundary.timestamp_ns
            if presentation_ns < start_ns:
                issues.error(
                    "cold_start.presentation_boundary.timestamp_ns",
                    "presentation_precedes_cold_start",
                    "Presentation completion cannot precede cold-start start.",
                )
            elif (
                not application_defined_completion
                and presentation_ns < end_ns
            ):
                issues.error(
                    "cold_start.presentation_boundary.timestamp_ns",
                    "presentation_precedes_application_frame",
                    "Presentation completion cannot precede app-frame completion.",
                )
            expected_presentation_ms = (
                presentation_ns - start_ns
            ) / 1_000_000.0
            if cold_start.presentation_duration_ms is None:
                issues.error(
                    "cold_start.presentation_duration_ms",
                    "presentation_duration_required",
                    "A presentation boundary requires its duration.",
                )
            elif (
                abs(
                    cold_start.presentation_duration_ms
                    - expected_presentation_ms
                )
                > self._duration_tolerance_ms
            ):
                issues.error(
                    "cold_start.presentation_duration_ms",
                    "presentation_duration_mismatch",
                    "Presentation duration does not match its boundaries.",
                )

        self._check_boundary_metric_candidate(
            cold_start,
            timeline_data=timeline_data,
            issues=issues,
        )

        if trace_bounds is None:
            issues.warning(
                "cold_start",
                "trace_range_unavailable",
                "无法读取 trace_range，未校验冷启动边界是否在 Trace 内",
            )
        elif (
            start_ns < trace_bounds.start_ns
            or end_ns > trace_bounds.end_ns
        ):
            issues.error(
                "cold_start",
                "cold_start_outside_trace",
                "冷启动边界超出 Trace 范围",
            )

        unreliable_boundary = (
            cold_start.start_boundary.confidence
            < self._reliable_boundary_confidence
            or cold_start.end_boundary.confidence
            < self._reliable_boundary_confidence
        )
        if findings_confirmed and (
            not cold_start.cold_start_proven or unreliable_boundary
        ):
            issues.error(
                "findings",
                "confirmed_with_unproven_cold_start",
                "未证明冷启动或边界不可靠时不能输出 confirmed 结论",
            )

        previous_start_ns: int | None = None
        for stage_index, stage in enumerate(cold_start.stages):
            stage_path = f"cold_start.stages[{stage_index}]"
            if stage.end_ns <= stage.start_ns:
                issues.error(
                    stage_path,
                    "invalid_stage_interval",
                    "阶段结束时间必须晚于开始时间",
                )
                continue
            if stage.start_ns < start_ns or stage.end_ns > end_ns:
                issues.error(
                    stage_path,
                    "stage_outside_cold_start",
                    "阶段超出冷启动窗口",
                )
            expected_stage_ms = (
                stage.end_ns - stage.start_ns
            ) / 1_000_000.0
            if (
                abs(stage.duration_ms - expected_stage_ms)
                > self._duration_tolerance_ms
            ):
                issues.error(
                    f"{stage_path}.duration_ms",
                    "stage_duration_mismatch",
                    "阶段耗时与起止时间不一致",
                )
            if (
                previous_start_ns is not None
                and stage.start_ns < previous_start_ns
            ):
                issues.warning(
                    stage_path,
                    "stages_not_ordered",
                    "阶段没有按开始时间排序",
                )
            previous_start_ns = stage.start_ns
            self._check_evidence_required(
                stage.evidence_ids,
                f"{stage_path}.evidence_ids",
                known_evidence_ids,
                issues,
            )
            if not stage.critical_threads:
                issues.warning(
                    f"{stage_path}.critical_threads",
                    "stage_without_critical_thread",
                    "阶段没有关键线程执行分析",
                )
            if stage.critical_threads and not has_scheduling:
                issues.error(
                    f"{stage_path}.critical_threads",
                    "scheduling_capability_missing",
                    "Trace 不具备调度数据却输出了线程执行分析",
                )
            for thread_index, thread in enumerate(
                stage.critical_threads
            ):
                self._thread_validator.validate(
                    thread,
                    interval_start_ns=stage.start_ns,
                    interval_end_ns=stage.end_ns,
                    path=(
                        f"{stage_path}.critical_threads"
                        f"[{thread_index}]"
                    ),
                    known_evidence_ids=known_evidence_ids,
                    issues=issues,
                )

    def _check_boundary_metric_candidate(
        self,
        cold_start: ColdStartAnalysis,
        *,
        timeline_data: dict[str, Any] | None,
        issues: _Issues,
    ) -> None:
        if not timeline_data:
            return
        boundary_evidence = timeline_data.get("boundary_evidence")
        if not isinstance(boundary_evidence, dict):
            return
        metric = boundary_evidence.get("metric_candidate")
        if not isinstance(metric, dict):
            return

        metric_start = metric.get("start")
        application_defined_start = (
            cold_start.start_boundary.kind
            in {"application-defined-start", "application-marker-start"}
        )
        if isinstance(metric_start, dict):
            expected_ns = metric_start.get("ts")
            if (
                isinstance(expected_ns, int)
                and metric.get("status")
                == "proven_platform_boundary_pair"
                and cold_start.cold_start_proven
                and not application_defined_start
                and cold_start.start_boundary.timestamp_ns != expected_ns
            ):
                issues.error(
                    "cold_start.start_boundary.timestamp_ns",
                    "unproven_launch_boundary",
                    "The selected launch start does not match the proven "
                    "platform launch marker in the metric candidate.",
                )

        application = metric.get("application_complete")
        application_defined_completion = (
            cold_start.end_boundary.kind
            in {
                "application-defined-completion",
                "application-marker-end",
                "stable-home-frame",
            }
        )
        if isinstance(application, dict):
            expected_ns = application.get("timestamp_ns")
            if (
                isinstance(expected_ns, int)
                and cold_start.cold_start_proven
                and not application_defined_completion
                and cold_start.end_boundary.timestamp_ns != expected_ns
            ):
                issues.error(
                    "cold_start.end_boundary.timestamp_ns",
                    "unproven_first_frame_boundary",
                    "The selected first frame is not the mapped main-thread "
                    "ReceiveVsync completion proven by the trace.",
                )

        presentation = metric.get("presentation_complete")
        if (
            cold_start.presentation_boundary is not None
            and isinstance(presentation, dict)
        ):
            expected_ns = presentation.get("timestamp_ns")
            if (
                isinstance(expected_ns, int)
                and cold_start.presentation_boundary.timestamp_ns
                != expected_ns
            ):
                issues.error(
                    "cold_start.presentation_boundary.timestamp_ns",
                    "unproven_presentation_boundary",
                    "The presentation boundary is not the mapped render "
                    "service frame completion proven by the trace.",
                )

    @staticmethod
    def _check_evidence_required(
        evidence_ids: list[str],
        path: str,
        known_evidence_ids: set[str],
        issues: _Issues,
    ) -> None:
        if not evidence_ids:
            issues.error(
                path,
                "evidence_required",
                "该冷启动字段必须引用 Evidence",
            )
        ThreadExecutionValidator._check_evidence(
            evidence_ids,
            path,
            known_evidence_ids,
            issues,
        )


class CompletionLatencyValidator:
    """Validate completion boundaries without turning heuristics into facts."""

    def __init__(
        self,
        thread_validator: ThreadExecutionValidator | None = None,
        *,
        duration_tolerance_ms: float = 1.0,
    ) -> None:
        self._thread_validator = (
            thread_validator or ThreadExecutionValidator()
        )
        self._duration_tolerance_ms = duration_tolerance_ms

    def validate(
        self,
        completion: CompletionLatencyAnalysis,
        *,
        known_evidence_ids: set[str],
        trace_bounds: TraceBounds | None,
        has_scheduling: bool,
        candidate_data: dict[str, Any] | None,
        request: AnalyzeRequest,
        issues: _Issues,
    ) -> None:
        self._check_evidence_required(
            completion.resolved_process.evidence_ids,
            "completion_latency.resolved_process.evidence_ids",
            known_evidence_ids,
            issues,
        )
        self._check_evidence_required(
            completion.input_boundary.evidence_ids,
            "completion_latency.input_boundary.evidence_ids",
            known_evidence_ids,
            issues,
        )
        self._check_evidence_required(
            completion.evidence_ids,
            "completion_latency.evidence_ids",
            known_evidence_ids,
            issues,
        )

        input_ns = completion.input_boundary.timestamp_ns
        response_ns = (
            completion.response_boundary.timestamp_ns
            if completion.response_boundary is not None
            else None
        )
        completion_ns = (
            completion.completion_boundary.timestamp_ns
            if completion.completion_boundary is not None
            else None
        )
        explicit_range = parse_time_range(request.time_range)
        if explicit_range is not None:
            if input_ns != explicit_range.start_ns:
                issues.error(
                    "completion_latency.input_boundary.timestamp_ns",
                    "explicit_time_range_start_mismatch",
                    "完成时延输入边界必须等于显式 --time-range 起点",
                )
            if (
                completion.completion_proven
                and completion_ns != explicit_range.end_ns
            ):
                issues.error(
                    "completion_latency.completion_boundary.timestamp_ns",
                    "explicit_time_range_end_mismatch",
                    "完成时延完成边界必须等于显式 --time-range 终点",
                )
        if completion.response_boundary is not None:
            self._check_evidence_required(
                completion.response_boundary.evidence_ids,
                "completion_latency.response_boundary.evidence_ids",
                known_evidence_ids,
                issues,
            )
        if completion.completion_boundary is not None:
            self._check_evidence_required(
                completion.completion_boundary.evidence_ids,
                "completion_latency.completion_boundary.evidence_ids",
                known_evidence_ids,
                issues,
            )

        if response_ns is not None and response_ns < input_ns:
            issues.error(
                "completion_latency.response_boundary.timestamp_ns",
                "response_precedes_input",
                "响应边界不能早于输入边界",
            )
        if completion_ns is not None and completion_ns <= input_ns:
            issues.error(
                "completion_latency.completion_boundary.timestamp_ns",
                "completion_boundary_order",
                "完成边界必须晚于输入边界",
            )
        if (
            response_ns is not None
            and completion_ns is not None
            and response_ns > completion_ns
        ):
            issues.error(
                "completion_latency.response_boundary.timestamp_ns",
                "response_after_completion",
                "响应边界不能晚于完成边界",
            )

        if response_ns is None:
            if completion.response_latency_ms is not None:
                issues.error(
                    "completion_latency.response_latency_ms",
                    "response_duration_without_boundary",
                    "缺少响应边界时不能给出响应时延",
                )
            if completion.post_response_duration_ms is not None:
                issues.error(
                    "completion_latency.post_response_duration_ms",
                    "post_response_without_boundary",
                    "缺少响应边界时不能给出响应后耗时",
                )
        else:
            self._check_duration(
                completion.response_latency_ms,
                (response_ns - input_ns) / 1_000_000.0,
                "completion_latency.response_latency_ms",
                "response_duration_required",
                "response_duration_mismatch",
                issues,
            )

        if completion.completion_proven:
            if completion_ns is None:
                issues.error(
                    "completion_latency.completion_boundary",
                    "proven_completion_boundary_required",
                    "已证明完成时延时必须提供完成边界",
                )
            else:
                self._check_duration(
                    completion.completion_latency_ms,
                    (completion_ns - input_ns) / 1_000_000.0,
                    "completion_latency.completion_latency_ms",
                    "completion_duration_required",
                    "completion_duration_mismatch",
                    issues,
                )
                if response_ns is not None:
                    self._check_duration(
                        completion.post_response_duration_ms,
                        (completion_ns - response_ns) / 1_000_000.0,
                        "completion_latency.post_response_duration_ms",
                        "post_response_duration_required",
                        "post_response_duration_mismatch",
                        issues,
                    )
        elif (
            completion_ns is not None
            or completion.completion_latency_ms is not None
            or completion.post_response_duration_ms is not None
        ):
            issues.error(
                "completion_latency.completion_proven",
                "unproven_completion_must_be_unavailable",
                "未证明业务完成时，完成边界和完成耗时必须留空",
            )

        timestamps = [input_ns]
        timestamps.extend(
            value for value in (response_ns, completion_ns)
            if value is not None
        )
        if trace_bounds is not None and any(
            value < trace_bounds.start_ns or value > trace_bounds.end_ns
            for value in timestamps
        ):
            issues.error(
                "completion_latency",
                "completion_latency_outside_trace",
                "完成时延边界超出 Trace 范围",
            )

        self._check_selected_candidates(
            completion,
            candidate_data=candidate_data,
            request=request,
            issues=issues,
        )
        outer_end_ns = completion_ns or response_ns
        for phase_index, phase in enumerate(completion.phases):
            path = f"completion_latency.phases[{phase_index}]"
            if phase.end_ns <= phase.start_ns:
                issues.error(
                    path,
                    "invalid_completion_phase_interval",
                    "阶段结束时间必须晚于开始时间",
                )
                continue
            if phase.start_ns < input_ns or (
                outer_end_ns is not None and phase.end_ns > outer_end_ns
            ):
                issues.error(
                    path,
                    "completion_phase_outside_interval",
                    "阶段超出已证明的完成/响应窗口",
                )
            expected_ms = (phase.end_ns - phase.start_ns) / 1_000_000.0
            if abs(phase.duration_ms - expected_ms) > self._duration_tolerance_ms:
                issues.error(
                    f"{path}.duration_ms",
                    "completion_phase_duration_mismatch",
                    "阶段耗时与起止时间不一致",
                )
            self._check_evidence_required(
                phase.evidence_ids,
                f"{path}.evidence_ids",
                known_evidence_ids,
                issues,
            )
            if phase.critical_threads and not has_scheduling:
                issues.error(
                    f"{path}.critical_threads",
                    "scheduling_capability_missing",
                    "Trace 不具备调度数据却输出了线程执行分析",
                )
            for thread_index, thread in enumerate(phase.critical_threads):
                self._thread_validator.validate(
                    thread,
                    interval_start_ns=phase.start_ns,
                    interval_end_ns=phase.end_ns,
                    path=f"{path}.critical_threads[{thread_index}]",
                    known_evidence_ids=known_evidence_ids,
                    issues=issues,
                )

    def _check_selected_candidates(
        self,
        completion: CompletionLatencyAnalysis,
        *,
        candidate_data: dict[str, Any] | None,
        request: AnalyzeRequest,
        issues: _Issues,
    ) -> None:
        if not candidate_data:
            return
        if candidate_data.get("target_ipid") != completion.resolved_process.ipid:
            issues.error(
                "completion_latency.resolved_process.ipid",
                "completion_candidate_process_mismatch",
                "最终进程与完成时延候选工具的 target_ipid 不一致",
            )

        selected_completion_ns = (
            completion.completion_boundary.timestamp_ns
            if completion.completion_boundary is not None
            else None
        )
        matching_candidates = [
            item
            for item in candidate_data.get("completion_candidates") or []
            if isinstance(item, dict)
            and item.get("timestamp_ns") == selected_completion_ns
        ]
        if completion.completion_proven and matching_candidates and all(
            item.get("discovery_only") is True
            or item.get("does_not_prove_business_completion") is True
            for item in matching_candidates
        ):
            issues.error(
                "completion_latency.completion_boundary",
                "heuristic_completion_not_proven",
                "帧静止或窗口末帧只能用于发现候选，不能单独证明业务完成",
            )

        span = candidate_data.get("operation_span")
        span_candidate = (
            span.get("candidate") if isinstance(span, dict) else None
        )
        if request.operation_marker and isinstance(span_candidate, dict):
            if (
                completion.input_boundary.timestamp_ns
                != span_candidate.get("start_ns")
                or selected_completion_ns != span_candidate.get("end_ns")
            ):
                issues.error(
                    "completion_latency",
                    "operation_marker_boundary_mismatch",
                    "显式 operation_marker 已唯一解析时，边界必须与其 ts/ts+dur 一致",
                )
        duration_window = candidate_data.get("duration_window")
        duration_is_primary = (
            request.problem_duration_ms is not None
            and not request.time_range
            and not request.operation_marker
            and not request.start_marker
            and not request.end_marker
            and not request.completion_marker
        )
        if (
            completion.completion_proven
            and duration_is_primary
            and isinstance(duration_window, dict)
            and duration_window.get("metric_definition_from_user") is True
            and (
                completion.input_boundary.timestamp_ns
                != duration_window.get("start_ns")
                or selected_completion_ns != duration_window.get("end_ns")
            )
        ):
            issues.error(
                "completion_latency",
                "user_duration_boundary_mismatch",
                "使用用户 duration 定义完成区间时，边界必须等于最后输入点及其 duration 终点",
            )

    def _check_duration(
        self,
        actual: float | None,
        expected: float,
        path: str,
        required_code: str,
        mismatch_code: str,
        issues: _Issues,
    ) -> None:
        if actual is None:
            issues.error(path, required_code, "该边界组合必须提供对应耗时")
        elif abs(actual - expected) > self._duration_tolerance_ms:
            issues.error(path, mismatch_code, "耗时与边界时间戳不一致")

    @staticmethod
    def _check_evidence_required(
        evidence_ids: list[str],
        path: str,
        known_evidence_ids: set[str],
        issues: _Issues,
    ) -> None:
        if not evidence_ids:
            issues.error(path, "evidence_required", "该完成时延字段必须引用 Evidence")
        ThreadExecutionValidator._check_evidence(
            evidence_ids,
            path,
            known_evidence_ids,
            issues,
        )


class PerfAnalysisValidator:
    """Validate bounded Perf output and its evidence references."""

    def __init__(
        self,
        *,
        symbolization_tolerance: float = 0.01,
    ) -> None:
        self._symbolization_tolerance = symbolization_tolerance

    def validate(
        self,
        perf: PerfAnalysis,
        *,
        known_evidence_ids: set[str],
        trace_bounds: TraceBounds | None,
        issues: _Issues,
    ) -> None:
        if perf.interval_end_ns <= perf.interval_start_ns:
            issues.error(
                "perf",
                "invalid_perf_interval",
                "Perf 分析区间结束时间必须晚于开始时间",
            )
        elif trace_bounds is not None and (
            perf.interval_start_ns < trace_bounds.start_ns
            or perf.interval_end_ns > trace_bounds.end_ns
        ):
            issues.error(
                "perf",
                "perf_interval_outside_trace",
                "Perf 分析区间超出 Trace 范围",
            )

        event_sample_count = sum(
            event.sample_count for event in perf.events
        )
        if event_sample_count != perf.sample_count:
            issues.error(
                "perf.sample_count",
                "perf_sample_count_mismatch",
                "Perf 总样本数与各事件样本数之和不一致",
            )

        if (
            perf.symbolized_callchain_frames
            > perf.total_callchain_frames
        ):
            issues.error(
                "perf.symbolized_callchain_frames",
                "symbolized_frames_exceed_total",
                "已符号化 Perf 帧数超过调用栈总帧数",
            )

        expected_rate = (
            perf.symbolized_callchain_frames
            / perf.total_callchain_frames
            if perf.total_callchain_frames
            else None
        )
        if (
            expected_rate is not None
            and perf.symbolization_rate is not None
            and abs(perf.symbolization_rate - expected_rate)
            > self._symbolization_tolerance
        ):
            issues.error(
                "perf.symbolization_rate",
                "symbolization_rate_mismatch",
                "Perf 符号化率与帧数统计不一致",
            )
        if (
            perf.total_callchain_frames == 0
            and perf.symbolization_rate is not None
        ):
            issues.warning(
                "perf.symbolization_rate",
                "symbolization_rate_without_frames",
                "没有调用栈帧却提供了 Perf 符号化率",
            )

        self._check_evidence_required(
            perf.evidence_ids,
            "perf.evidence_ids",
            known_evidence_ids,
            issues,
        )
        for event_index, event in enumerate(perf.events):
            event_path = f"perf.events[{event_index}]"
            self._check_evidence_required(
                event.evidence_ids,
                f"{event_path}.evidence_ids",
                known_evidence_ids,
                issues,
            )
            for hotspot_index, hotspot in enumerate(event.hotspots):
                hotspot_path = (
                    f"{event_path}.hotspots[{hotspot_index}]"
                )
                if hotspot.self_samples > hotspot.inclusive_samples:
                    issues.error(
                        f"{hotspot_path}.self_samples",
                        "perf_self_samples_exceed_inclusive",
                        "Perf self 样本数超过 inclusive 样本数",
                    )
                if (
                    hotspot.self_event_count
                    > hotspot.inclusive_event_count
                ):
                    issues.error(
                        f"{hotspot_path}.self_event_count",
                        "perf_self_weight_exceeds_inclusive",
                        "Perf self 事件权重超过 inclusive 事件权重",
                    )
                self._check_evidence_required(
                    hotspot.evidence_ids,
                    f"{hotspot_path}.evidence_ids",
                    known_evidence_ids,
                    issues,
                )
    @staticmethod
    def _check_evidence_required(
        evidence_ids: list[str],
        path: str,
        known_evidence_ids: set[str],
        issues: _Issues,
    ) -> None:
        if not evidence_ids:
            issues.error(
                path,
                "perf_evidence_required",
                "Perf 分析必须引用 Evidence",
            )
        ThreadExecutionValidator._check_evidence(
            evidence_ids,
            path,
            known_evidence_ids,
            issues,
        )


class AnalysisResultValidator:
    """Run scenario and cross-cutting semantic validation."""

    def __init__(
        self,
        cold_start_validator: ColdStartValidator | None = None,
        completion_latency_validator: CompletionLatencyValidator | None = None,
        perf_validator: PerfAnalysisValidator | None = None,
    ) -> None:
        self._cold_start_validator = (
            cold_start_validator or ColdStartValidator()
        )
        self._perf_validator = (
            perf_validator or PerfAnalysisValidator()
        )
        self._completion_latency_validator = (
            completion_latency_validator or CompletionLatencyValidator()
        )

    def validate(
        self,
        *,
        request: AnalyzeRequest,
        analysis: AnalysisResult,
        evidence: list[EvidenceRecord],
        trace: TraceHandle,
    ) -> ValidationReport:
        issues = _Issues()
        evidence_index = EvidenceIndex(evidence)
        known_evidence_ids = {
            item.evidence_id for item in evidence_index.records
        }
        confirmed = False
        for index, finding in enumerate(analysis.findings):
            path = f"findings[{index}].evidence_ids"
            if (
                finding.status is FindingStatus.CONFIRMED
                and not finding.evidence_ids
            ):
                issues.error(
                    path,
                    "confirmed_finding_without_evidence",
                    "confirmed finding 必须引用 Evidence",
                )
            if finding.status is FindingStatus.CONFIRMED:
                confirmed = True
            ThreadExecutionValidator._check_evidence(
                finding.evidence_ids,
                path,
                known_evidence_ids,
                issues,
            )

        trace_bounds: TraceBounds | None = None
        if (
            request.scenario_type is ScenarioType.COLD_START
            or request.scenario_type is ScenarioType.COMPLETION_LATENCY
            or analysis.problem_interval is not None
            or analysis.completion_latency is not None
            or analysis.perf is not None
        ):
            trace_bounds = self._read_trace_bounds(
                trace,
                issues,
                path="trace",
            )

        if analysis.problem_interval is not None:
            self._validate_problem_interval(
                analysis.problem_interval,
                known_evidence_ids=known_evidence_ids,
                trace_bounds=trace_bounds,
                issues=issues,
            )
            explicit_range = parse_time_range(request.time_range)
            if explicit_range is not None:
                if (
                    analysis.problem_interval.start_boundary.timestamp_ns
                    != explicit_range.start_ns
                ):
                    issues.error(
                        "problem_interval.start_boundary.timestamp_ns",
                        "explicit_problem_interval_start_mismatch",
                        "问题区间起点必须等于显式 --time-range 起点",
                    )
                if (
                    analysis.problem_interval.end_boundary.timestamp_ns
                    != explicit_range.end_ns
                ):
                    issues.error(
                        "problem_interval.end_boundary.timestamp_ns",
                        "explicit_problem_interval_end_mismatch",
                        "问题区间终点必须等于显式 --time-range 终点",
                    )

        if request.scenario_type is ScenarioType.COLD_START:
            if analysis.cold_start is None:
                issues.error(
                    "cold_start",
                    "cold_start_result_required",
                    "cold-start 场景必须返回 cold_start 结构",
                )
            else:
                timeline_record = evidence_index.cold_start_timeline(
                    ipid=analysis.cold_start.resolved_process.ipid,
                    start_ns=analysis.cold_start.start_boundary.timestamp_ns,
                    end_ns=analysis.cold_start.end_boundary.timestamp_ns,
                )
                if timeline_record is None:
                    timeline_record = evidence_index.last(
                        "inspect_cold_start_timeline",
                        lambda record: (
                            isinstance(
                                record.data.get("target_process"), dict
                            )
                            and record.data["target_process"].get("ipid")
                            == analysis.cold_start.resolved_process.ipid
                        ),
                    )
                if timeline_record is None:
                    timeline_records = evidence_index.all(
                        "inspect_cold_start_timeline"
                    )
                    if len(timeline_records) == 1:
                        timeline_record = timeline_records[0]
                self._cold_start_validator.validate(
                    analysis.cold_start,
                    findings_confirmed=confirmed,
                    known_evidence_ids=known_evidence_ids,
                    trace_bounds=trace_bounds,
                    has_scheduling=(
                        TraceCapability.CPU_SCHEDULING
                        in trace.capabilities
                    ),
                    timeline_data=(
                        timeline_record.data
                        if timeline_record is not None
                        else None
                    ),
                    issues=issues,
                )
                if request.agent is AgentKind.QODER:
                    self._validate_thread_execution_evidence(
                        analysis.cold_start,
                        evidence=evidence,
                        issues=issues,
                    )
        elif analysis.cold_start is not None:
            issues.error(
                "cold_start",
                "unexpected_cold_start_result",
                "非 cold-start 场景不应返回 cold_start 结构",
            )

        if request.scenario_type is ScenarioType.COMPLETION_LATENCY:
            if analysis.completion_latency is None:
                if request.agent is AgentKind.QODER:
                    issues.error(
                        "completion_latency",
                        "completion_latency_result_required",
                        "completion-latency 场景必须返回 completion_latency 结构",
                    )
                else:
                    issues.warning(
                        "completion_latency",
                        "completion_latency_not_run_by_local_agent",
                        "本地 smoke Agent 不执行完成时延分析",
                    )
            else:
                target_record = evidence_index.completion_candidate(
                    ipid=(
                        analysis.completion_latency.resolved_process.ipid
                    ),
                )
                self._completion_latency_validator.validate(
                    analysis.completion_latency,
                    known_evidence_ids=known_evidence_ids,
                    trace_bounds=trace_bounds,
                    has_scheduling=(
                        TraceCapability.CPU_SCHEDULING in trace.capabilities
                    ),
                    candidate_data=(
                        target_record.data if target_record is not None else None
                    ),
                    request=request,
                    issues=issues,
                )
                if request.agent is AgentKind.QODER:
                    if target_record is None:
                        issues.error(
                            "completion_latency.evidence_ids",
                            "completion_candidate_evidence_required",
                            "必须针对最终 ipid 调用完成时延候选工具",
                        )
                    elif target_record.evidence_id not in (
                        analysis.completion_latency.evidence_ids
                    ):
                        issues.error(
                            "completion_latency.evidence_ids",
                            "completion_candidate_evidence_not_referenced",
                            "完成时延分析必须引用候选工具生成的 Evidence",
                        )
                    if (
                        TraceCapability.CPU_SCHEDULING
                        in trace.capabilities
                        and analysis.completion_latency.phases
                    ):
                        completion = analysis.completion_latency
                        response_ns = (
                            completion.response_boundary.timestamp_ns
                            if completion.response_boundary is not None
                            else None
                        )
                        completion_ns = (
                            completion.completion_boundary.timestamp_ns
                            if completion.completion_boundary is not None
                            and completion.completion_proven
                            else None
                        )
                        phase_record = evidence_index.completion_phases(
                            ipid=completion.resolved_process.ipid,
                            input_ns=(
                                completion.input_boundary.timestamp_ns
                            ),
                            response_ns=response_ns,
                            completion_ns=completion_ns,
                        )
                        if phase_record is None:
                            issues.error(
                                "completion_latency.phases",
                                "completion_phase_evidence_required",
                                "完成时延阶段必须引用相同应用和精确边界的 "
                                "inspect_completion_latency_phases Evidence",
                            )
                        else:
                            expected_intervals = {
                                (
                                    raw.get("start_ns"),
                                    raw.get("end_ns"),
                                )
                                for raw in phase_record.data.get("phases")
                                or []
                                if isinstance(raw, dict)
                            }
                            for index, phase in enumerate(
                                completion.phases
                            ):
                                path = (
                                    f"completion_latency.phases[{index}]"
                                )
                                if (
                                    phase.start_ns,
                                    phase.end_ns,
                                ) not in expected_intervals:
                                    issues.error(
                                        path,
                                        "completion_phase_interval_not_inspected",
                                        "阶段区间未被完成时延阶段工具精确取证",
                                    )
                                if phase_record.evidence_id not in (
                                    phase.evidence_ids
                                ):
                                    issues.error(
                                        f"{path}.evidence_ids",
                                        "completion_phase_evidence_not_referenced",
                                        "阶段必须引用完成时延阶段工具 Evidence",
                                    )
                    self._validate_completion_thread_execution_evidence(
                        analysis.completion_latency,
                        evidence=evidence,
                        issues=issues,
                    )
        elif analysis.completion_latency is not None:
            issues.error(
                "completion_latency",
                "unexpected_completion_latency_result",
                "非 completion-latency 场景不应返回 completion_latency 结构",
            )

        has_perf = TraceCapability.PERF_SAMPLES in trace.capabilities
        if has_perf and analysis.perf is None:
            if request.agent is AgentKind.QODER:
                issues.error(
                    "perf",
                    "perf_analysis_required",
                    "Trace 包含有效 Perf 样本，Qoder 分析必须返回 perf 结构",
                )
            else:
                issues.warning(
                    "perf",
                    "perf_analysis_not_run_by_local_agent",
                    "Trace 包含有效 Perf 样本，但本地 smoke Agent 不执行 Perf 分析",
                )
        elif not has_perf and analysis.perf is not None:
            issues.error(
                "perf",
                "unexpected_perf_analysis",
                "Trace 不具备 perf-samples 能力却返回了 Perf 分析",
            )
        elif analysis.perf is not None:
            if has_perf and request.agent is AgentKind.QODER:
                self._validate_perf_profile_evidence(
                    analysis.perf,
                    evidence=evidence,
                    issues=issues,
                )
            self._perf_validator.validate(
                analysis.perf,
                known_evidence_ids=known_evidence_ids,
                trace_bounds=trace_bounds,
                issues=issues,
            )

        return issues.report()

    @classmethod
    def _validate_thread_execution_evidence(
        cls,
        cold_start: ColdStartAnalysis,
        *,
        evidence: list[EvidenceRecord],
        issues: _Issues,
    ) -> None:
        profiles = [
            item
            for item in evidence
            if item.tool == "inspect_thread_execution"
        ]
        for stage_index, stage in enumerate(cold_start.stages):
            for thread_index, thread in enumerate(stage.critical_threads):
                path = (
                    f"cold_start.stages[{stage_index}].critical_threads"
                    f"[{thread_index}]"
                )
                matching = [
                    item
                    for item in profiles
                    if item.evidence_id in thread.evidence_ids
                    and item.data.get("interval_start_ns") == stage.start_ns
                    and item.data.get("interval_end_ns") == stage.end_ns
                    and item.data.get("itid") == thread.itid
                ]
                if not matching:
                    issues.error(
                        f"{path}.evidence_ids",
                        "thread_execution_profile_required",
                        "阶段关键线程必须引用相同阶段边界和 itid 的 "
                        "inspect_thread_execution Evidence",
                    )
                    continue
                expected = matching[-1].data.get("thread_execution")
                if not isinstance(expected, dict):
                    issues.error(
                        f"{path}.evidence_ids",
                        "thread_execution_profile_invalid",
                        "inspect_thread_execution Evidence 缺少确定性线程结果",
                    )
                    continue
                actual = thread.model_dump(mode="json")
                for field in (
                    "state_breakdown",
                    "cpu_distribution",
                    "cpu_migrations",
                    "schedule_slices",
                    "longest_running_ms",
                    "longest_runnable_ms",
                    "longest_sleep_ms",
                    "priority",
                    "contention_intervals",
                    "wakeup_chain",
                ):
                    if not cls._deterministic_values_match(
                        actual.get(field),
                        expected.get(field),
                    ):
                        issues.error(
                            f"{path}.{field}",
                            "thread_execution_profile_mismatch",
                            "阶段线程统计与 inspect_thread_execution "
                            "确定性 Evidence 不一致",
                        )

    @classmethod
    def _validate_completion_thread_execution_evidence(
        cls,
        completion: CompletionLatencyAnalysis,
        *,
        evidence: list[EvidenceRecord],
        issues: _Issues,
    ) -> None:
        cls._validate_phase_thread_execution_evidence(
            phases=completion.phases,
            path_prefix="completion_latency.phases",
            evidence=evidence,
            issues=issues,
        )

    @classmethod
    def _validate_phase_thread_execution_evidence(
        cls,
        *,
        phases: Iterable[Any],
        path_prefix: str,
        evidence: list[EvidenceRecord],
        issues: _Issues,
    ) -> None:
        profiles: list[dict[str, Any]] = []
        for item in evidence:
            if item.tool == "inspect_thread_execution":
                profiles.append(
                    {
                        "evidence_id": item.evidence_id,
                        "start_ns": item.data.get("interval_start_ns"),
                        "end_ns": item.data.get("interval_end_ns"),
                        "itid": item.data.get("itid"),
                        "thread_execution": item.data.get(
                            "thread_execution"
                        ),
                    }
                )
                continue
            if item.tool != "inspect_completion_latency_phases":
                continue
            for raw_phase in item.data.get("phases") or []:
                if not isinstance(raw_phase, dict):
                    continue
                for wrapper in raw_phase.get("thread_profiles") or []:
                    raw_thread = (
                        wrapper.get("thread_execution")
                        if isinstance(wrapper, dict)
                        else None
                    )
                    if not isinstance(raw_thread, dict):
                        continue
                    profiles.append(
                        {
                            "evidence_id": item.evidence_id,
                            "start_ns": raw_phase.get("start_ns"),
                            "end_ns": raw_phase.get("end_ns"),
                            "itid": raw_thread.get("itid"),
                            "thread_execution": raw_thread,
                        }
                    )
        for phase_index, phase in enumerate(phases):
            for thread_index, thread in enumerate(phase.critical_threads):
                path = (
                    f"{path_prefix}[{phase_index}].critical_threads"
                    f"[{thread_index}]"
                )
                matching = [
                    item
                    for item in profiles
                    if item["evidence_id"] in thread.evidence_ids
                    and item["start_ns"] == phase.start_ns
                    and item["end_ns"] == phase.end_ns
                    and item["itid"] == thread.itid
                ]
                if not matching:
                    issues.error(
                        f"{path}.evidence_ids",
                        "thread_execution_profile_required",
                        "阶段关键线程必须引用相同阶段边界和 itid 的 "
                        "inspect_thread_execution Evidence",
                    )
                    continue
                expected = matching[-1].get("thread_execution")
                if not isinstance(expected, dict):
                    issues.error(
                        f"{path}.evidence_ids",
                        "thread_execution_profile_invalid",
                        "inspect_thread_execution Evidence 缺少确定性线程结果",
                    )
                    continue
                actual = thread.model_dump(mode="json")
                for field in (
                    "state_breakdown",
                    "cpu_distribution",
                    "cpu_migrations",
                    "schedule_slices",
                    "longest_running_ms",
                    "longest_runnable_ms",
                    "longest_sleep_ms",
                    "priority",
                    "contention_intervals",
                    "wakeup_chain",
                ):
                    if not cls._deterministic_values_match(
                        actual.get(field), expected.get(field)
                    ):
                        issues.error(
                            f"{path}.{field}",
                            "thread_execution_profile_mismatch",
                            "阶段线程统计与 inspect_thread_execution "
                            "确定性 Evidence 不一致",
                        )

    @classmethod
    def _deterministic_values_match(
        cls,
        actual: Any,
        expected: Any,
        *,
        float_tolerance: float = 0.001,
    ) -> bool:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return actual is expected
        if isinstance(actual, (int, float)) and isinstance(
            expected, (int, float)
        ):
            return abs(float(actual) - float(expected)) <= float_tolerance
        if isinstance(actual, dict) and isinstance(expected, dict):
            return actual.keys() == expected.keys() and all(
                cls._deterministic_values_match(
                    actual[key],
                    expected[key],
                    float_tolerance=float_tolerance,
                )
                for key in actual
            )
        if isinstance(actual, list) and isinstance(expected, list):
            return len(actual) == len(expected) and all(
                cls._deterministic_values_match(
                    actual_item,
                    expected_item,
                    float_tolerance=float_tolerance,
                )
                for actual_item, expected_item in zip(actual, expected)
            )
        return actual == expected

    @staticmethod
    def _validate_perf_profile_evidence(
        perf: PerfAnalysis,
        *,
        evidence: list[EvidenceRecord],
        issues: _Issues,
    ) -> None:
        profile_records = [
            item for item in evidence
            if item.tool == "inspect_perf_profile"
        ]
        if not profile_records:
            issues.error(
                "perf.evidence_ids",
                "perf_profile_evidence_required",
                "Trace 含 Perf 样本时必须调用 inspect_perf_profile，"
                "不能提交 capability-only 的空 Perf 占位",
            )
            return

        referenced = [
            item for item in profile_records
            if item.evidence_id in perf.evidence_ids
        ]
        if not referenced:
            issues.error(
                "perf.evidence_ids",
                "perf_profile_evidence_not_referenced",
                "Perf 分析必须引用 inspect_perf_profile 生成的 Evidence",
            )
            return

        record = referenced[-1]
        requested_thread_ids = record.data.get("requested_thread_ids")
        if not requested_thread_ids:
            issues.error(
                "perf.thread_ids",
                "perf_relevant_thread_scope_required",
                "最终 Perf 分析必须限定 Trace 关键路径相关 OS TID；"
                "进程级 Profile 只能用于线程发现",
            )
        elif sorted(perf.thread_ids) != sorted(requested_thread_ids):
            issues.error(
                "perf.thread_ids",
                "perf_thread_scope_mismatch",
                "Perf thread_ids 与 inspect_perf_profile Evidence 不一致",
            )
        expected_sample_count = record.data.get("sample_count")
        if (
            isinstance(expected_sample_count, int)
            and expected_sample_count != perf.sample_count
        ):
            issues.error(
                "perf.sample_count",
                "perf_profile_sample_count_mismatch",
                "Perf 样本数与 inspect_perf_profile Evidence 不一致："
                f"期望 {expected_sample_count}，实际 {perf.sample_count}",
            )

        expected_events = record.data.get("event_profiles")
        if not isinstance(expected_events, list):
            return
        expected_by_id = {
            item.get("event_type_id"): item
            for item in expected_events
            if isinstance(item, dict)
            and isinstance(item.get("event_type_id"), int)
        }
        if expected_sample_count and not perf.events:
            issues.error(
                "perf.events",
                "perf_profile_events_required",
                "inspect_perf_profile 返回了样本，Perf events 不能为空",
            )
            return
        actual_by_id = {
            event.event_type_id: event for event in perf.events
        }
        for event_id, expected in expected_by_id.items():
            actual = actual_by_id.get(event_id)
            if actual is None:
                issues.error(
                    "perf.events",
                    "perf_profile_event_missing",
                    f"缺少 Perf event_type_id={event_id} 的分析结果",
                )
                continue
            if actual.sample_count != expected.get("sample_count"):
                issues.error(
                    "perf.events",
                    "perf_profile_event_sample_count_mismatch",
                    f"event_type_id={event_id} 样本数与 Evidence 不一致",
                )
            if actual.total_event_count != expected.get(
                "total_event_count"
            ):
                issues.error(
                    "perf.events",
                    "perf_profile_event_weight_mismatch",
                    f"event_type_id={event_id} 事件权重与 Evidence 不一致",
                )
            if record.evidence_id not in actual.evidence_ids:
                issues.error(
                    "perf.events",
                    "perf_profile_event_evidence_required",
                    f"event_type_id={event_id} 必须引用 Perf Profile Evidence",
                )

    @staticmethod
    def _validate_problem_interval(
        interval: ProblemInterval,
        *,
        known_evidence_ids: set[str],
        trace_bounds: TraceBounds | None,
        issues: _Issues,
    ) -> None:
        start_ns = interval.start_boundary.timestamp_ns
        end_ns = interval.end_boundary.timestamp_ns
        if end_ns <= start_ns:
            issues.error(
                "problem_interval.end_boundary.timestamp_ns",
                "problem_interval_boundary_order",
                "问题区间终点必须晚于起点",
            )
            return
        expected_ms = (end_ns - start_ns) / 1_000_000.0
        if abs(interval.duration_ms - expected_ms) > 1.0:
            issues.error(
                "problem_interval.duration_ms",
                "problem_interval_duration_mismatch",
                "问题持续时间与起止边界不一致",
            )
        if trace_bounds is not None and (
            start_ns < trace_bounds.start_ns
            or end_ns > trace_bounds.end_ns
        ):
            issues.error(
                "problem_interval",
                "problem_interval_outside_trace",
                "问题区间超出 Trace 范围",
            )
        ThreadExecutionValidator._check_evidence(
            interval.start_boundary.evidence_ids,
            "problem_interval.start_boundary.evidence_ids",
            known_evidence_ids,
            issues,
        )
        ThreadExecutionValidator._check_evidence(
            interval.end_boundary.evidence_ids,
            "problem_interval.end_boundary.evidence_ids",
            known_evidence_ids,
            issues,
        )
        ThreadExecutionValidator._check_evidence(
            interval.evidence_ids,
            "problem_interval.evidence_ids",
            known_evidence_ids,
            issues,
        )

    @staticmethod
    def _read_trace_bounds(
        trace: TraceHandle,
        issues: _Issues,
        *,
        path: str,
    ) -> TraceBounds | None:
        if trace.database_path is None:
            return None
        try:
            result = SQLiteTraceRepository(
                trace.database_path
            ).query(
                "SELECT start_ts, end_ts FROM trace_range LIMIT 2",
                max_rows=2,
            )
        except (FileNotFoundError, TraceQueryError) as exc:
            issues.warning(
                path,
                "trace_range_query_failed",
                f"读取 trace_range 失败：{exc}",
            )
            return None
        if not result.rows:
            return None
        if len(result.rows) > 1:
            issues.warning(
                path,
                "multiple_trace_ranges",
                "trace_range 存在多行，仅使用第一行",
            )
        row = result.rows[0]
        start_ns = row.get("start_ts")
        end_ns = row.get("end_ts")
        if not isinstance(start_ns, int) or not isinstance(end_ns, int):
            return None
        if end_ns <= start_ns:
            issues.error(
                path,
                "invalid_trace_range",
                "trace_range 结束时间必须晚于开始时间",
            )
            return None
        return TraceBounds(start_ns=start_ns, end_ns=end_ns)
