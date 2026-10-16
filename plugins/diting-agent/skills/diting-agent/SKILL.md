---
name: diting-agent
description: "Drive the DitingAgent CLI to analyze a performance Trace capture and read back its evidence-linked report. Use when a user supplies an .htrace or other supported Trace file and asks about cold start, response latency, completion latency, or frame rate and jank, when a DitingAgent results directory or report.html needs interpreting, or when the user asks to run the local web panel or the deterministic eval harness."
---

# DitingAgent CLI

DitingAgent is a task-shaped Trace analysis agent. It converts the Trace with a
bundled Trace Streamer, registers bounded read-only Trace tools in process,
routes to the matching scenario methodology, and writes a self-contained report.
Run it as a CLI; do not try to reproduce its pipeline by hand.

## Scope And Boundaries

- Four problem types only: `cold-start`, `response-latency`,
  `completion-latency`, `frame-jank`. CPU, scheduling, locks, IPC, synchronous
  I/O, task queues, and rendering are cause dimensions inside them.
- Performance only. Do not investigate memory usage, leaks, GC pressure, or OOM.
- The sibling skills in this plugin (`trace-analysis`, the four scenario skills,
  and `perf-sample-analysis`) are the analysis methodology. Their tool names
  such as `get_trace_overview` and `query_trace_sql` are DitingAgent's in-process
  read-only tools; they are not callable from a Codex session. Read those skills
  and their `references/` to understand or review a result, and to write a good
  `--scenario` / `--symptom`, not as tools to invoke here.
- Never open the converted Trace DB or raw Trace path directly to answer a
  performance question. Cite the Evidence IDs and findings the run produced.

## Prerequisites

Python 3.12 and [uv](https://docs.astral.sh/uv/). From the DitingAgent checkout:

```bash
uv sync --extra claude        # primary SDK
uv run diting-agent --help
```

Model credentials live in `.env` (see `.env.example`). Without them, use
`--agent local`, the built-in deterministic agent, which needs no model access
and is the right choice for a pipeline smoke test.

## Run An Analysis

```bash
uv run diting-agent analyze <trace.htrace> \
  --type completion-latency \
  --scenario "Tap into the detail page and finish rendering" \
  --symptom "Page only stabilizes about 2s after the tap" \
  --problem-duration-ms 2000 \
  --agent local \
  --output ./results/case-001
```

On Windows use `.\.venv\Scripts\diting-agent.exe` instead of `uv run diting-agent`.

Choose the interval inputs by what the user actually knows:

- `--time-range "788297.524337656s-788298.373582031s"` when the window is known.
- `--start-marker` / `--end-marker` or `--operation-marker` for application-defined
  boundaries; markers must resolve uniquely in the intended process.
- `--problem-duration-ms` with no marker, which combines the duration with the
  last valid input event. Treat that as an explicit fallback assumption and say
  so when reporting the result.
- `--refresh-rate` for frame-jank, `--baseline <trace>` for comparison,
  `--target-process` when the app process is ambiguous.

Add `--no-progress` for CI or when redirecting logs, `--show-traceback` when a
run fails, and `--resume` with `--run-id` to continue an interrupted run. The
first conversion of a large Trace is slow; reruns hit the content-addressed DB
cache unless `--refresh-trace-cache` is passed.

## Read The Result

The `--output` directory holds `report.html` (self-contained, open it in a
browser), plus `run.json`, `findings.json`, `performance.json`, `evidence.json`,
`validation.json`, `analysis-checkpoint.json`, and `agent-log.jsonl`.

When summarizing for the user, read `findings.json` and `validation.json` first:
report the problem interval and its winning selection rule, each finding with
its status (`observed`, `suspected`, or `confirmed`) and its Evidence IDs, the
recommendations, and everything listed under `limitations`. Keep observed facts
separate from interpretation, and never upgrade a `suspected` finding to
`confirmed` in prose because the run did not.

## Web Panel And Evals

```bash
uv run diting-agent serve --host 127.0.0.1 --port 8790 --results-dir ./results
uv run diting-agent eval run --root evals/cases --agent local --trials 1
uv run diting-agent eval report <run-id>
```

`serve` starts the local browser panel over an existing results directory and
picks a free port when the requested one is taken. Give the user the URL. Eval
corpora stay isolated from the production memory corpus; do not point an eval
run at production memory roots.
