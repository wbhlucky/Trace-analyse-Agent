---
name: response-latency-analysis
description: Analyze interaction response latency from a user input event such as a click to the first frame containing effective visual feedback. Use when scenario_type is response-latency to establish input and first-response boundaries, measure user-perceived response time, trace the critical path through application and rendering stages, and test performance causes without claiming full action completion.
---

# Response Latency Analysis

Measure how quickly the user first sees an effective response.

## Establish Boundaries

1. If `start_marker` and `end_marker`/`response_marker` resolve as a unique
   application Slice pair, use them as the application-defined response
   interval and preserve that metric definition.
2. Otherwise identify the user input event and its dispatch into the target
   application. When explicit boundaries are absent,
   `inspect_problem_window_candidates` may provide the last input point plus
   problem-duration fallback under the one-operation assumption.
3. Identify the first presented frame that visibly reflects the requested
   action.
4. Use scenario markers or explicit business context to decide what counts as
   effective feedback.
5. Do not substitute any subsequent frame for the response frame merely because
   it is easy to locate.

## Analyze

1. Measure input-to-first-effective-frame latency.
2. Decompose input dispatch, application handling, UI state update, render
   preparation, and frame presentation when data is available.
3. Test CPU execution, scheduling, locks, IPC, synchronous I/O, task queues, and
   rendering as candidate causes.
4. Compare the same input and visual-response semantics with the baseline.

## Output Requirements

- Report input and first-response boundaries with evidence IDs.
- Describe why the selected frame is effective feedback.
- Do not claim the entire action completed at the response boundary.
- If the effective-response frame cannot be proven, record a limitation and
  avoid a confirmed root cause.
