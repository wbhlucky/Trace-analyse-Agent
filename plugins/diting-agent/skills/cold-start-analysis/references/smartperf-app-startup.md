# SmartPerf AppStartup Semantics

Use this reference only to interpret rows that actually exist in
`app_startup`. It is not a fallback Marker dictionary.

## Parser-Defined Phases

The Trace Streamer `AppStartup` configuration bundled with the project defines
these six ordered phase labels:

| Order | `start_name` | Meaning |
| --- | --- | --- |
| 1 | `ProcessTouchEvent` | Input dispatch and touch processing |
| 2 | `StartUIAbilityBySCB` | Start request handled by the scene board |
| 3 | `LoadAbility` | Ability load and application-process preparation |
| 4 | `Application Launching` | Application launch work |
| 5 | `UI Ability Launching` | UIAbility creation and launch |
| 6 | `UI Ability OnForeground` | UIAbility foreground transition |

The parser creates a phase interval from configured start and end Marker
patterns. Those patterns can execute in different processes or threads.

SmartPerf views can additionally show `First Frame - App Phase` and
`First Frame - Render Phase`. Treat those as related frame/render evidence.
Do not assume they are `app_startup` rows unless the current database actually
contains them.

## Field Handling

- Resolve `start_name` through `data_dict` when it is dictionary-backed.
- Treat `packed_name` as the target application or bundle name. Depending on
  the parser version it can be direct text or a `data_dict` ID.
- Treat `start_time` and `end_time` as nanoseconds on the Trace timeline.
- Treat `ipid` as the process executing that phase, not automatically as the
  target application process.
- Preserve `call_id` and `tid` as raw identifiers until their exact relation is
  confirmed against the current parser schema.

## Analysis Rules

1. Prefer actual rows, filtered by the resolved `packed_name`, and order them
   by `start_time`.
2. Validate timestamps against `trace_range` and reject negative intervals.
3. Keep the cross-process chain intact; do not filter all rows to the selected
   application `ipid`.
4. Correlate phase boundaries with lifecycle Slices, process creation, and
   first-frame evidence before choosing total cold-start boundaries.
5. Do not blindly sum overlapping or duplicate phase intervals.
6. If the table is absent or empty, do not synthesize these six labels. Discover
   the actual lifecycle and rendering evidence in that Trace and lower
   confidence when boundaries remain ambiguous.
7. Treat the phase names in this reference as versioned parser knowledge.
   Actual rows and the current bundled Trace Streamer configuration take
   precedence.
