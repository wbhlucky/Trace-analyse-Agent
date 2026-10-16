"""The write path has to govern itself when nothing else will.

Two deployments share one memory corpus: the dashboard, which owns a 30s beat and
a periodic ``maintain()``, and the CLI (and eval, which drives the CLI), which
owns nothing and exits as soon as the report is on disk. Every job a CLI run
enqueued therefore waited for a process that never came, and the sweep that turns
"two runs agreed" into a ``supports`` link never ran at all. A corpus built from
the command line stayed entirely ``candidate`` no matter how much corroboration it
contained - the shape the warm-up run produced, and the reason ``_govern_memory``
exists.

These tests pin the wiring rather than the governance rules themselves: the queue
drains, the sweep runs, the transitions reach ``run.json``, and a locked store
costs a beat instead of an analysis.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from trace_agent.application import AnalyzeApplication
from trace_agent.memory import MemoryRuntime, MemoryStatus
from trace_agent.memory.flags import MEMORY_PROMPT_CHARS_ENV
from trace_agent.memory.lifecycle.quality_gate import DERIVED_DIGEST_KINDS
from trace_agent.memory.models import EpisodicMemory, MemoryTrace
from trace_agent.runtime.events import EventType

# One conclusion, worded differently by each run. Deliberately the shape the real
# corpus produced: below the conflict ceiling, above the overlap floor.
_AGREEMENT = (
    "the main thread was blocked by a synchronous file read during cold launch",
    "the main thread was blocked by a synchronous file read during warm launch",
)


def _episode(index: int, analysis: str) -> EpisodicMemory:
    return EpisodicMemory(
        episode_id=f"ep-{index}",
        run_id=f"run-{index}",
        trace_id=f"trace-{index}",
        scenario_type="white_screen",
        scenario="cold start white screen",
        symptom="blank page after launch",
        summary="main thread blocked",
        root_causes=[analysis],
        findings=[
            {
                "title": "Main thread blocked",
                "severity": "high",
                "status": "confirmed",
                "confidence": 0.85,
                "analysis": analysis,
                "recommendation": "",
                "evidence_ids": ["ev-1"],
            }
        ],
    )


def _seed(runtime: MemoryRuntime) -> None:
    """Two finished CLI runs, exactly as ``_remember_episode`` leaves them."""
    for index, analysis in enumerate(_AGREEMENT, start=1):
        episode = _episode(index, analysis)
        runtime.remember_episode(episode, consolidate=True)
        runtime.dreaming.enqueue_gate_review(episode.episode_id)


def _application(
    runtime: MemoryRuntime | None,
    tmp_path: Path,
    *,
    memory_enabled: bool = True,
) -> AnalyzeApplication:
    application = AnalyzeApplication(
        trace_adapter=object(),
        agent=object(),
        memory_runtime=runtime,
        memory_enabled=memory_enabled,
    )
    application._memory_trace = MemoryTrace(enabled=True)
    application._current_run_path = tmp_path / "run.json"
    return application


def test_governance_promotes_what_the_beat_never_saw(tmp_path: Path) -> None:
    """Two agreeing runs reach ``active`` without a dashboard, and say so."""
    runtime = MemoryRuntime(tmp_path / "memory")
    _seed(runtime)
    assert all(
        memory.status is MemoryStatus.CANDIDATE
        for memory in runtime.store.list_curated(active_only=False)
    )
    manifest = SimpleNamespace(memory_trace=None)

    _application(runtime, tmp_path)._govern_memory(manifest)

    assert all(
        memory.status is MemoryStatus.ACTIVE
        for memory in runtime.store.list_curated(active_only=False)
    )
    trace = manifest.memory_trace
    assert trace is not None
    # Every governed claim moved, and only those: the sweep's inline ``maintain()``
    # also writes the derived digests, which are exempt from the gate and land
    # ``active`` on insert - so they are active without ever appearing in a
    # transition, and comparing against the raw corpus would count them as misses.
    governed = [
        memory
        for memory in runtime.store.list_curated(active_only=False)
        if memory.kind not in DERIVED_DIGEST_KINDS
    ]
    assert governed
    assert sorted(trace["trust_changed"]) == sorted(
        memory.memory_id for memory in governed
    )


def test_governance_is_switchable_and_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off means off, and a missing runtime is not an error.

    The flag exists for the deployment that runs a beat of its own and does not
    want a second sweep racing it; the ``None`` branch is the memory-disabled
    path, where there is nothing to govern.
    """
    runtime = MemoryRuntime(tmp_path / "memory")
    _seed(runtime)
    manifest = SimpleNamespace(memory_trace=None)

    monkeypatch.setenv("TRACE_AGENT_MEMORY_INLINE_GOVERNANCE", "0")
    _application(runtime, tmp_path)._govern_memory(manifest)
    assert manifest.memory_trace is None
    assert all(
        memory.status is MemoryStatus.CANDIDATE
        for memory in runtime.store.list_curated(active_only=False)
    )

    monkeypatch.setenv("TRACE_AGENT_MEMORY_INLINE_GOVERNANCE", "1")
    _application(None, tmp_path)._govern_memory(manifest)
    assert manifest.memory_trace is None


