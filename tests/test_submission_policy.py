from __future__ import annotations

from pathlib import Path
from typing import Any

from trace_agent.models import (
    AnalysisResult,
    AnalyzeRequest,
    ScenarioType,
    TraceCapability,
)
from trace_agent.policies import EvidenceSubmissionPolicy
from trace_agent.tools import ToolDefinition, ToolRegistry


INPUT_NS = 1_000_000_000
RESPONSE_NS = 1_100_000_000
OLD_COMPLETION_NS = 5_000_000_000
USER_COMPLETION_NS = 6_600_000_000


def _request() -> AnalyzeRequest:
    return AnalyzeRequest(
        trace_id="duration-policy",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COMPLETION_LATENCY,
        scenario="open page",
        symptom="about 5.6 seconds",
        output_dir=Path("results"),
        problem_duration_ms=5600,
    )


def _registry() -> ToolRegistry:
    registry = ToolRegistry(
        {TraceCapability.CPU_SCHEDULING, TraceCapability.PERF_SAMPLES}
    )

    def register(name: str) -> None:
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                input_schema={"data": dict},
                required_capabilities=frozenset(),
                handler=lambda arguments: {
                    "evidence_id": f"ev-{name}",
                    "data": arguments["data"],
                },
            )
        )

    register("inspect_completion_latency_candidates")
    register("inspect_completion_latency_phases")
    register("inspect_perf_profile")
    registry.invoke(
        "inspect_completion_latency_candidates",
        {
            "data": {
                "target_ipid": 10,
                "duration_window": {
                    "start_ns": INPUT_NS,
                    "end_ns": USER_COMPLETION_NS,
                    "metric_definition_from_user": True,
                },
            }
        },
    )
    return registry


def _phase_data(completion_ns: int) -> dict[str, Any]:
    return {
        "target_process": {"ipid": 10},
        "input_ns": INPUT_NS,
        "response_ns": RESPONSE_NS,
        "completion_ns": completion_ns,
        "recommended_perf_scope": {"thread_ids": [100, 101]},
    }


def _perf_data(end_ns: int) -> dict[str, Any]:
    return {
        "interval_start_ns": INPUT_NS,
        "interval_end_ns": end_ns,
        "requested_thread_ids": [100, 101],
    }


def _analysis(completion_ns: int) -> AnalysisResult:
    completion_ms = (completion_ns - INPUT_NS) / 1_000_000.0
    post_ms = (completion_ns - RESPONSE_NS) / 1_000_000.0
    return AnalysisResult.model_validate(
        {
            "summary": "completion",
            "completion_latency": {
                "resolved_process": {
                    "name": "app",
                    "pid": 100,
                    "ipid": 10,
                    "selection_reason": "input owner",
                    "confidence": 0.9,
                },
                "input_boundary": {
                    "name": "input",
                    "timestamp_ns": INPUT_NS,
                    "source": "callstack",
                    "confidence": 0.9,
                },
                "response_boundary": {
                    "name": "response",
                    "timestamp_ns": RESPONSE_NS,
                    "source": "frame_maps",
                    "confidence": 0.8,
                },
                "completion_boundary": {
                    "name": "completion",
                    "timestamp_ns": completion_ns,
                    "source": "user_problem_duration",
                    "confidence": 0.9,
                },
                "completion_proven": True,
                "completion_semantics": "user duration",
                "response_latency_ms": 100,
                "post_response_duration_ms": post_ms,
                "completion_latency_ms": completion_ms,
                "phases": [
                    {
                        "name": "response",
                        "start_ns": INPUT_NS,
                        "end_ns": RESPONSE_NS,
                        "duration_ms": 100,
                        "assessment": "response",
                    },
                    {
                        "name": "post-response",
                        "start_ns": RESPONSE_NS,
                        "end_ns": completion_ns,
                        "duration_ms": post_ms,
                        "assessment": "completion",
                    },
                ],
                "critical_path_summary": "browser work",
            },
        }
    )


def test_duration_policy_requires_exact_phase_and_perf_windows() -> None:
    registry = _registry()
    policy = EvidenceSubmissionPolicy(request=_request(), registry=registry)
    registry.invoke(
        "inspect_completion_latency_phases",
        {"data": _phase_data(OLD_COMPLETION_NS)},
    )
    registry.invoke(
        "inspect_perf_profile",
        {"data": _perf_data(OLD_COMPLETION_NS)},
    )

    rejection = policy.validate_collected_evidence()
    assert rejection is not None
    assert "阶段取证未使用用户 duration" in rejection.error

    registry.invoke(
        "inspect_completion_latency_phases",
        {"data": _phase_data(USER_COMPLETION_NS)},
    )
    rejection = policy.validate_collected_evidence()
    assert rejection is not None
    assert "Perf 未覆盖" in rejection.error

    registry.invoke(
        "inspect_perf_profile",
        {"data": _perf_data(USER_COMPLETION_NS)},
    )
    assert policy.validate_collected_evidence() is None


def test_duration_policy_rejects_wrong_final_boundary() -> None:
    registry = _registry()
    registry.invoke(
        "inspect_completion_latency_phases",
        {"data": _phase_data(USER_COMPLETION_NS)},
    )
    registry.invoke(
        "inspect_perf_profile",
        {"data": _perf_data(USER_COMPLETION_NS)},
    )
    policy = EvidenceSubmissionPolicy(request=_request(), registry=registry)

    rejection = policy.validate_analysis(_analysis(OLD_COMPLETION_NS))
    assert rejection is not None
    assert "最终完成边界" in rejection.error
    assert policy.validate_analysis(_analysis(USER_COMPLETION_NS)) is None
