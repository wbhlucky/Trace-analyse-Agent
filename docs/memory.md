# Memory Runtime

This document describes the project-scoped memory loop added to
`src/trace_agent/memory`.

## Layers

- Execution state: `analysis-checkpoint.json` and `steps/` provide
  resume/crash recovery. This is not long-term memory.
- Episodic memory: every completed run is projected to an `EpisodicMemory`
  record under `.trace-agent/memory/episodes/`.
- Curated memory: `ConsolidationService` promotes findings and root causes
  into reusable `CuratedMemory` entries (pattern, fact, procedure,
  anti-pattern) under `.trace-agent/memory/curated/`.
- Conditional recall: `RecallService` scores curated and episodic records
  against the current scenario before the agent starts, and injects compact
  context into the Qoder system prompt.
- Provenance and governance: every curated entry references its source
  episodes/runs/evidence and carries status and confidence fields.

## Layout

```
.trace-agent/memory/
├── episodes/<episode-id>.json
├── curated/<memory-id>.json
```

## Configuration

Memory is enabled by default. Set `TRACE_AGENT_MEMORY=0` to disable it.
`TRACE_AGENT_MEMORY=1` enables it explicitly.

```
$env:TRACE_AGENT_MEMORY='0'   # disable
$env:TRACE_AGENT_MEMORY='1'   # enable (default)
```

## Failure semantics

All memory operations are best-effort. A missing or unwritable memory
directory, or a recall/consolidation failure, degrades retrieval quality
but never blocks or fails the analysis task.

## Notes

Memory context is advisory. Current Trace evidence always takes precedence
over recalled project knowledge.
