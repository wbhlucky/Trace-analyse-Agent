# Report Standard

Each finding must contain:

- a concise problem title;
- severity proportional to user impact;
- `observed`, `suspected`, or `confirmed` status;
- calibrated confidence;
- one or more evidence IDs when a finding is reported;
- analysis that connects evidence to the conclusion;
- an actionable recommendation;
- a measurable regression verification step.

Do not repeat the same evidence as multiple findings. Put unsupported questions
and data gaps in `limitations` instead of presenting them as findings.

When a problem interval is established, fill `problem_interval` and report:

- the application/scenario metric definition;
- exact start and end boundaries;
- the winning interval-selection rule;
- whether the one-operation fallback assumption was used;
- evidence IDs and any incomplete Trace coverage.
