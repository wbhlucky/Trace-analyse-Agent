#!/usr/bin/env python
# Section 14 A/B driver: same cases, memory off then memory on, then compare.
#
# Section 14's last acceptance row ("does memory actually help?") cannot be
# answered by unit tests, and answering it by hand means juggling six
# environment variables across two runs that must not see each other's state.
# This script owns that juggling:
#
#   1. each side runs in its own subprocess with every ``TRACE_AGENT_MEMORY*``
#      variable scrubbed first. In-process toggling is not enough: the store,
#      the namespace and the gate thresholds are all resolved from the
#      environment at construction time, so a leaked variable from side A would
#      silently configure side B;
#   2. side B is pinned to an explicit root *and* an explicit namespace. That is
#      the escape hatch ``eval run`` documents: without an explicit namespace
#      every eval run gets a fresh ``eval:<run_id>`` one, which is correct for
#      isolation but means side B starts from an empty corpus and the A/B
#      compares "no memory" against "no memory";
#   3. by default the corpus is snapshot-copied before side B touches it, so an
#      experiment can never write into the production corpus it is benchmarking
#      against (section 8);
#   4. the comparison goes through the real ``eval compare`` CLI, so what is
#      printed here is exactly what a reviewer would see running it themselves -
#      including the verdict block that refuses to read a contaminated delta as
#      a memory win.
#
# Cost warning: every trial converts its trace. The first run over a ~100MB
# htrace is minutes; later runs reuse ``.trace-agent/cache``.

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

#: Everything that configures memory, scrubbed before each side so neither run
#: inherits the other's - or the caller's shell's - idea of what memory is.
MEMORY_ENVS = (
    "TRACE_AGENT_MEMORY",
    "TRACE_AGENT_MEMORY_ROOT",
    "TRACE_AGENT_MEMORY_NAMESPACE",
    "TRACE_AGENT_MEMORY_STORE",
    "TRACE_AGENT_MEMORY_DSN",
    "TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE",
    "TRACE_AGENT_EVAL_RUN_ID",
)

DEFAULT_CASES = ROOT / "evals" / "cases"
DEFAULT_OUTPUT = ROOT / ".trace-agent" / "eval"
DEFAULT_NAMESPACE = "project:default"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the section 14 memory A/B and print the comparison."
    )
    parser.add_argument("--cases-root", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--case",
        action="append",
        default=None,
        help="restrict to a case id; repeatable (default: every case)",
    )
    parser.add_argument("--suite", default=None)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument(
        "--agent",
        default="local",
        help="local (offline, deterministic) | claude | qoder",
    )
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=None,
        help="warm corpus side B reads; default is a fresh empty root, which "
        "measures the plumbing rather than the value of memory",
    )
    parser.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    parser.add_argument(
        "--include-candidate",
        action="store_true",
        help="let side B recall unverified candidate memories at low weight; "
        "needed for a corpus that has not passed the quality gate yet",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="let side B write into --memory-root itself instead of a copy",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the snapshot corpus after the run for inspection",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the two commands and exit without running anything",
    )
    return parser.parse_args()


def side_env(
    *,
    memory: bool,
    root: Path | None,
    namespace: str,
    include_candidate: bool = False,
) -> dict[str, str]:
    """Environment for one side, with every memory variable set from scratch."""
    env = {k: v for k, v in os.environ.items() if k not in MEMORY_ENVS}
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["TRACE_AGENT_MEMORY"] = "1" if memory else "0"
    if memory:
        env["TRACE_AGENT_MEMORY_ROOT"] = str(root)
        env["TRACE_AGENT_MEMORY_NAMESPACE"] = namespace
        if include_candidate:
            env["TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE"] = "1"
    return env


