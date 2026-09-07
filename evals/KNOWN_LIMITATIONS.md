# Known Limitations — Deterministic Outcome Grader

This file records grader limitations that are intentional for the current
version and should be revisited when the next eval layer lands.

## 1. Level-2 alias matching can produce false positives

**Status:** known, tracked by an `xfail` test.

`RootCauseGold` / `BottleneckGold` matching uses canonical ID + alias terms as
substring matches against the agent's free-text findings. Because it never sees
a structured `category`, it can "pass" an otherwise wrong classification when a
valid Gold term appears inside an incorrect explanation.

Example:

```text
Gold canonical: render_vsync_distribution
Agent answer:   "Flutter VSync 接收存在问题，但实际上根因是 GPU 饱和"
```

Here the label "Flutter VSync" matches the Gold alias even though the agent's
actual root cause is wrong.

**Do not fix by adding more aliases.** The durable fix is structured output:
once the Agent emits `bottleneck.category`, `root_cause.type`, and
`uncertainty.topic`, the grader can compare canonical IDs directly (Level 1).

Tracking test: `tests/test_grader_robustness.py::test_wrong_classification_with_keyword_does_not_pass`.

## 2. `uncertainty` topic matching relies on trigger phrases

Uncertainty is graded by topic (canonical `topic` + `aliases`), not by literal
sentences. This fixes false negatives from rewording, but a topic is only
detected when at least one of its trigger phrases appears. Fully synonymous
wording with no shared term still needs a semantic judge (Level 3).