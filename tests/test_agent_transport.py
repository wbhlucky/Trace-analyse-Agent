from __future__ import annotations

from trace_agent.agent import AgentTransport, FakeTransport
from trace_agent.models import AnalyzeRequest, ScenarioType


def _request(tmp_path) -> AnalyzeRequest:
    return AnalyzeRequest(
        trace_id="transport",
        trace_path=tmp_path / "trace.htrace",
        scenario_type=ScenarioType.COLD_START,
        scenario="scenario",
        symptom="symptom",
        output_dir=tmp_path,
    )


def test_fake_transport_replays_responses(tmp_path) -> None:
    request = _request(tmp_path)
    transport: AgentTransport = FakeTransport("first", "second")

    async def drive() -> list[object]:
        await transport.start(request)
        messages = []
        async for item in transport.receive():
            messages.append(item)
        await transport.close()
        return messages

    import asyncio

    messages = asyncio.run(drive())
    assert messages == ["first", "second"]
    assert transport.sent == []


def test_fake_transport_records_state(tmp_path) -> None:
    transport = FakeTransport()
    import asyncio

    async def drive() -> None:
        await transport.start(_request(tmp_path))
        await transport.interrupt()

    asyncio.run(drive())
    assert transport.interrupted is True
