---
name: frame-jank-analysis
description: Analyze frame-rate degradation, slow frames, dropped frames, and visible jank over a bounded interaction or time interval. Use when scenario_type is frame-jank to derive the frame budget from the actual display refresh rate, locate affected frames and clusters, correlate application and rendering work, compare a baseline, and identify evidence-backed performance causes.
---

# Frame Jank Analysis

Analyze frame performance against the actual display cadence.

## Establish Scope

1. Identify the target application and a bounded analysis interval using the
   scenario boundary rules. Do not use the whole Trace when a smaller problem
   interval is proven.
2. Identify the application frame-producing pipeline. Never equate the OS
   process main thread with the UI thread without frame or stack evidence.
3. Call `inspect_frame_jank` after the target ipid and interval are known. Use
   `frame_producer_itid=0` first. When multiple producers exist, select another
   candidate only with framework, frame-ownership, or application evidence.
4. Use the refresh rate or VSync period returned by the Tool. Never assume
   60 Hz. Treat each cadence segment separately when dynamic refresh is seen.
5. Distinguish application actual frames, mapped RenderService presentation,
   expected slots, slow frames, missed deadlines, and dropped candidates.
   Do not claim a presentation drop merely from a long duration.

## Rendering Architecture

1. Treat the critical path as a graph of roles, not a fixed main-thread path:
   input/VSync -> framework UI/JS/GUI -> layout/mount -> raster/render ->
   RenderService/UniRender -> GPU/fence -> presentation.
2. UI work may live away from the main thread in Qt, Flutter, React Native,
   Compose Multiplatform/KMP-hosted UI, and other engines. Framework detection
   is a hypothesis unless supported by thread names plus slices/call stacks or
   frame ownership. KMP alone does not identify the UI toolkit.
3. App-to-RenderService attribution requires `frame_maps`, a VSync/frame link,
   or an equivalent platform correlation. RenderService is a global process;
   its whole-process CPU or Perf share never belongs to the target application.
4. Unified rendering is proven only by UniRender/platform evidence on the
   target frame path. A generic RenderService mapping proves cross-process
   rendering, not the exact unified-rendering mode.
5. GPU attribution requires mapped GPU slices, queues, or fences. Otherwise
   report GPU cause as unavailable.
6. When `frame_producer_ui_mismatch` reports that a strongly corroborated
   framework UI/paint thread differs from the `frame_slice` owner, keep both
   threads in the critical-path analysis. Treat the frame owner as a possible
   platform/wrapper pipeline and do not present its frame count or FPS as the
   definitive FPS of the target UI Surface.

## Metrics

1. Derive `budget_ms = 1000 / refresh_rate_hz` for each cadence segment.
2. Prefer mapped presentation timestamps for effective FPS. Application frame
   production FPS is a separate metric and must be labelled as such.
3. Compute effective FPS over active rendering clusters. A static or mostly
   idle interval must be reported as not meaningful, not as low FPS.
4. Report actual frame count, mapped presented count, expected slots, slow
   frames, jank frames, missed VSyncs, jank rate, P50/P90/P95/P99/max frame
   duration, and the worst consecutive jank clusters when available.
5. Use expected-without-actual only as a dropped-frame candidate unless final
   presentation evidence proves the drop.

## Analyze

1. Start with the worst individual frames and clusters. Analyze their exact
   windows instead of applying a whole-window thread profile to every frame.
2. `observable_delay_stage` locates a late pipeline stage but is not a root
   cause. Correlate it with Running, Runnable, sleep, D-state, priorities, CPU
   distribution, wakeup chains, slices, and target-thread Perf.
3. For application execution overload, name a concrete application slice or
   symbol. Generic event loops, framework wrappers, HAP+offset aggregates, and
   module names are context rather than optimization targets.
4. For scheduling shortage, prove Runnable delay and relevant competing work.
   For blocking, identify the wait and waker when data permits. For I/O, cite
   the exact D-state or synchronous operation.
5. For RenderService delay, use only the target frame's mapped RS thread and
   interval. Separate inherited late submission from RS execution over-budget.
6. Use Bottom-up Perf only to refine a Top-down target-frame path. Scope Perf to
   selected application producer/framework render threads and mapped RS threads.
7. Compare a baseline only with identical interval semantics, target pipeline,
   and cadence.

## Recommendations

1. Tie every recommendation to a proved cause and concrete operation.
2. Application work: split, defer, cache, or move the named critical-path work.
3. Runnable/scheduling: reduce named competing work or correct a proved
   priority/core-distribution issue; do not recommend affinity generically.
4. Wait/I/O: remove the named synchronous dependency, prefetch, batch, or
   shorten the proved lock/IPC chain.
5. RenderService/UniRender: recommend layer, overdraw, effect, texture, or
   composition changes only when target-frame render evidence supports them.
6. GPU: recommend shader/texture/render-target changes only with GPU evidence.
7. Include a verification metric such as effective FPS, missed VSyncs, jank
   rate, P95 duration, or worst-cluster duration.

## Output Requirements

- Cite the selected producer pipeline, refresh/cadence source, frame budget,
  affected interval, bad frame/cluster identifiers, and Evidence IDs.
- State explicitly when effective FPS, Surface identity, unified-rendering mode,
  final presentation, or GPU cause is unavailable.
- Do not use average FPS alone to dismiss isolated but visible jank.
- Stop without a frame finding when frame events, a target pipeline, or a valid
  interval are unavailable.
