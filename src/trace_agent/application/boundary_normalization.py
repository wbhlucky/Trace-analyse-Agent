from __future__ import annotations

from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
    LatencyPhaseAnalysis,
    ScenarioType,
    TraceBoundary,
)
from trace_agent.time_range import parse_time_range


class ExplicitProblemIntervalNormalizer:
    """Make explicit CLI window constraints authoritative."""

    def normalize(
        self,
        analysis: AnalysisResult,
        request: AnalyzeRequest,
    ) -> AnalysisResult:
        explicit = parse_time_range(request.time_range)
        interval = analysis.problem_interval
        if interval is None:
            return analysis

        if explicit is None:
            return self._normalize_requested_duration(
                analysis,
                request,
            )

        previous_start_ns = interval.start_boundary.timestamp_ns
        previous_end_ns = interval.end_boundary.timestamp_ns
        interval.start_boundary = self._explicit_boundary(
            interval.start_boundary,
            timestamp_ns=explicit.start_ns,
            kind="explicit-time-range-start",
        )
        interval.end_boundary = self._explicit_boundary(
            interval.end_boundary,
            timestamp_ns=explicit.end_ns,
            kind="explicit-time-range-end",
        )
        interval.duration_ms = explicit.duration_ms
        interval.selection_rule = (
            "显式 --time-range 具有最高问题区间优先级；Agent 可在区间内"
            "选择技术子指标，但不得改写问题窗口。"
        )
        interval.single_operation_assumption = False

        if (
            previous_start_ns != explicit.start_ns
            or previous_end_ns != explicit.end_ns
        ):
            note = (
                "Agent 提议的问题区间已按显式 --time-range 确定性回填："
                f"{explicit.start_ns} → {explicit.end_ns}。"
            )
            if note not in analysis.limitations:
                analysis.limitations.append(note)
        return analysis

    def _normalize_requested_duration(
        self,
        analysis: AnalysisResult,
        request: AnalyzeRequest,
    ) -> AnalysisResult:
        requested_ms = request.problem_duration_ms
        interval = analysis.problem_interval
        if requested_ms is None or interval is None:
            return analysis

        start_ns = interval.start_boundary.timestamp_ns
        expected_end_ns = start_ns + round(requested_ms * 1_000_000)
        previous_end_ns = interval.end_boundary.timestamp_ns
        previous_duration_ms = interval.duration_ms
        interval.end_boundary = TraceBoundary(
            name=f"用户观察窗口结束（起点 + {requested_ms:g}ms）",
            timestamp_ns=expected_end_ns,
            source="user_problem_duration",
            source_id="problem_duration_ms",
            kind="user-duration-end",
            confidence=interval.start_boundary.confidence,
            evidence_ids=list(
                dict.fromkeys(
                    [
                        *interval.end_boundary.evidence_ids,
                        *interval.evidence_ids,
                    ]
                )
            ),
        )
        interval.duration_ms = requested_ms
        interval.selection_rule = (
            f"--problem-duration-ms={requested_ms:g} 定义问题观察窗口长度；"
            "起点由场景证据选择，技术完成点可作为窗口内的独立子边界，"
            "不得替代观察窗口终点。"
        )

        if request.scenario_type is ScenarioType.COMPLETION_LATENCY:
            self._normalize_completion_duration(
                analysis,
                requested_ms=requested_ms,
                start_ns=start_ns,
                expected_end_ns=expected_end_ns,
            )
            interval.selection_rule = (
                f"--problem-duration-ms={requested_ms:g} 定义本次完成时延；"
                "input→completion、问题区间、阶段取证和 Perf 必须使用"
                "同一组起止边界。"
            )

        if (
            previous_end_ns != expected_end_ns
            or previous_duration_ms != requested_ms
        ):
            note = (
                "Agent 返回的问题窗口已按 --problem-duration-ms 确定性回填："
                f"{start_ns} → {expected_end_ns}（{requested_ms:g}ms）；"
                "场景技术完成边界保持独立。"
            )
            if note not in analysis.limitations:
                analysis.limitations.append(note)
        return analysis

    @staticmethod
    def _normalize_completion_duration(
        analysis: AnalysisResult,
        *,
        requested_ms: float,
        start_ns: int,
        expected_end_ns: int,
    ) -> None:
        completion = analysis.completion_latency
        if completion is None:
            return
        input_ns = completion.input_boundary.timestamp_ns
        if input_ns != start_ns:
            note = (
                "完成时延 input 与用户 duration 窗口起点不一致，"
                "未自动改写输入事件；确定性校验将要求修正。"
            )
            if note not in analysis.limitations:
                analysis.limitations.append(note)
            return

        previous_boundary = completion.completion_boundary
        previous_end_ns = (
            previous_boundary.timestamp_ns
            if previous_boundary is not None
            else None
        )
        previous_duration_ms = completion.completion_latency_ms
        boundary_evidence = list(
            dict.fromkeys(
                [
                    *(previous_boundary.evidence_ids if previous_boundary else []),
                    *completion.input_boundary.evidence_ids,
                    *completion.evidence_ids,
                ]
            )
        )
        completion.completion_boundary = TraceBoundary(
            name=f"用户定义完成（输入 + {requested_ms:g}ms）",
            timestamp_ns=expected_end_ns,
            source="user_problem_duration",
            source_id="problem_duration_ms",
            kind="user-duration-completion",
            confidence=completion.input_boundary.confidence,
            evidence_ids=boundary_evidence,
        )
        completion.completion_proven = True
        completion.completion_latency_ms = requested_ms
        response_ns = (
            completion.response_boundary.timestamp_ns
            if completion.response_boundary is not None
            else None
        )
        completion.response_latency_ms = (
            (response_ns - input_ns) / 1_000_000.0
            if response_ns is not None
            else None
        )
        completion.post_response_duration_ms = (
            (expected_end_ns - response_ns) / 1_000_000.0
            if response_ns is not None
            and input_ns <= response_ns <= expected_end_ns
            else None
        )
        completion.completion_semantics = (
            f"用户通过 --problem-duration-ms={requested_ms:g} 定义"
            " input→completion 完成时延；Trace 内更早的动画或帧结束"
            "仅作为技术子边界。"
        )
        completion.phases = ExplicitProblemIntervalNormalizer._phases(
            existing=completion.phases,
            input_ns=input_ns,
            response_ns=response_ns,
            completion_ns=expected_end_ns,
        )

        if (
            previous_end_ns != expected_end_ns
            or previous_duration_ms != requested_ms
        ):
            old = (
                f"{previous_end_ns}（{previous_duration_ms:g}ms）"
                if previous_end_ns is not None
                and previous_duration_ms is not None
                else "未提供"
            )
            note = (
                "Agent 提议的技术完成点 "
                f"{old} 已降级为窗口内候选；完成时延按用户定义回填为 "
                f"{start_ns} → {expected_end_ns}（{requested_ms:g}ms）。"
            )
            if note not in analysis.limitations:
                analysis.limitations.append(note)
            analysis.summary = (
                f"按用户定义，本次完成时延为 {requested_ms:g}ms；"
                "更早出现的动画或帧结束只作为技术候选。"
            )

    @staticmethod
    def _phases(
        *,
        existing: list[LatencyPhaseAnalysis],
        input_ns: int,
        response_ns: int | None,
        completion_ns: int,
    ) -> list[LatencyPhaseAnalysis]:
        intervals: list[tuple[str, int, int, str]] = []
        if response_ns is not None and input_ns < response_ns < completion_ns:
            intervals.append(
                ("response", input_ns, response_ns, "输入到首次有效响应")
            )
            intervals.append(
                (
                    "post-response",
                    response_ns,
                    completion_ns,
                    "首次响应到用户定义完成终点",
                )
            )
        else:
            intervals.append(
                ("completion", input_ns, completion_ns, "用户定义完成窗口")
            )

        normalized: list[LatencyPhaseAnalysis] = []
        for name, phase_start, phase_end, assessment in intervals:
            previous = next(
                (
                    phase
                    for phase in existing
                    if phase.start_ns == phase_start
                    and (
                        phase.end_ns == phase_end
                        or phase.name == name
                        or phase.start_ns == response_ns
                    )
                ),
                None,
            )
            unchanged = (
                previous is not None
                and previous.start_ns == phase_start
                and previous.end_ns == phase_end
            )
            normalized.append(
                LatencyPhaseAnalysis(
                    name=(previous.name if previous is not None else name),
                    start_ns=phase_start,
                    end_ns=phase_end,
                    duration_ms=(phase_end - phase_start) / 1_000_000.0,
                    critical_threads=(
                        list(previous.critical_threads)
                        if unchanged and previous is not None
                        else []
                    ),
                    assessment=(
                        previous.assessment
                        if unchanged and previous is not None
                        else assessment
                    ),
                    evidence_ids=(
                        list(previous.evidence_ids)
                        if previous is not None
                        else []
                    ),
                )
            )
        return normalized

    @staticmethod
    def _explicit_boundary(
        previous: TraceBoundary,
        *,
        timestamp_ns: int,
        kind: str,
    ) -> TraceBoundary:
        return TraceBoundary(
            name=previous.name,
            timestamp_ns=timestamp_ns,
            source="explicit_time_range",
            source_id="time_range",
            kind=kind,
            confidence=1.0,
            evidence_ids=list(previous.evidence_ids),
        )