def test_governance_survives_a_broken_store(tmp_path: Path) -> None:
    """A locked database costs a governance beat, never a finished analysis.

    ``_govern_memory`` runs after the report is written, so the only thing it can
    still damage is the run that already succeeded. Every memory call in this
    module carries the same contract; this is the one place it is checked for the
    governance path.
    """
    runtime = MemoryRuntime(tmp_path / "memory")
    _seed(runtime)
    manifest = SimpleNamespace(memory_trace=None)

    def explode() -> None:
        raise RuntimeError("database is locked")

    runtime.dreaming.drain_inline = explode  # type: ignore[method-assign]

    _application(runtime, tmp_path)._govern_memory(manifest)

    assert manifest.memory_trace is None


def test_recall_event_carries_latency_and_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The last hop of the section 12.4 chain: composer, result, trace, event.

    ``truncated`` is only useful if the party that reads ``run.json`` can see it,
    and ``latency_ms`` only means one thing if the event quotes the number the
    retrieval stamped rather than a second measurement taken around it. Both are
    asserted against the trace, which is what lands on disk, so a payload that
    drifted from the trace would fail here instead of in a dashboard nobody is
    looking at.
    """
    runtime = MemoryRuntime(tmp_path / "memory")
    _seed(runtime)
    runtime.dreaming.drain_inline()

    published: list[tuple[Any, dict[str, Any]]] = []
    application = _application(runtime, tmp_path)
    application._run_context = SimpleNamespace(
        publish=lambda event_type, data=None: published.append(
            (event_type, dict(data or {}))
        )
    )
    request = SimpleNamespace(
        scenario_type=SimpleNamespace(value="white_screen"),
        scenario="冷启动白屏",
        symptom="main thread blocked by a synchronous file read",
        target_process=None,
        device=None,
        build=None,
    )

    application._prepare_memory(request, tmp_path, resuming=False)
    roomy = application._memory_trace
    assert roomy is not None and roomy.enabled and roomy.hits
    assert roomy.latency_ms > 0.0
    assert roomy.truncated is False

    monkeypatch.setenv(MEMORY_PROMPT_CHARS_ENV, "120")
    application._prepare_memory(request, tmp_path, resuming=False)
    tight = application._memory_trace
    assert tight is not None and tight.hits
    assert tight.truncated is True
    assert len(tight.injected_ids) < len(tight.hits)

    recalled = [
        payload
        for event_type, payload in published
        if event_type is EventType.MEMORY_RECALLED
    ]
    assert len(recalled) == 2
    for trace, payload in ((roomy, recalled[0]), (tight, recalled[1])):
        assert payload["latency_ms"] == trace.latency_ms
        assert payload["truncated"] is trace.truncated
