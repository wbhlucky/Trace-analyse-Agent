from __future__ import annotations

import asyncio
from pathlib import Path

from trace_agent.agent import AgentResponse, AgentRuntime, FakeRunTransport
from trace_agent.contract.task import TaskContract
from trace_agent.models import ScenarioType


def _task() -> TaskContract:
    return TaskContract(
        task_id="runtime",
        trace_id="runtime",
        trace_path=Path("trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="scenario",
        symptom="symptom",
        output_dir=Path("."),
    )


def test_runtime_invokes_transport_and_reports_verification() -> None:
    transport = FakeRunTransport(AgentResponse(text="done"))
    runtime = AgentRuntime(transport, max_repair_rounds=0)

    result = asyncio.run(runtime.run(_task()))
    assert result.conclusion == "done"
    assert len(transport.requests) == 1
    assert "verification" in result.metadata


def test_fake_run_transport_reuses_session_id() -> None:
    transport = FakeRunTransport()
    runtime = AgentRuntime(transport, max_repair_rounds=0)
    asyncio.run(runtime.run(_task()))

    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.session_id is not None
    assert request.session_id.startswith("runtime-")
