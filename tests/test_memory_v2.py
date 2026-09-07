from __future__ import annotations

from pathlib import Path

from trace_agent.memory import MemoryRuntime, build_episode
from trace_agent.memory.models import SessionRole
from trace_agent.models import (
    AgentKind,
    AnalysisResult,
    AnalyzeRequest,
    Finding,
    FindingSeverity,
    FindingStatus,
    ScenarioType,
)


def _request() -> AnalyzeRequest:
    return AnalyzeRequest(
        trace_id="trace-1",
        trace_path=Path("/tmp/trace.htrace"),
        scenario_type=ScenarioType.COLD_START,
        scenario="open app",
        symptom="long white screen",
        output_dir=Path("/tmp/case"),
        target_process="com.example.app",
        device="phone",
        build="1.0.0",
        agent=AgentKind.LOCAL,
    )


def _analysis() -> AnalysisResult:
    return AnalysisResult(
        summary="cold start is slow",
        findings=[
            Finding(
                title="MainThread blocked",
                severity=FindingSeverity.HIGH,
                status=FindingStatus.CONFIRMED,
                confidence=0.9,
                evidence_ids=["ev-0001"],
                analysis="main thread blocked on io",
                recommendation="move off main thread",
                verification="manual",
            )
        ],
    )


def test_session_memory_roundtrip(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    session = runtime.sessions.create_session(title="cold start followup")
    session_id = session.session_id
    runtime.append_session_message(session_id, "why is this slow?", role="user")
    loaded = runtime.sessions.get_session(session_id)
    assert loaded is not None
    assert [m.role for m in loaded.messages] == [SessionRole.USER]
    assert loaded.messages[0].content == "why is this slow?"


def test_deep_recall_upgrades_weak_history_query(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    runtime.remember_episode(episode, consolidate=True)
    result = runtime.recall(
        "has this device had a similar main thread issue before?",
        scenario_type="cold-start",
    )
    assert result.context_prompt
    # Deep recall should have surfaced curated or episodic context.
    assert "MainThread" in result.context_prompt or "main thread" in result.context_prompt


def test_execution_snapshot_detects_resumable(tmp_path: Path) -> None:
    from trace_agent.application.checkpoint import StepCheckpointStore
    from trace_agent.models import StepStatus

    output = tmp_path / "case"
    output.mkdir()
    checkpoint = StepCheckpointStore(output)
    checkpoint.record(
        "run.prepare",
        status=StepStatus.DONE,
        input_hash="abc",
    )
    checkpoint.record(
        "trace.prepare",
        status=StepStatus.FAILED,
        input_hash="abc",
        error="boom",
    )
    (output / "run.json").write_text(
        '{"run_id": "run-1", "status": "interrupted", "step_states": []}',
        encoding="utf-8",
    )
    runtime = MemoryRuntime(tmp_path)
    snapshot = runtime.resume_plan(output)
    assert snapshot.run_id == "run-1"
    assert snapshot.resumable is True
    assert "run.prepare" in snapshot.completed_steps
    assert "trace.prepare" in snapshot.failed_steps


def test_dreaming_consumes_queued_episode(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    runtime.remember_episode(episode, consolidate=False, dream=True)
    assert runtime.dreaming.run_once(limit=10).processed == 1
