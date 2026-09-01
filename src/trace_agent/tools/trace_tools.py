from __future__ import annotations

from time import perf_counter
from typing import Any, Callable

from trace_agent.database import (
    ColdStartRepository,
    CompletionLatencyPhaseRepository,
    CompletionLatencyRepository,
    FrameJankRepository,
    PerfAnalysisRepository,
    ProblemWindowRepository,
    SQLiteTraceRepository,
    ThreadExecutionRepository,
    summarize_completion_latency_phases,
)
from trace_agent.evidence import EvidenceStore
from trace_agent.models import (
    ToolAuditRecord,
    TraceCapability,
    TraceHandle,
    utc_now,
)
from trace_agent.tools.registry import ToolDefinition, ToolRegistry


class TraceToolset:
    """Only exposes bounded, read-only trace operations to an agent."""

    def __init__(self, trace: TraceHandle, evidence: EvidenceStore) -> None:
        self._trace = trace
        self._evidence = evidence

    def build_registry(self) -> ToolRegistry:
        registry = ToolRegistry(set(self._trace.capabilities))
        registry.register(
            ToolDefinition(
                name="get_trace_overview",
                description=(
                    "读取已注册 Trace 的受限概览。每次分析必须首先调用。"
                ),
                input_schema={},
                required_capabilities=frozenset(
                    {TraceCapability.FILE_METADATA}
                ),
                handler=lambda _: self.get_trace_overview(),
            )
        )
        registry.register(
            ToolDefinition(
                name="compare_traces",
                description=(
                    "对当前 Trace 和已注册基线做受限对比。仅在工具可用时调用。"
                ),
                input_schema={},
                required_capabilities=frozenset(
                    {TraceCapability.BASELINE_METADATA}
                ),
                handler=lambda _: self.compare_traces(),
            )
        )
        registry.register(
            ToolDefinition(
                name="query_trace_sql",
                description=(
                    "对当前 Trace Streamer SQLite DB 执行一条受限的只读查询。"
                    "由你根据固定 Trace Schema 编写 SELECT、WITH...SELECT 或 "
                    "EXPLAIN；parameters 必须是位置参数数组，purpose 说明查询目的，"
                    "max_rows 范围为 1 到 500。禁止写操作、ATTACH、PRAGMA 和扩展。"
                ),
                input_schema={
                    "sql": str,
                    "parameters": list,
                    "purpose": str,
                    "max_rows": int,
                },
                required_capabilities=frozenset(
                    {TraceCapability.TRACE_DATABASE}
                ),
                handler=self.query_trace_sql,
                progress_formatter=self._query_progress_message,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_problem_window_candidates",
                description=(
                    "读取跨场景的问题区间候选。若 start_marker/end_marker "
                    "在目标应用范围内各自唯一，则返回应用自定义 Slice 对；"
                    "否则发现 Trace 中最后一个有效 TouchEvent、PointerEvent "
                    "或点击打点，并可用 problem_duration_ms 推导候选终点。"
                    "该工具默认一次 Trace 只包含一次用户操作，但只返回候选，"
                    "不会覆盖显式 time_range 或场景专用业务边界。无值时传"
                    "空字符串或 0；max_candidates 范围为 1 到 100。"
                ),
                input_schema={
                    "target_process": str,
                    "start_marker": str,
                    "end_marker": str,
                    "problem_duration_ms": float,
                    "max_candidates": int,
                },
                required_capabilities=frozenset(
                    {TraceCapability.TRACE_DATABASE}
                ),
                handler=self.inspect_problem_window_candidates,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_cold_start_candidates",
                description=(
                    "为 cold-start 场景读取有界的候选事实：Trace 范围、"
                    "候选进程和主线程、app_startup 阶段、首帧候选及最早 "
                    "Slice。该工具不按固定 Marker 名称合成阶段，不选择"
                    "目标进程、不证明冷启动，也不直接"
                    "确定边界；target_process 可传空字符串，"
                    "max_candidates 范围为 1 到 50。"
                ),
                input_schema={
                    "target_process": str,
                    "max_candidates": int,
                },
                required_capabilities=frozenset(
                    {TraceCapability.TRACE_DATABASE}
                ),
                handler=self.inspect_cold_start_candidates,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_frame_jank",
                description=(
                    "对已确定的目标应用和问题区间执行刷新率感知的帧取证。"
                    "工具动态识别 actual/expected 应用帧、产帧线程候选、"
                    "活跃渲染区间、有效 FPS、慢帧、deadline miss、丢帧候选、"
                    "坏帧簇，以及通过 frame_maps 严格关联的 RenderService 帧。"
                    "它不会默认主线程是 UI 线程，也不会把整个 RenderService "
                    "进程负载归因给目标应用。refresh_rate_hz 未指定时传 0；"
                    "frame_producer_itid=0 表示选择区间内主导实际产帧线程，"
                    "多管线时可用候选 itid 再调用一次。observable_delay_stage "
                    "只是流水线定位，根因仍须结合精确窗口的调度、Slice、唤醒链"
                    "和 Perf Evidence。"
                ),
                input_schema={
                    "target_ipid": int,
                    "interval_start_ns": int,
                    "interval_end_ns": int,
                    "refresh_rate_hz": float,
                    "frame_producer_itid": int,
                    "max_bad_frames": int,
                    "max_clusters": int,
                },
                required_capabilities=frozenset(
                    {
                        TraceCapability.TRACE_DATABASE,
                        TraceCapability.FRAME_EVENTS,
                    }
                ),
                handler=self.inspect_frame_jank,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_completion_latency_candidates",
                description=(
                    "读取完成时延专用候选事实。支持唯一业务 duration Slice "
                    "（ts→ts+dur）、唯一开始/完成 Marker 对、TouchUp/Pointer "
                    "输入完成点、响应 Marker、应用实际帧及其 RS 映射、动画结束"
                    "和帧空档候选。首次 target_ipid=0 可发现目标进程；选择应用 "
                    "ipid 后必须再次调用以获取精确帧候选。工具不选择最终边界："
                    "interval_start_ns/interval_end_ns 非零时表示已由应用层解析的"
                    "显式 --time-range，必须作为最高优先级发现窗口；"
                    "动画结束必须验证因果，200ms 帧空档和窗口最后一帧只用于"
                    "发现，绝不证明业务完成。所有缺省字符串传空，缺省数字传 0。"
                ),
                input_schema={
                    "target_ipid": int,
                    "target_process": str,
                    "operation_marker": str,
                    "start_marker": str,
                    "end_marker": str,
                    "response_marker": str,
                    "completion_marker": str,
                    "problem_duration_ms": float,
                    "interval_start_ns": int,
                    "interval_end_ns": int,
                    "lookahead_ms": int,
                    "max_candidates": int,
                },
                required_capabilities=frozenset(
                    {TraceCapability.TRACE_DATABASE}
                ),
                handler=self.inspect_completion_latency_candidates,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_completion_latency_phases",
                description=(
                    "在 Agent 已经确认完成时延边界后，对精确且不重叠的响应阶段和响应后阶段"
                    "执行一次确定性取证。工具按绝对 Running 时间筛选应用线程，保留主线程，"
                    "补充由 frame_maps 关联的 RenderService 线程，并返回阶段级线程调度投影、"
                    "Slice 热点、大帧、映射呈现帧和建议的 Perf OS TID 范围。工具不选择边界、"
                    "不证明业务完成、也不输出根因。response_ns 或 completion_ns 缺失时传 0；"
                    "二者至少一个非零。"
                ),
                input_schema={
                    "target_ipid": int,
                    "input_ns": int,
                    "response_ns": int,
                    "completion_ns": int,
                    "max_threads_per_phase": int,
                    "max_slices_per_phase": int,
                    "max_frames_per_phase": int,
                },
                required_capabilities=frozenset(
                    {
                        TraceCapability.TRACE_DATABASE,
                        TraceCapability.CPU_SCHEDULING,
                    }
                ),
                handler=self.inspect_completion_latency_phases,
                reserved_budget=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_cold_start_timeline",
                description=(
                    "在 Agent 选定目标 ipid 后，围绕进程创建或后备锚点读取"
                    "有界冷启动候选时间线：同名进程、线程、浅层 Slice、"
                    "包名相关跨进程 Slice、锚点附近根 Slice、app_startup "
                    "真实阶段、应用帧及 frame_maps 关系。该工具只返回"
                    "结构化候选事实，不证明冷启动、不选择启动/首帧边界，"
                    "也不认定根因。lookback_ms 范围 0 到 60000，"
                    "lookahead_ms 范围 1 到 120000，max_events 范围 "
                    "20 到 500。"
                ),
                input_schema={
                    "target_ipid": int,
                    "lookback_ms": int,
                    "lookahead_ms": int,
                    "max_events": int,
                },
                required_capabilities=frozenset(
                    {TraceCapability.TRACE_DATABASE}
                ),
                handler=self.inspect_cold_start_timeline,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_perf_profile",
                description=(
                    "对已确定的问题区间和相关进程/线程执行一次确定性的 Perf "
                    "分析：读取采集配置、窗口样本、事件权重、线程/CPU 分布、"
                    "符号化率、应用层热点、应用模块、调用上下文，以及仅供 Agent "
                    "内部归因的 Bottom-up 具体操作诊断。Bottom-up 不生成独立榜单；"
                    "只有定位到具体应用函数或明确操作并被 Top-down 关键路径印证时，"
                    "才能并入根因。模块 offsets 等通用聚合不得形成结论。"
                    "hotspots 只返回"
                    "应用 HAP/HSP/ABC/AOT 与应用自带 SO；appspawn、加载器和系统"
                    "运行时单列为 context，不得据其高占比直接形成应用根因。"
                    "存在 perf-samples 时必须调用；"
                    "它使用独立保留预算，即使普通 Trace 工具预算已用完仍可调用。"
                    "process_ids/thread_ids 使用 OS PID/TID；至少一个列表非空。"
                    "thread_ids 为空时结果只用于线程发现；最终必须再次传入"
                    "与关键路径相关的 TID，不得展示目标进程所有线程。"
                ),
                input_schema={
                    "interval_start_ns": int,
                    "interval_end_ns": int,
                    "process_ids": list,
                    "thread_ids": list,
                    "max_hotspots": int,
                },
                required_capabilities=frozenset(
                    {TraceCapability.PERF_SAMPLES}
                ),
                handler=self.inspect_perf_profile,
                reserved_budget=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="inspect_thread_execution",
                description=(
                    "对一个精确问题/阶段区间内的单个关键线程执行确定性"
                    "调度投影，返回裁剪后的状态时长、最长连续状态、CPU "
                    "分布、迁移、调度片和原始优先级。填写任何阶段的 "
                    "critical_threads 前必须调用；不得把整窗统计复用到"
                    "子阶段。该工具有独立保留预算。"
                ),
                input_schema={
                    "interval_start_ns": int,
                    "interval_end_ns": int,
                    "itid": int,
                },
                required_capabilities=frozenset(
                    {
                        TraceCapability.TRACE_DATABASE,
                        TraceCapability.CPU_SCHEDULING,
                    }
                ),
                handler=self.inspect_thread_execution,
                reserved_budget=True,
            )
        )
        return registry

    def get_trace_overview(self) -> dict[str, Any]:
        return self._execute(
            tool="get_trace_overview",
            arguments={},
            operation=self._get_trace_overview,
        )

    def compare_traces(self) -> dict[str, Any]:
        return self._execute(
            tool="compare_traces",
            arguments={},
            operation=self._compare_traces,
        )

    def query_trace_sql(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        sql = arguments.get("sql")
        parameters = arguments.get("parameters", [])
        purpose = arguments.get("purpose")
        max_rows = arguments.get("max_rows", 100)
        if not isinstance(sql, str):
            raise ValueError("query_trace_sql.sql 必须是字符串")
        if not isinstance(parameters, list):
            raise ValueError("query_trace_sql.parameters 必须是数组")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("query_trace_sql.purpose 必须是非空字符串")
        if len(purpose) > 500:
            raise ValueError("query_trace_sql.purpose 不能超过 500 字符")

        audit_arguments = {
            "sql": sql,
            "parameters": parameters,
            "purpose": purpose.strip(),
            "max_rows": max_rows,
        }
        return self._execute(
            tool="query_trace_sql",
            arguments=audit_arguments,
            operation=lambda: self._query_trace_sql(
                sql=sql,
                parameters=parameters,
                purpose=purpose.strip(),
                max_rows=max_rows,
            ),
        )

    @staticmethod
    def _query_progress_message(
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> str:
        del result
        purpose = arguments.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            return "执行补充只读查询"
        return f"补充取证：{purpose.strip()}"

    def inspect_cold_start_candidates(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        target_process = arguments.get("target_process", "")
        max_candidates = arguments.get("max_candidates", 20)
        if not isinstance(target_process, str):
            raise ValueError(
                "inspect_cold_start_candidates.target_process 必须是字符串"
            )
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= 50
        ):
            raise ValueError(
                "inspect_cold_start_candidates.max_candidates "
                "必须是 1 到 50 的整数"
            )
        normalized_target = target_process.strip()
        return self._execute(
            tool="inspect_cold_start_candidates",
            arguments={
                "target_process": normalized_target,
                "max_candidates": max_candidates,
            },
            operation=lambda: self._inspect_cold_start_candidates(
                target_process=normalized_target,
                max_candidates=max_candidates,
            ),
        )

    def inspect_problem_window_candidates(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        target_process = arguments.get("target_process", "")
        start_marker = arguments.get("start_marker", "")
        end_marker = arguments.get("end_marker", "")
        problem_duration_ms = arguments.get("problem_duration_ms", 0)
        max_candidates = arguments.get("max_candidates", 50)

        string_arguments = {
            "target_process": target_process,
            "start_marker": start_marker,
            "end_marker": end_marker,
        }
        for name, value in string_arguments.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"inspect_problem_window_candidates.{name} 必须是字符串"
                )
            if len(value) > 500:
                raise ValueError(
                    f"inspect_problem_window_candidates.{name} "
                    "不能超过 500 字符"
                )
        if (
            isinstance(problem_duration_ms, bool)
            or not isinstance(problem_duration_ms, (int, float))
            or not 0 <= problem_duration_ms <= 3_600_000
        ):
            raise ValueError(
                "inspect_problem_window_candidates.problem_duration_ms "
                "必须是 0 到 3600000 的数字；0 表示未提供"
            )
        if (
            isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int)
            or not 1 <= max_candidates <= 100
        ):
            raise ValueError(
                "inspect_problem_window_candidates.max_candidates "
                "必须是 1 到 100 的整数"
            )

        audit_arguments = {
            "target_process": target_process.strip(),
            "start_marker": start_marker.strip(),
            "end_marker": end_marker.strip(),
            "problem_duration_ms": float(problem_duration_ms),
            "max_candidates": max_candidates,
        }
        return self._execute(
            tool="inspect_problem_window_candidates",
            arguments=audit_arguments,
            operation=lambda: self._inspect_problem_window_candidates(
                target_process=target_process.strip(),
                start_marker=start_marker.strip(),
                end_marker=end_marker.strip(),
                problem_duration_ms=(
                    float(problem_duration_ms)
                    if problem_duration_ms > 0
                    else None
                ),
                max_candidates=max_candidates,
            ),
        )

    def inspect_cold_start_timeline(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        target_ipid = arguments.get("target_ipid")
        lookback_ms = arguments.get("lookback_ms", 3000)
        lookahead_ms = arguments.get("lookahead_ms", 5000)
        max_events = arguments.get("max_events", 300)
        integer_arguments = {
            "target_ipid": target_ipid,
            "lookback_ms": lookback_ms,
            "lookahead_ms": lookahead_ms,
            "max_events": max_events,
        }
        for name, value in integer_arguments.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"inspect_cold_start_timeline.{name} 必须是整数"
                )
        if target_ipid < 0:
            raise ValueError(
                "inspect_cold_start_timeline.target_ipid 必须是非负整数"
            )
        if not 0 <= lookback_ms <= 60_000:
            raise ValueError(
                "inspect_cold_start_timeline.lookback_ms "
                "必须在 0 到 60000 之间"
            )
        if not 1 <= lookahead_ms <= 120_000:
            raise ValueError(
                "inspect_cold_start_timeline.lookahead_ms "
                "必须在 1 到 120000 之间"
            )
        if not 20 <= max_events <= 500:
            raise ValueError(
                "inspect_cold_start_timeline.max_events "
                "必须在 20 到 500 之间"
            )
        audit_arguments = {
            "target_ipid": target_ipid,
            "lookback_ms": lookback_ms,
            "lookahead_ms": lookahead_ms,
            "max_events": max_events,
        }
        return self._execute(
            tool="inspect_cold_start_timeline",
            arguments=audit_arguments,
            operation=lambda: self._inspect_cold_start_timeline(
                target_ipid=target_ipid,
                lookback_ms=lookback_ms,
                lookahead_ms=lookahead_ms,
                max_events=max_events,
            ),
        )

    def inspect_completion_latency_candidates(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        target_ipid = arguments.get("target_ipid", 0)
        problem_duration_ms = arguments.get("problem_duration_ms", 0)
        interval_start_ns = arguments.get("interval_start_ns", 0)
        interval_end_ns = arguments.get("interval_end_ns", 0)
        lookahead_ms = arguments.get("lookahead_ms", 5000)
        max_candidates = arguments.get("max_candidates", 80)
        string_arguments = {
            name: arguments.get(name, "")
            for name in (
                "target_process",
                "operation_marker",
                "start_marker",
                "end_marker",
                "response_marker",
                "completion_marker",
            )
        }
        for name, value in string_arguments.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"inspect_completion_latency_candidates.{name} "
                    "必须是字符串"
                )
            if len(value) > 500:
                raise ValueError(
                    f"inspect_completion_latency_candidates.{name} "
                    "不能超过 500 字符"
                )
        for name, value in {
            "target_ipid": target_ipid,
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "lookahead_ms": lookahead_ms,
            "max_candidates": max_candidates,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"inspect_completion_latency_candidates.{name} 必须是整数"
                )
        if target_ipid < 0:
            raise ValueError(
                "inspect_completion_latency_candidates.target_ipid "
                "必须是非负整数；0 表示尚未选择"
            )
        if (interval_start_ns == 0) != (interval_end_ns == 0):
            raise ValueError(
                "inspect_completion_latency_candidates 显式区间必须同时"
                "提供 interval_start_ns 和 interval_end_ns"
            )
        if interval_start_ns < 0 or interval_end_ns < 0 or (
            interval_start_ns > 0 and interval_end_ns <= interval_start_ns
        ):
            raise ValueError(
                "inspect_completion_latency_candidates 显式区间无效"
            )
        if not 100 <= lookahead_ms <= 120_000:
            raise ValueError(
                "inspect_completion_latency_candidates.lookahead_ms "
                "必须在 100 到 120000 之间"
            )
        if not 1 <= max_candidates <= 200:
            raise ValueError(
                "inspect_completion_latency_candidates.max_candidates "
                "必须在 1 到 200 之间"
            )
        if (
            isinstance(problem_duration_ms, bool)
            or not isinstance(problem_duration_ms, (int, float))
            or not 0 <= problem_duration_ms <= 3_600_000
        ):
            raise ValueError(
                "inspect_completion_latency_candidates.problem_duration_ms "
                "必须是 0 到 3600000 的数字"
            )
        audit_arguments = {
            "target_ipid": target_ipid,
            **{
                name: value.strip()
                for name, value in string_arguments.items()
            },
            "problem_duration_ms": float(problem_duration_ms),
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "lookahead_ms": lookahead_ms,
            "max_candidates": max_candidates,
        }
        return self._execute(
            tool="inspect_completion_latency_candidates",
            arguments=audit_arguments,
            operation=lambda: self._inspect_completion_latency_candidates(
                **audit_arguments
            ),
        )

    def inspect_frame_jank(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        integer_values = {
            "target_ipid": arguments.get("target_ipid"),
            "interval_start_ns": arguments.get("interval_start_ns"),
            "interval_end_ns": arguments.get("interval_end_ns"),
            "frame_producer_itid": arguments.get(
                "frame_producer_itid", 0
            ),
            "max_bad_frames": arguments.get("max_bad_frames", 20),
            "max_clusters": arguments.get("max_clusters", 10),
        }
        for name, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"inspect_frame_jank.{name} 必须是整数")
        if integer_values["target_ipid"] <= 0:
            raise ValueError(
                "inspect_frame_jank.target_ipid 必须是已确认应用的非零 ipid"
            )
        start_ns = integer_values["interval_start_ns"]
        end_ns = integer_values["interval_end_ns"]
        if start_ns < 0 or end_ns <= start_ns:
            raise ValueError("inspect_frame_jank 时间区间无效")
        if end_ns - start_ns > 300_000_000_000:
            raise ValueError("inspect_frame_jank 单次分析区间不得超过 300 秒")
        if integer_values["frame_producer_itid"] < 0:
            raise ValueError(
                "inspect_frame_jank.frame_producer_itid 必须是非负整数"
            )
        if not 1 <= integer_values["max_bad_frames"] <= 50:
            raise ValueError(
                "inspect_frame_jank.max_bad_frames 必须在 1 到 50 之间"
            )
        if not 1 <= integer_values["max_clusters"] <= 30:
            raise ValueError(
                "inspect_frame_jank.max_clusters 必须在 1 到 30 之间"
            )
        refresh = arguments.get("refresh_rate_hz", 0)
        if (
            isinstance(refresh, bool)
            or not isinstance(refresh, (int, float))
            or refresh < 0
            or refresh > 1000
        ):
            raise ValueError(
                "inspect_frame_jank.refresh_rate_hz 必须是 0 到 1000 的数字"
            )
        audit_arguments = {
            **integer_values,
            "refresh_rate_hz": float(refresh),
        }
        return self._execute(
            tool="inspect_frame_jank",
            arguments=audit_arguments,
            operation=lambda: self._inspect_frame_jank(
                target_ipid=integer_values["target_ipid"],
                interval_start_ns=start_ns,
                interval_end_ns=end_ns,
                refresh_rate_hz=(float(refresh) if refresh > 0 else None),
                frame_producer_itid=(
                    integer_values["frame_producer_itid"] or None
                ),
                max_bad_frames=integer_values["max_bad_frames"],
                max_clusters=integer_values["max_clusters"],
            ),
        )

    def inspect_perf_profile(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        interval_start_ns = arguments.get("interval_start_ns")
        interval_end_ns = arguments.get("interval_end_ns")
        process_ids = arguments.get("process_ids", [])
        thread_ids = arguments.get("thread_ids", [])
        max_hotspots = arguments.get("max_hotspots", 20)
        for name, value in {
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "max_hotspots": max_hotspots,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"inspect_perf_profile.{name} 必须是整数")
        if interval_start_ns < 0 or interval_end_ns <= interval_start_ns:
            raise ValueError(
                "inspect_perf_profile 时间区间无效：结束时间必须晚于起始时间"
            )
        if not 1 <= max_hotspots <= 50:
            raise ValueError(
                "inspect_perf_profile.max_hotspots 必须在 1 到 50 之间"
            )

        normalized_ids: dict[str, list[int]] = {}
        for name, values in {
            "process_ids": process_ids,
            "thread_ids": thread_ids,
        }.items():
            if not isinstance(values, list) or len(values) > 32:
                raise ValueError(
                    f"inspect_perf_profile.{name} 必须是最多 32 项的数组"
                )
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in values
            ):
                raise ValueError(
                    f"inspect_perf_profile.{name} 只能包含非负整数"
                )
            normalized_ids[name] = sorted(set(values))
        if not normalized_ids["process_ids"] and not normalized_ids[
            "thread_ids"
        ]:
            raise ValueError(
                "inspect_perf_profile 必须限定 process_ids 或 thread_ids"
            )

        audit_arguments = {
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "process_ids": normalized_ids["process_ids"],
            "thread_ids": normalized_ids["thread_ids"],
            "max_hotspots": max_hotspots,
        }
        return self._execute(
            tool="inspect_perf_profile",
            arguments=audit_arguments,
            operation=lambda: self._inspect_perf_profile(
                **audit_arguments,
            ),
        )

    def inspect_completion_latency_phases(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        values = {
            "target_ipid": arguments.get("target_ipid"),
            "input_ns": arguments.get("input_ns"),
            "response_ns": arguments.get("response_ns", 0),
            "completion_ns": arguments.get("completion_ns", 0),
            "max_threads_per_phase": arguments.get(
                "max_threads_per_phase", 4
            ),
            "max_slices_per_phase": arguments.get(
                "max_slices_per_phase", 12
            ),
            "max_frames_per_phase": arguments.get(
                "max_frames_per_phase", 8
            ),
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"inspect_completion_latency_phases.{name} 必须是整数"
                )
        if values["target_ipid"] <= 0:
            raise ValueError(
                "inspect_completion_latency_phases.target_ipid "
                "必须是已确认应用的非零 ipid"
            )
        if values["input_ns"] < 0:
            raise ValueError(
                "inspect_completion_latency_phases.input_ns 必须非负"
            )
        for name in ("response_ns", "completion_ns"):
            if values[name] < 0:
                raise ValueError(
                    f"inspect_completion_latency_phases.{name} 必须非负；"
                    "缺失时传 0"
                )
        audit_arguments = dict(values)
        return self._execute(
            tool="inspect_completion_latency_phases",
            arguments=audit_arguments,
            operation=lambda: self._inspect_completion_latency_phases(
                **audit_arguments
            ),
        )

    def inspect_thread_execution(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        interval_start_ns = arguments.get("interval_start_ns")
        interval_end_ns = arguments.get("interval_end_ns")
        itid = arguments.get("itid")
        for name, value in {
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "itid": itid,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"inspect_thread_execution.{name} 必须是整数"
                )
        if interval_start_ns < 0 or interval_end_ns <= interval_start_ns:
            raise ValueError(
                "inspect_thread_execution 时间区间无效："
                "结束时间必须晚于起始时间"
            )
        if itid < 0:
            raise ValueError(
                "inspect_thread_execution.itid 必须是非负整数"
            )
        audit_arguments = {
            "interval_start_ns": interval_start_ns,
            "interval_end_ns": interval_end_ns,
            "itid": itid,
        }
        return self._execute(
            tool="inspect_thread_execution",
            arguments=audit_arguments,
            operation=lambda: self._inspect_thread_execution(
                **audit_arguments,
            ),
        )

    def _get_trace_overview(self) -> tuple[str, dict[str, Any]]:
        data = {
            "trace_id": self._trace.trace_id,
            "format": self._trace.format,
            "size_bytes": self._trace.size_bytes,
            "has_baseline": self._trace.baseline_trace_path is not None,
            "database_available": self._trace.database_path is not None,
            "capabilities": [
                capability.value
                for capability in self._trace.capabilities
            ],
            "parser_level": (
                "trace_database"
                if self._trace.database_path is not None
                else "file_metadata"
            ),
        }
        return "已读取 Trace 文件级概览", data

    def _compare_traces(self) -> tuple[str, dict[str, Any]]:
        if self._trace.baseline_trace_path is None:
            raise ValueError("当前任务没有注册基线 Trace")

        assert self._trace.baseline_size_bytes is not None
        delta = self._trace.size_bytes - self._trace.baseline_size_bytes
        ratio = (
            self._trace.size_bytes / self._trace.baseline_size_bytes
            if self._trace.baseline_size_bytes
            else None
        )
        data = {
            "trace_id": self._trace.trace_id,
            "current_size_bytes": self._trace.size_bytes,
            "baseline_size_bytes": self._trace.baseline_size_bytes,
            "size_delta_bytes": delta,
            "size_ratio": ratio,
            "comparison_level": "file_metadata",
        }
        return "已完成当前 Trace 与基线的文件级元数据对比", data

    def _query_trace_sql(
        self,
        *,
        sql: str,
        parameters: list[Any],
        purpose: str,
        max_rows: Any,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        repository = SQLiteTraceRepository(self._trace.database_path)
        result = repository.query(
            sql,
            parameters,
            max_rows=max_rows,
        )
        data = {
            "purpose": purpose,
            "sql": sql,
            "parameters": parameters,
            **result.as_dict(),
        }
        summary = (
            f"已执行只读 Trace SQL：{purpose}；"
            f"返回 {result.returned_rows} 行"
            f"{'，结果已截断' if result.truncated else ''}"
        )
        return summary, data

    def _inspect_cold_start_candidates(
        self,
        *,
        target_process: str,
        max_candidates: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = ColdStartRepository(
            self._trace.database_path
        ).inspect(
            target_process=target_process or None,
            max_candidates=max_candidates,
        )
        process_count = len(data["process_candidates"])
        stage_count = len(data["app_startup"]["stages"])
        frame_count = len(data["first_frame_candidates"])
        summary = (
            "已读取冷启动候选事实："
            f"{process_count} 个进程候选、"
            f"{stage_count} 条 app_startup 阶段、"
            f"{frame_count} 个首帧候选"
        )
        return summary, data

    def _inspect_problem_window_candidates(
        self,
        *,
        target_process: str,
        start_marker: str,
        end_marker: str,
        problem_duration_ms: float | None,
        max_candidates: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = ProblemWindowRepository(
            self._trace.database_path
        ).inspect(
            target_process=target_process or None,
            start_marker=start_marker or None,
            end_marker=end_marker or None,
            problem_duration_ms=problem_duration_ms,
            max_candidates=max_candidates,
        )
        marker_status = data["application_marker_pair"]["status"]
        input_found = (
            data["last_input_point"]["candidate"] is not None
        )
        candidate_kind = (
            data["default_candidate"].get("candidate_kind")
            if data["default_candidate"]
            else "none"
        )
        summary = (
            "已读取通用问题区间候选："
            f"应用 Marker 对状态={marker_status}，"
            f"最后输入打点={'已找到' if input_found else '未找到'}，"
            f"默认候选={candidate_kind}"
        )
        return summary, data

    def _inspect_cold_start_timeline(
        self,
        *,
        target_ipid: int,
        lookback_ms: int,
        lookahead_ms: int,
        max_events: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = ColdStartRepository(
            self._trace.database_path
        ).inspect_timeline(
            target_ipid=target_ipid,
            lookback_ms=lookback_ms,
            lookahead_ms=lookahead_ms,
            max_events=max_events,
        )
        summary = (
            f"已读取 ipid={target_ipid} 的冷启动候选时间线："
            f"{len(data['timeline_events'])} 条事件、"
            f"{len(data['app_startup']['stages'])} 条 app_startup 阶段、"
            f"{len(data['frame_candidates'])} 条帧候选、"
            f"{len(data['frame_links'])} 条帧映射；"
            f"锚点来源为 {data['window']['anchor_source']}"
        )
        return summary, data

    def _inspect_completion_latency_candidates(
        self,
        *,
        target_ipid: int,
        target_process: str,
        operation_marker: str,
        start_marker: str,
        end_marker: str,
        response_marker: str,
        completion_marker: str,
        problem_duration_ms: float,
        interval_start_ns: int,
        interval_end_ns: int,
        lookahead_ms: int,
        max_candidates: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = CompletionLatencyRepository(
            self._trace.database_path
        ).inspect(
            target_ipid=target_ipid or None,
            target_process=target_process or None,
            operation_marker=operation_marker or None,
            start_marker=start_marker or None,
            end_marker=end_marker or None,
            response_marker=response_marker or None,
            completion_marker=completion_marker or None,
            problem_duration_ms=(
                problem_duration_ms if problem_duration_ms > 0 else None
            ),
            interval_start_ns=(interval_start_ns or None),
            interval_end_ns=(interval_end_ns or None),
            lookahead_ms=lookahead_ms,
            max_candidates=max_candidates,
        )
        operation_status = data["operation_span"]["status"]
        summary = (
            "已读取完成时延候选事实："
            f"operation Slice={operation_status}、"
            f"目标进程候选={len(data['target_process_candidates'])}、"
            f"响应候选={len(data['response_candidates'])}、"
            f"完成候选={len(data['completion_candidates'])}、"
            f"应用帧={len(data['app_frames'])}"
        )
        return summary, data

    def _inspect_perf_profile(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        process_ids: list[int],
        thread_ids: list[int],
        max_hotspots: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = PerfAnalysisRepository(
            self._trace.database_path
        ).inspect(
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
            process_ids=process_ids,
            thread_ids=thread_ids,
            max_hotspots=max_hotspots,
        )
        summary = (
            "已完成问题窗口 Perf 确定性分析："
            f"{data['sample_count']} 个样本、"
            f"{data['total_callchain_frames']} 个调用栈帧、"
            f"{len(data['event_profiles'])} 类事件"
        )
        return summary, data

    def _inspect_frame_jank(
        self,
        *,
        target_ipid: int,
        interval_start_ns: int,
        interval_end_ns: int,
        refresh_rate_hz: float | None,
        frame_producer_itid: int | None,
        max_bad_frames: int,
        max_clusters: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = FrameJankRepository(self._trace.database_path).inspect(
            target_ipid=target_ipid,
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
            refresh_rate_hz=refresh_rate_hz,
            frame_producer_itid=frame_producer_itid,
            max_bad_frames=max_bad_frames,
            max_clusters=max_clusters,
        )
        if not data.get("available"):
            return "问题区间没有可用的目标应用帧证据", data
        metrics = data["metrics"]
        cadence = data["cadence"]
        summary = (
            "已完成刷新率感知的目标应用帧取证："
            f"actual={metrics['application_actual_frames']}，"
            f"mapped-presented={metrics['mapped_presented_frames']}，"
            f"jank={metrics['jank_frames']}，"
            f"missed-vsync={metrics['total_missed_vsyncs']}，"
            "refresh="
            f"{cadence['dominant_refresh_rate_hz'] or 'unavailable'}Hz；"
            "UI 线程未按主线程假定，RenderService 仅统计 frame_maps 关联工作"
        )
        return summary, data

    def _inspect_completion_latency_phases(
        self,
        *,
        target_ipid: int,
        input_ns: int,
        response_ns: int,
        completion_ns: int,
        max_threads_per_phase: int,
        max_slices_per_phase: int,
        max_frames_per_phase: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = CompletionLatencyPhaseRepository(
            self._trace.database_path
        ).inspect(
            target_ipid=target_ipid,
            input_ns=input_ns,
            response_ns=response_ns or None,
            completion_ns=completion_ns or None,
            max_threads_per_phase=max_threads_per_phase,
            max_slices_per_phase=max_slices_per_phase,
            max_frames_per_phase=max_frames_per_phase,
        )
        return summarize_completion_latency_phases(data), data

    def _inspect_thread_execution(
        self,
        *,
        interval_start_ns: int,
        interval_end_ns: int,
        itid: int,
    ) -> tuple[str, dict[str, Any]]:
        if self._trace.database_path is None:
            raise ValueError("当前 Trace 没有可查询的 SQLite DB")
        data = ThreadExecutionRepository(
            self._trace.database_path
        ).inspect(
            interval_start_ns=interval_start_ns,
            interval_end_ns=interval_end_ns,
            itid=itid,
        )
        states = data["thread_execution"]["state_breakdown"]
        summary = (
            f"已完成 itid={itid} 的阶段级线程执行分析："
            f"Running={states['running_ms']:.3f}ms、"
            f"Runnable={states['runnable_ms']:.3f}ms、"
            f"Sleep={states['sleeping_ms']:.3f}ms"
        )
        return summary, data

    def _execute(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        operation: Callable[[], tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        started_at = utc_now()
        started = perf_counter()
        try:
            summary, data = operation()
            evidence = self._evidence.add_evidence(
                tool=tool,
                summary=summary,
                data=data,
            )
            self._evidence.add_audit(
                ToolAuditRecord(
                    tool=tool,
                    arguments=arguments,
                    status="success",
                    started_at=started_at,
                    duration_ms=(perf_counter() - started) * 1000,
                    evidence_id=evidence.evidence_id,
                )
            )
            return evidence.model_dump(mode="json")
        except Exception as exc:
            self._evidence.add_audit(
                ToolAuditRecord(
                    tool=tool,
                    arguments=arguments,
                    status="error",
                    started_at=started_at,
                    duration_ms=(perf_counter() - started) * 1000,
                    error=str(exc),
                )
            )
            raise
