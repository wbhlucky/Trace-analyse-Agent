from __future__ import annotations

from pathlib import Path

from trace_agent.memory import (
    MemoryRuntime,
    build_episode,
    default_memory_runtime,
)
from trace_agent.memory.backend import JsonMemoryBackend
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    MemoryStatus,
    Provenance,
)
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
                analysis="main thread blocked on IO",
                recommendation="move IO off main thread",
                verification="manual",
            )
        ],
    )


def test_episode_backend_roundtrip(tmp_path: Path) -> None:
    backend = JsonMemoryBackend(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    backend.write_episode(episode)
    loaded = backend.load_episode(episode.episode_id)
    assert loaded is not None
    assert loaded.run_id == "run-1"
    assert loaded.scenario_type == "cold-start"
    assert loaded.root_causes
    assert loaded.findings[0]["title"] == "MainThread blocked"


def test_consolidation_deduplicates_and_promotes(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    first = runtime.remember_episode(episode, consolidate=True)
    second = runtime.remember_episode(episode, consolidate=True)
    assert len(first) == 1
    assert first[0].memory_id == second[0].memory_id
    assert second[0].occurrences == 2
    assert second[0].status is MemoryStatus.ACTIVE


def test_recall_ranks_curated_and_episodic(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    runtime.remember_episode(episode, consolidate=True)
    result = runtime.recall(
        "main thread blocked cold start",
        scenario_type="cold-start",
    )
    assert result.hits
    assert "MainThread blocked" in result.context_prompt
    assert any(
        hit.memory.kind is MemoryKind.PROCEDURE
        for hit in result.hits
        if isinstance(hit.memory, CuratedMemory)
    )


def test_recall_is_empty_for_unrelated_query(tmp_path: Path) -> None:
    runtime = MemoryRuntime(tmp_path)
    episode = build_episode(
        _request(),
        _analysis(),
        run_id="run-1",
        output_dir=str(tmp_path / "case"),
    )
    runtime.remember_episode(episode, consolidate=True)
    result = runtime.recall("frame jank rendering fps")
    assert result.hits == []


def test_default_runtime_uses_project_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    runtime = default_memory_runtime(Path.cwd())
    assert runtime.backend.root == tmp_path / ".trace-agent" / "memory"