def corpus_trust_mix(corpus: Path, namespace: str) -> dict[str, int]:
    """Memories per trust level in the corpus side B is about to read.

    Read through the real ``memory stats --json`` rather than by opening the
    store here, so the number checked is the number the CLI would show an
    operator. An empty corpus and a corpus the namespace cannot see look the
    same to this script, and both are worth catching before spending minutes.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_agent",
            "memory",
            "stats",
            "--root",
            str(corpus),
            "--namespace",
            namespace,
            "--json",
        ],
        cwd=ROOT,
        env=side_env(memory=False, root=corpus, namespace=namespace),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        return {}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {}
    buckets = payload.get("stats", {}).get("by_trust") or {}
    return {str(key): int(value) for key, value in buckets.items()}


def preflight_corpus(corpus: Path, namespace: str, *, include_candidate: bool) -> None:
    """Print what side B will read, and refuse to start when it will read nothing.

    Runs before the snapshot, not after: an experiment that cannot produce a
    meaningful delta should cost the operator seconds, not a corpus copy and ten
    minutes of trials.
    """
    subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_agent",
            "memory",
            "stats",
            "--root",
            str(corpus),
            "--namespace",
            namespace,
        ],
        cwd=ROOT,
        env=side_env(memory=False, root=corpus, namespace=namespace),
    )

    # Section 12.4 demotes every migrated record to candidate on purpose, so a
    # freshly migrated production corpus is *all* candidate - and the default
    # trust gate recalls active/verified only. Side B would inject nothing, the
    # two sides would be identical, and the report would read as "memory does not
    # help" instead of "memory was never consulted".
    by_trust = corpus_trust_mix(corpus, namespace)
    recallable = by_trust.get("active", 0) + by_trust.get("verified", 0)
    if by_trust:
        mix = " ".join(f"{key}={value}" for key, value in sorted(by_trust.items()))
        print(f"语料库 trust 分布: {mix}（默认可召回 {recallable} 条）")
    if recallable == 0 and not include_candidate:
        raise SystemExit(
            "语料库中没有 active/verified 记忆：默认信任闸门不会召回任何条目，\n"
            "侧 B 将与侧 A 完全等价，跑完只会得到「两组无差异」。\n"
            "  - 要度量低权重候选先验的效果，加 --include-candidate；\n"
            "  - 要度量已验证记忆的效果，先让语料库过 quality gate"
            "（distinct_runs >= 2 且 confidence >= 阈值）。"
        )


def run_command(args: list[str], env: dict[str, str]) -> None:
    """Run one CLI command with the console attached, raising on failure."""
    print(f"\n$ {' '.join(args)}\n", flush=True)
    completed = subprocess.run(args, cwd=ROOT, env=env)
    if completed.returncode != 0:
        raise SystemExit(f"command failed with exit {completed.returncode}: {' '.join(args)}")


def new_run_ids(output_root: Path, before: set[str]) -> list[str]:
    """Run directories that appeared during a side, newest last."""
    if not output_root.exists():
        return []
    return sorted(
        (p.name for p in output_root.iterdir() if p.is_dir() and p.name not in before)
    )


def snapshot_corpus(source: Path, destination: Path) -> Path:
    """Copy a corpus so the experiment writes to the copy, not to production."""
    if destination.exists():
        shutil.rmtree(destination)
    if source.exists():
        shutil.copytree(source, destination)
    else:
        destination.mkdir(parents=True)
    return destination


def main() -> int:
    args = parse_args()
    output_root: Path = args.output_root
    output_root.mkdir(parents=True, exist_ok=True)

    experiment_root = output_root / "memory" / f"ab-{int(time.time())}"

    # Before the snapshot: side B reads this corpus under an explicit namespace,
    # and a corpus that holds nothing recallable makes both sides identical. An
    # experiment that cannot produce a meaningful delta should cost seconds, not
    # a corpus copy and ten minutes of trials.
    if args.memory_root is not None and not args.dry_run:
        preflight_corpus(
            args.memory_root, args.namespace, include_candidate=args.include_candidate
        )

    if args.memory_root is None:
        corpus = experiment_root / "corpus"
        corpus.mkdir(parents=True, exist_ok=True)
        print(
            "未指定 --memory-root：对比组将读取一个空语料库。\n"
            "这会验证 A/B 链路本身，但注入条数为 0，结论会被判为「两组实际等价」。\n"
            "要度量记忆收益，请用 --memory-root 指向一个已经积累过 active/verified 记忆的语料库。"
        )
    elif args.no_snapshot:
        corpus = args.memory_root
        print(f"按 --no-snapshot：对比组会直接写入 {corpus}")
    else:
        corpus = snapshot_corpus(args.memory_root, experiment_root / "corpus")
        print(f"已快照语料库 {args.memory_root} -> {corpus}（实验不会写入原语料库）")

    common: list[str] = [
        sys.executable,
        "-m",
        "trace_agent",
        "eval",
        "run",
        "--root",
        str(args.cases_root),
        "--agent",
        args.agent,
        "--trials",
        str(args.trials),
        "--output-root",
        str(output_root),
    ]
    if args.suite:
        common += ["--suite", args.suite]
    for case in args.case or []:
        common += ["--case", case]

    if args.dry_run:
        print("\n基线 A（记忆关）:", " ".join(common))
        print("对比 B（记忆开）:", " ".join(common))
        print("对比:", f"{sys.executable} -m trace_agent eval compare <A> <B> --output-root {output_root}")
        return 0

    print("\n=== 基线 A：记忆关 ===")
    before = set(new_run_ids(output_root, set()))
    run_command(common, side_env(memory=False, root=corpus, namespace=args.namespace))
    side_a = new_run_ids(output_root, before)

    mode = "记忆开（含低权重候选）" if args.include_candidate else "记忆开"
    print(f"\n=== 对比 B：{mode} ===")
    before = set(new_run_ids(output_root, set()))
    run_command(
        common,
        side_env(
            memory=True,
            root=corpus,
            namespace=args.namespace,
            include_candidate=args.include_candidate,
        ),
    )
    side_b = new_run_ids(output_root, before)

    if len(side_a) != 1 or len(side_b) != 1:
        raise SystemExit(
            f"无法确定 run id：A={side_a} B={side_b}（期望各一个新增目录）"
        )
    run_a, run_b = side_a[0], side_b[0]
    print(f"\nA={run_a}  B={run_b}")

    run_command(
        [
            sys.executable,
            "-m",
            "trace_agent",
            "eval",
            "compare",
            run_a,
            run_b,
            "--output-root",
            str(output_root),
        ],
        side_env(memory=False, root=corpus, namespace=args.namespace),
    )

    if not args.keep and args.memory_root is not None and not args.no_snapshot:
        shutil.rmtree(experiment_root, ignore_errors=True)
        print(f"\n已清理快照 {experiment_root}（--keep 可保留）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
