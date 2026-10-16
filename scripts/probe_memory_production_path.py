# Production-path memory probe: real trace, real CLI, isolated corpus.
#
# The unit tests build a corpus by hand and then assert on it. That proves the
# gate, the composer and the store agree with each other; it does not prove the
# analysis pipeline calls them. This script closes that gap by driving
# ``diting-agent analyze`` twice over one real trace and then reading back what
# the two runs actually left behind:
#
#   1. write path  - one episode per run, projected once (section 5.1 removed the
#                    second consolidation that used to fake a cross-run
#                    corroboration out of a single run);
#   2. gate        - neither episode is trusted by default, because ``--agent
#                    local`` produces no findings and an uncorroborated episode
#                    derives to ``candidate`` (appendix F);
#   3. read path   - ``run.json.memory_trace`` shows the query, the namespace,
#                    the hits and what reached the prompt, which is the only
#                    place section 14's self-contamination rate can come from;
#   4. channel     - re-running recall with ``TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE=1``
#                    must surface those same episodes. Without this step an empty
#                    ``hits`` list is ambiguous: "correctly gated" and "silently
#                    broken" look identical.
#
# Cost warning: each analyze re-converts the trace into its own work database,
# which is minutes for a ~100MB htrace. Use --inspect-only to re-read a corpus a
# previous run already produced instead of paying for the conversion twice.

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DEFAULT_TRACE = Path(r"D:\Users\Administrator\Desktop\trace\白屏分析\jd_white_screen.htrace")
DEFAULT_MEMORY_ROOT = ROOT / ".codex_tmp" / "prod-memory"
DEFAULT_OUTPUT = ROOT / ".codex_tmp" / "analyze-mem"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Drive two production analyze runs and read back the memory corpus."
    )
    parser.add_argument("--trace", type=Path, default=DEFAULT_TRACE)
    parser.add_argument("--memory-root", type=Path, default=DEFAULT_MEMORY_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="skip analyze and read back whatever the corpus already holds",
    )
    return parser.parse_args()


def configure_environment(memory_root: Path) -> None:
    """Pin the corpus before anything snapshots the namespace.

    Both store adapters read ``TRACE_AGENT_MEMORY_NAMESPACE`` at construction
    time, so the environment has to be right before the first runtime is built -
    and the eval stamp has to be gone, or this run would be recorded as an
    experiment instead of as production.
    """
    os.environ["TRACE_AGENT_MEMORY"] = "1"
    os.environ["TRACE_AGENT_MEMORY_ROOT"] = str(memory_root)
    os.environ.pop("TRACE_AGENT_EVAL_RUN_ID", None)
    os.environ.pop("TRACE_AGENT_MEMORY_NAMESPACE", None)
    os.environ.pop("TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE", None)


def analyse(args: argparse.Namespace, index: int) -> None:
    from trace_agent.cli import app

    sys.argv = [
        "diting-agent",
        "analyze",
        str(args.trace),
        "--type",
        "frame-jank",
        "--scenario",
        "京东进入商品详情白屏",
        "--symptom",
        "点击进入商品详情后长时间白屏，首屏渲染明显延后",
        "--time-range",
        "1485769162578256ns-1485770662578256ns",
        "--operation-marker",
        "ABILITY_OR_PAGE_SWITCH",
        "--agent",
        "local",
        "--output",
        str(args.output / f"run{index}"),
    ]
    print(f"===== analyze #{index} =====", flush=True)
    try:
        app()
    except SystemExit as exc:  # typer exits the process; we want the next run
        print(f"exit code: {exc.code}", flush=True)


def inspect(args: argparse.Namespace) -> None:
    from trace_agent.memory.rerank import episode_confidence, episode_trust
    from trace_agent.memory.runtime import MemoryRuntime
    from trace_agent.memory.store import build_memory_store, store_diagnostics

    print("===== corpus =====", flush=True)
    store = build_memory_store(args.memory_root)
    print("diagnostics:", json.dumps(store_diagnostics(store), ensure_ascii=False), flush=True)
    stats = store.stats()
    print(
        "memories={m} episodes={e} sessions={s} by_trust={t}".format(
            m=stats.memories, e=stats.episodes, s=stats.sessions, t=stats.by_trust
        ),
        flush=True,
    )
    for episode in store.list_episodes():
        print(
            "  episode {id} run={run} scenario={sc} findings={n} trust={t} conf={c:.2f} "
            "graded={g} eval={ev}".format(
                id=episode.episode_id,
                run=episode.run_id,
                sc=episode.scenario_type,
                n=len(episode.findings),
                t=episode_trust(episode).value,
                c=episode_confidence(episode),
                g=episode.graded_pass,
                ev=episode.eval_run_id,
            ),
            flush=True,
        )
    for memory in store.list_curated():
        print(
            "  memory {id} trust={t} kind={k} conf={c:.2f} runs={r} topic={p}".format(
                id=memory.memory_id,
                t=memory.status.value,
                k=memory.kind.value,
                c=memory.confidence,
                r=store.distinct_runs(memory.memory_id),
                p=memory.topic[:40],
            ),
            flush=True,
        )

    print("===== memory_trace =====", flush=True)
    query = ""
    scenario_type = ""
    for index in range(1, args.runs + 1):
        manifest = args.output / f"run{index}" / "run.json"
        if not manifest.is_file():
            print(f"  run{index}: no manifest", flush=True)
            continue
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        trace = payload.get("memory_trace") or {}
        query = trace.get("query") or query
        scenario_type = payload.get("scenario_type") or scenario_type
        print(f"  run{index}:", json.dumps(trace, ensure_ascii=False), flush=True)

    if not query:
        print("===== recall: no query recorded, skipped =====", flush=True)
        return

    # Two reads of the same corpus, differing only in the gate. The first is
    # what production injects; the second proves an empty first result was a
    # decision and not a broken channel.
    print("===== recall =====", flush=True)
    runtime = MemoryRuntime(args.memory_root)
    default = runtime.recall(query, scenario_type=scenario_type, top_k=5)
    print(f"  default     : hits={len(default.hits)} chars={len(default.context_prompt)}", flush=True)
    os.environ["TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE"] = "1"
    widened = MemoryRuntime(args.memory_root).recall(
        query, scenario_type=scenario_type, top_k=5
    )
    print(
        "  candidate-on: hits={n} ids={ids}".format(
            n=len(widened.hits),
            ids=[
                getattr(hit.memory, "memory_id", None)
                or getattr(hit.memory, "episode_id", "")
                for hit in widened.hits
            ],
        ),
        flush=True,
    )
    if widened.context_prompt:
        print("  prompt:", flush=True)
        for line in widened.context_prompt.splitlines():
            print(f"    {line}", flush=True)


def main() -> None:
    args = parse_args()
    configure_environment(args.memory_root)
    if not args.inspect_only:
        if not args.trace.is_file():
            raise SystemExit(f"trace not found: {args.trace}")
        for index in range(1, args.runs + 1):
            analyse(args, index)
    inspect(args)


if __name__ == "__main__":
    main()
