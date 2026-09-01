# DitingAgent Architecture

The project is a modular monolith.  Infrastructure may implement domain
ports, but domain facts and application policies must not depend on a model
provider or an HTML template.

## Dependency direction

```text
CLI
  -> AnalyzeApplication
      -> ScenarioCatalog
      -> TraceAdapter / ToolRegistry
      -> AnalysisAgent port
      -> EvidenceStore / EvidenceIndex
      -> Normalizers / Validators
      -> ReportProjectionBuilder
          -> ReportRenderer

Qoder Agent SDK -> AnalysisAgent port implementation
SQLite / TraceStreamer -> Trace and repository implementations
Jinja / Canvas -> ReportRenderer implementation
```

## Responsibility rules

### Deterministic core

- Converts Trace files and detects capabilities.
- Executes bounded read-only queries and calculations.
- Records immutable Evidence and selects it through `EvidenceIndex`.
- Hydrates deterministic fields and validates numerical consistency.
- Projects validated Analysis and Trace facts into report view data.

### Agent

- Selects semantically meaningful candidates.
- Chooses which available tool to call next.
- Relates Trace, scheduling, frame, and Perf Evidence.
- Produces explanations, root-cause judgments, confidence, and advice.

The Agent must not recreate deterministic thread, scheduling, or Perf
profiles.  Provider adapters must not own scenario evidence requirements;
they delegate to `EvidenceSubmissionPolicy`.

### Skills

- Describe analysis method and evidence standards.
- Must not hard-code application-specific marker names as global truth.
- Must not replace deterministic Tool input validation or result validation.

### Presentation

- `ReportProjectionBuilder` may read validated Analysis, Evidence, and Trace
  DB facts to create a report view.
- `ReportRenderer` only renders a supplied projection and never executes SQL,
  chooses related threads, or derives a diagnosis.
- The final report may stay self-contained even when source templates and
  assets are split.

## Extension rules

When adding a scenario:

1. Register its Skill, report title, result field, and preflight invocation in
   `ScenarioCatalog`.
2. Reuse cross-cutting process, marker, frame, scheduling, wakeup, and Perf
   capabilities before adding a scenario-specific Tool.
3. Add scenario validation without changing an Agent provider adapter.
4. Add report sections through projection data, not template-side analysis.
5. Add contract and characterization tests before changing metric semantics.

When adding a Tool:

1. Return deterministic facts and limitations, not an LLM-style conclusion.
2. Declare required Trace capabilities and a bounded invocation budget.
3. Store the full result as Evidence and expose only a compact Agent view when
   the payload is large.
4. Add a typed Evidence lookup when more than one consumer needs the result.

## Remaining decomposition targets

The boundaries above are active.  Further refactoring can now be mechanical:

- split timeline, Perf, and startup-module report projectors;
- split CSS, HTML sections, timeline JS, and flame-graph JS while keeping the
  final HTML self-contained;
- split Tool definitions by capability;
- split validators and domain models by scenario;
- centralize SQLite table/column introspection.

These changes must preserve current SQL semantics and real-Trace results.
