from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable

from trace_agent.models import TraceCapability
from trace_agent.progress import (
    ProgressCallback,
    ProgressEvent,
    ProgressStatus,
    emit_progress,
)


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
ToolProgressFormatter = Callable[
    [dict[str, Any], dict[str, Any] | None],
    str,
]


class ToolBudgetExceeded(RuntimeError):
    """Raised when the run must stop gathering evidence and finalize."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, type]
    required_capabilities: frozenset[TraceCapability]
    handler: ToolHandler
    progress_formatter: ToolProgressFormatter | None = None
    reserved_budget: bool = False


class ToolRegistry:
    """Run-scoped registry of bounded tools exposed to an analysis agent."""

    def __init__(
        self,
        capabilities: set[TraceCapability],
        *,
        max_invocations: int = 14,
        reserved_max_invocations: int = 5,
        per_tool_limits: dict[str, int] | None = None,
    ) -> None:
        if max_invocations <= 0:
            raise ValueError("工具总调用预算必须大于 0")
        if reserved_max_invocations <= 0:
            raise ValueError("保留工具调用预算必须大于 0")
        self._capabilities = frozenset(capabilities)
        self._definitions: dict[str, ToolDefinition] = {}
        self._max_invocations = max_invocations
        self._reserved_max_invocations = reserved_max_invocations
        self._per_tool_limits = dict(
            per_tool_limits
            or {
                "get_trace_overview": 1,
                "query_trace_sql": 6,
                "inspect_problem_window_candidates": 1,
                "inspect_cold_start_candidates": 2,
                "inspect_cold_start_timeline": 2,
                "inspect_completion_latency_candidates": 2,
                "inspect_completion_latency_phases": 2,
                "inspect_frame_jank": 2,
                "inspect_perf_profile": 2,
                "inspect_thread_execution": 5,
            }
        )
        self._invocation_count = 0
        self._reserved_invocation_count = 0
        self._tool_counts: dict[str, int] = {}
        self._last_results: dict[str, dict[str, Any]] = {}
        self._result_history: dict[str, list[dict[str, Any]]] = {}
        self._sealed_reason: str | None = None
        self._progress_callback: ProgressCallback | None = None
        self._preloaded_results: list[dict[str, Any]] = []

    @property
    def capabilities(self) -> frozenset[TraceCapability]:
        return self._capabilities

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._definitions:
            raise ValueError(f"工具重复注册：{definition.name}")
        self._definitions[definition.name] = definition

    def available(self) -> list[ToolDefinition]:
        return [
            definition
            for definition in self._definitions.values()
            if definition.required_capabilities <= self._capabilities
        ]

    def names(self) -> list[str]:
        return [definition.name for definition in self.available()]

    def set_progress_callback(
        self,
        callback: ProgressCallback | None,
    ) -> None:
        self._progress_callback = callback

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        definition = self._definitions.get(name)
        if definition is None:
            raise KeyError(f"未注册工具：{name}")
        missing = definition.required_capabilities - self._capabilities
        if missing:
            values = ", ".join(sorted(item.value for item in missing))
            raise RuntimeError(f"工具 {name} 缺少 Trace 能力：{values}")
        if self._sealed_reason is not None:
            raise ToolBudgetExceeded(
                "Trace 工具取证阶段已结束："
                f"{self._sealed_reason}。请立即基于现有 Evidence 输出最终结果。"
            )
        tool_limit = self._per_tool_limits.get(name)
        tool_count = self._tool_counts.get(name, 0)
        if tool_limit is not None and tool_count >= tool_limit:
            raise ToolBudgetExceeded(
                f"{name} 调用预算 {tool_limit} 次已用完；"
                "请停止同类查询并基于现有 Evidence 输出最终结构化结果。"
            )
        if definition.reserved_budget:
            if (
                self._reserved_invocation_count
                >= self._reserved_max_invocations
            ):
                raise ToolBudgetExceeded(
                    "Trace 保留工具调用预算 "
                    f"{self._reserved_max_invocations} 次已用完；"
                    "请基于已有 Evidence 输出最终结构化结果。"
                )
        else:
            if self._invocation_count >= self._max_invocations:
                perf_instruction = (
                    "Perf 有独立保留预算；必须先调用 "
                    "inspect_perf_profile，再提交最终结果。"
                    if (
                        TraceCapability.PERF_SAMPLES in self._capabilities
                        and self._tool_counts.get(
                            "inspect_perf_profile", 0
                        ) == 0
                    )
                    else "请停止取证并立即输出最终结构化结果。"
                )
                raise ToolBudgetExceeded(
                    f"Trace 普通工具总调用预算 {self._max_invocations} "
                    f"次已用完；{perf_instruction}"
                )
        call_arguments = arguments or {}
        activity = self._progress_activity(
            definition,
            arguments=call_arguments,
            result=None,
        )
        started_message = activity or f"正在调用 {name}"
        started = perf_counter()
        emit_progress(
            self._progress_callback,
            ProgressEvent(
                stage="agent.tool",
                status=ProgressStatus.STARTED,
                message=started_message,
                details={
                    "tool": name,
                    "activity": activity,
                    "tool_count": tool_count + 1,
                },
            ),
        )
        try:
            result = definition.handler(call_arguments)
        except Exception as exc:
            emit_progress(
                self._progress_callback,
                ProgressEvent(
                    stage="agent.tool",
                    status=ProgressStatus.FAILED,
                    message=started_message,
                    elapsed_ms=(perf_counter() - started) * 1000,
                    details={
                        "tool": name,
                        "activity": activity,
                        "error": str(exc),
                        "tool_count": tool_count + 1,
                    },
                ),
            )
            raise
        if definition.reserved_budget:
            self._reserved_invocation_count += 1
        else:
            self._invocation_count += 1
        self._tool_counts[name] = tool_count + 1
        self._last_results[name] = result
        self._result_history.setdefault(name, []).append(result)
        completed_activity = self._progress_activity(
            definition,
            arguments=call_arguments,
            result=result,
        )
        completed_message = completed_activity or f"工具 {name} 执行完成"
        result_data = result.get("data")
        result_details = result_data if isinstance(result_data, dict) else {}
        emit_progress(
            self._progress_callback,
            ProgressEvent(
                stage="agent.tool",
                status=ProgressStatus.COMPLETED,
                message=completed_message,
                elapsed_ms=(perf_counter() - started) * 1000,
                details={
                    "tool": name,
                    "activity": completed_activity,
                    "evidence_id": result.get("evidence_id"),
                    "tool_count": self._tool_counts[name],
                    "returned_rows": result_details.get("returned_rows"),
                    "truncated": result_details.get("truncated"),
                },
            ),
        )
        return result

    @staticmethod
    def _progress_activity(
        definition: ToolDefinition,
        *,
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
    ) -> str | None:
        if definition.progress_formatter is None:
            return None
        try:
            value = definition.progress_formatter(arguments, result)
        except Exception:
            return None
        normalized = " ".join(str(value or "").split())
        if not normalized:
            return None
        if len(normalized) > 120:
            return normalized[:119].rstrip() + "…"
        return normalized

    def preload(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self.invoke(name, arguments)
        self._preloaded_results.append(result)
        return result

    def preloaded_results(self) -> list[dict[str, Any]]:
        return [dict(result) for result in self._preloaded_results]

    def last_result(self, name: str) -> dict[str, Any] | None:
        result = self._last_results.get(name)
        return dict(result) if result is not None else None

    def results(self, name: str) -> list[dict[str, Any]]:
        return [dict(result) for result in self._result_history.get(name, [])]

    def seal(self, reason: str) -> None:
        self._sealed_reason = reason.strip() or "进入最终结构化输出阶段"
        emit_progress(
            self._progress_callback,
            ProgressEvent(
                stage="agent.finalize",
                status=ProgressStatus.INFO,
                message="Agent 正在整理并校验结构化结论",
            ),
        )

    def budget_snapshot(self) -> dict[str, Any]:
        return {
            "max_invocations": self._max_invocations,
            "invocation_count": self._invocation_count,
            "remaining_invocations": max(
                self._max_invocations - self._invocation_count,
                0,
            ),
            "reserved_max_invocations": self._reserved_max_invocations,
            "reserved_invocation_count": self._reserved_invocation_count,
            "reserved_remaining_invocations": max(
                self._reserved_max_invocations
                - self._reserved_invocation_count,
                0,
            ),
            "per_tool_limits": dict(self._per_tool_limits),
            "tool_counts": dict(self._tool_counts),
            "sealed": self._sealed_reason is not None,
            "sealed_reason": self._sealed_reason,
        }
