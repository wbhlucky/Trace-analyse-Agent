from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4
from pathlib import Path
from typing import Annotated, Any

import typer

from trace_agent.agent import DefaultAnalysisAgentFactory, LocalAnalysisAgent
from trace_agent.application import AnalyzeApplication
from trace_agent.cli_text import display_width, pad, rpad
from trace_agent.config import LlmRuntimeConfig
from trace_agent.eval import (
    EvalHarness,
    compare as compare_runs,
    iter_cases,
    load as load_run_by_id,
    save_run,
    summarize,
)
from trace_agent.memory.flags import (
    MEMORY_NAMESPACE_ENV,
    MEMORY_ROOT_ENV,
    memory_enabled_explicit,
    memory_namespace,
)
from trace_agent.memory.flags import EVAL_RUN_ID_ENV
from trace_agent.memory.eval_probe import MemoryEvalProbe, build_eval_probe
from trace_agent.models import AgentKind, LlmProvider
from trace_agent.trace import HTraceAdapter

eval_app = typer.Typer(
    name="eval",
    help="确定性 Agent 评测框架",
    no_args_is_help=True,
)


def _factory(
    *,
    agent: str,
    project_root: Path,
    provider: LlmProvider | None = None,
    model: str | None = None,
    memory_enabled: bool = False,
) -> object:
    if agent == "local":
        return lambda: AnalyzeApplication(
            trace_adapter=HTraceAdapter(),
            agent=LocalAnalysisAgent(),
            memory_enabled=memory_enabled,
        )

    llm_config = LlmRuntimeConfig.resolve(
        project_root=project_root,
        provider=provider,
        model=model,
    )
    return lambda: AnalyzeApplication(
        trace_adapter=HTraceAdapter(),
        agent_factory=DefaultAnalysisAgentFactory(llm_config=llm_config),
        memory_enabled=memory_enabled,
    )


def _prepare_eval_memory(output_root: Path) -> bool:
    """Decide whether eval may touch memory, and if so, where.

    Memory is off unless ``TRACE_AGENT_MEMORY=1`` is set explicitly. Leaving it
    on by default meant every trial shared the production corpus, so a wrong
    guess from trial 0 was recalled as a prior by trial 1 and the score measured
    self-contamination instead of agent capability.

    When it is turned on for an A/B run, the store is redirected to a fresh
    eval-only root so the experiment still cannot write into production memory.
    Set ``TRACE_AGENT_MEMORY_ROOT`` yourself to benchmark against a persistent
    corpus instead.
    """
    if memory_enabled_explicit() is not True:
        return False
    if not os.environ.get(MEMORY_ROOT_ENV):
        isolated = output_root / "memory" / f"eval-{uuid4().hex[:8]}"
        os.environ[MEMORY_ROOT_ENV] = str(isolated)
    return True


def _eval_namespace(eval_run_id: str) -> str | None:
    """Namespace to stamp this eval run with, or ``None`` to keep the caller's.

    Section 8 of the design asked for ``eval:{run}:{case}``. The implementation
    is run-scoped deliberately: a case-scoped namespace also severs the
    cross-trial and cross-case links a corpus needs to earn ``active``, so every
    memory an experiment writes would stay ``candidate`` forever and the A/B
    comparison would measure a memory system that cannot learn. Run scope keeps
    the corpus unreadable from production (retrieval never crosses a namespace)
    while still letting trials interact, which is exactly what the contamination
    audit exists to measure.

    An explicitly set ``TRACE_AGENT_MEMORY_NAMESPACE`` wins, the same escape
    hatch ``TRACE_AGENT_MEMORY_ROOT`` offers for benchmarking against a
    persistent corpus.
    """
    if os.environ.get(MEMORY_NAMESPACE_ENV):
        return None
    return f"eval:{eval_run_id}"


def _echo_memory(payload: object, *, namespace: str = "") -> None:
    """Print the memory half of a summary, only when there is something to say.

    Contamination is reported even when the rate is zero, because "we measured it
    and it is clean" is the claim section 14 asks for and silence is not that
    claim. A memory-off run says so explicitly for the same reason: an omitted
    line reads as "forgot to check".
    """
    if not isinstance(payload, dict) or not payload:
        return
    if not payload.get("enabled"):
        typer.echo("Memory: off (no priors injected, no episodes graded)")
        return
    typer.echo(
        "Memory: trials={trials} injected={injected} graded={graded}".format(
            trials=payload.get("trials_with_memory", 0),
            injected=payload.get("injected_total", 0),
            graded=payload.get("episodes_graded", 0),
        )
    )
    if namespace:
        # Named because "clean" is only meaningful next to the corpus it was
        # measured on: a zero rate against an isolated namespace says the
        # experiment behaved, a zero rate against ``project:default`` would say
        # the production corpus has no same-run records at all.
        typer.echo(f"  namespace: {namespace}")
    typer.echo(
        "  self-contamination: {rate} [{flag}] "
        "(unverified={unverified}, trusted-same-run={trusted})".format(
            rate=payload.get("self_contamination_rate", 0.0),
            flag="clean" if payload.get("clean") else "CONTAMINATED",
            unverified=payload.get("contaminated", 0),
            trusted=payload.get("trusted_from_same_run", 0),
        )
    )
    items = payload.get("items") or []
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        typer.echo(
            "  ! {case}/{trial} {record}: {reason}".format(
                case=item.get("case_id", ""),
                trial=item.get("trial_id", ""),
                record=item.get("record_id", ""),
                reason=item.get("reason", ""),
            )
        )
    for transition in (payload.get("gate_transitions") or [])[:5]:
        if not isinstance(transition, dict):
            continue
        typer.echo(
            "  trust {memory}: {before} -> {after} ({reason})".format(
                memory=transition.get("memory_id", ""),
                before=transition.get("from", ""),
                after=transition.get("to", ""),
                reason=transition.get("reason", ""),
            )
        )


def _fmt(value: object, *, signed: bool = False) -> str:
    """Render a metric cell, keeping "not measured" visibly distinct from zero."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return f"{value:+.4f}" if signed else f"{value:.4f}"
    return str(value)


def _echo_ab(result: dict[str, Any]) -> None:
    """Print the section 14 A/B table: three axes, then whether to believe them.

    The verdict is printed last and always, because a delta on its own invites
    the wrong reading. A positive number from a run that recalled its own
    unverified guesses is self-contamination wearing a green arrow, and the
    reasons list is what tells the two apart.
    """
    sides = result.get("sides") or {}
    a = sides.get("a") or {}
    b = sides.get("b") or {}
    deltas = result.get("deltas") or {}
    if not a or not b:
        return

    label = "指标"
    rows = (
        ("grader 通过率", "pass_rate", True),
        ("root cause 命中率", "root_cause_hit_rate", True),
        ("平均工具调用", "avg_tool_calls", True),
        ("平均总分", "mean_score", True),
    )
    width = max(display_width(name) for name, _, _ in rows) + 2
    typer.echo(
        f"A/B（§14）: {a.get('run_id')} -> {b.get('run_id')}"
    )
    typer.echo(
        pad(label, width)
        + rpad("基线 A", 12)
        + rpad("对比 B", 12)
        + rpad("Δ", 12)
    )
    for name, key, signed in rows:
        typer.echo(
            pad(name, width)
            + rpad(_fmt(a.get(key)), 12)
            + rpad(_fmt(b.get(key)), 12)
            + rpad(_fmt(deltas.get(key), signed=signed), 12)
        )

    mem_a = a.get("memory") or {}
    mem_b = b.get("memory") or {}
    typer.echo(
        pad("记忆", width)
        + rpad("off" if not mem_a.get("enabled") else "on", 12)
        + rpad("off" if not mem_b.get("enabled") else "on", 12)
    )
    typer.echo(
        pad("注入条数", width)
        + rpad(str(mem_a.get("injected_total", 0)), 12)
        + rpad(str(mem_b.get("injected_total", 0)), 12)
    )
    typer.echo(
        pad("自污染率", width)
        + rpad(_contamination_cell(mem_a), 12)
        + rpad(_contamination_cell(mem_b), 12)
    )

    verdict = result.get("verdict") or {}
    reasons = verdict.get("reasons") or []
    if verdict.get("trustworthy"):
        typer.secho("结论: 可信（两组 trial 数一致，对比组无自污染）", fg=typer.colors.GREEN)
        return
    typer.secho("结论: 暂不可信", fg=typer.colors.YELLOW)
    for reason in reasons:
        typer.echo(f"  - {reason}")


def _contamination_cell(memory: dict[str, Any]) -> str:
    """Contamination rate with its flag, or a dash when memory was off.

    ``self_contamination_rate`` counts unverified injections only, while
    ``clean`` also folds in trusted same-run ones. Printing a bare
    ``0.00 CONTAMINATED`` for the second case reads as a contradiction, so the
    cell names which axis broke; the verdict block below carries the counts.
    """
    if not memory.get("enabled"):
        return "-"
    rate = memory.get("self_contamination_rate", 0.0)
    if memory.get("clean"):
        return f"{rate:.2f} clean"
    flag = "CONTAMINATED"
    if not memory.get("contaminated") and memory.get("trusted_from_same_run"):
        flag = "CONTAMINATED(同run可信)"
    return f"{rate:.2f} {flag}"


def _eval_memory_probe(
    eval_run_id: str,
    memory_enabled: bool,
    project_root: Path,
) -> MemoryEvalProbe | None:
    """Probe for this run, or ``None`` when memory is not part of the experiment.

    Built after :func:`_prepare_eval_memory` has redirected the store root, so it
    opens the isolated corpus the trials will write to and never the production
    one. Any failure to open yields ``None``: losing the memory metrics of an
    eval run is acceptable, pointing those metrics at a production corpus is not.
    """
    if not memory_enabled:
        return None
    return build_eval_probe(eval_run_id, project_root=project_root)


@contextmanager
def _eval_memory_env(
    eval_run_id: str,
    *,
    memory_enabled: bool,
    project_root: Path,
) -> Iterator[tuple[MemoryEvalProbe | None, str]]:
    """Own the memory environment for exactly as long as one eval run needs it.

    Yields ``(probe, namespace)``. Two variables are stamped and both are
    restored on the way out, including through an exception: a leaked
    ``TRACE_AGENT_EVAL_RUN_ID`` would attribute later episodes to an experiment
    that already finished, and a leaked ``eval:*`` namespace would make every
    subsequent write in this process - a dashboard, a test session - invisible
    to production recall.

    The probe is built *inside* the namespace rather than handed in, because both
    store adapters resolve the namespace once at construction. Building it a line
    earlier would pin the audit to whatever namespace the process happened to be
    in and silently compare the trial prompts against the wrong corpus.
    """
    namespace = _eval_namespace(eval_run_id) if memory_enabled else None
    if namespace is not None:
        os.environ[MEMORY_NAMESPACE_ENV] = namespace
    try:
        probe = _eval_memory_probe(eval_run_id, memory_enabled, project_root)
        if probe is not None:
            os.environ[EVAL_RUN_ID_ENV] = eval_run_id
        # Read while the run still owns the environment: the summary is echoed
        # after this block exits, and resolving it there would print
        # ``project:default`` for trials that never wrote to it.
        yield probe, (memory_namespace() if memory_enabled else "")
    finally:
        os.environ.pop(EVAL_RUN_ID_ENV, None)
        if namespace is not None:
            os.environ.pop(MEMORY_NAMESPACE_ENV, None)


@eval_app.command("run")
def run(
    root: Annotated[
        Path,
        typer.Option("--root", help="eval cases 根目录"),
    ] = Path("evals/cases"),
    case: Annotated[
        str | None,
        typer.Option("--case", help="仅运行指定 case"),
    ] = None,
    suite: Annotated[
        str | None,
        typer.Option("--suite", help="仅运行指定 suite"),
    ] = None,
    trials: Annotated[
        int,
        typer.Option("--trials", help="每个 case 的 trial 次数"),
    ] = 1,
    agent: Annotated[
        str,
        typer.Option("--agent", help="生产 Agent 类型 (local/claude/qoder)"),
    ] = "local",
    provider: Annotated[
        LlmProvider | None,
        typer.Option("--provider", help="BYOK model provider; defaults to project .env"),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="DeepSeek model; defaults to project .env"),
    ] = None,
    output_root: Annotated[
        Path,
        typer.Option("--output-root", help="Eval artifacts 输出目录"),
    ] = Path(".trace-agent/eval"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="以 JSON 输出评测结果"),
    ] = False,
) -> None:
    """对 golden cases 执行确定性评测。"""
    import asyncio

    cases = iter_cases(root, suite=suite)
    if case is not None:
        cases = [item for item in cases if item.id == case]

    project_root = Path.cwd()
    # Minted before anything opens the store, because three consumers need the
    # same id up front: the artifact directory, the isolated namespace, and the
    # episode provenance written through ``TRACE_AGENT_EVAL_RUN_ID`` that the
    # audit compares an injected memory's source episode against.
    eval_run_id = f"eval-{uuid4().hex[:12]}"
    memory_enabled = _prepare_eval_memory(output_root)
    with _eval_memory_env(
        eval_run_id,
        memory_enabled=memory_enabled,
        project_root=project_root,
    ) as (probe, namespace_label):
        factory = _factory(
            agent=agent,
            project_root=project_root,
            provider=provider,
            model=model,
            memory_enabled=memory_enabled,
        )
        run = asyncio.run(
            EvalHarness(
                application_factory=factory,
                output_root=output_root,
                trials=trials,
                agent=AgentKind(agent),
                memory_probe=probe,
            ).run(cases, run_id=eval_run_id)
        )

    artifact_root = output_root / run.run_id
    saved_path = save_run(run, artifact_root / "eval-run.json")

    summary = summarize(run)
    if json_output:
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)

    typer.echo(f"Eval Run: {run.run_id}")
    typer.echo(f"保存: {saved_path}")
    typer.echo(
        f"总体评分: {summary['overall_score']} "
        f"({summary['total_cases']} cases)"
    )
    for case_id, score in sorted(run.case_scores.items()):
        typer.echo(f"  {case_id}: {score}")
    _echo_memory(summary.get("memory"), namespace=namespace_label)


@eval_app.command("report")
def report(
    run_id: Annotated[str, typer.Argument(help="Eval run id")],
    output_root: Annotated[
        Path,
        typer.Option("--output-root", help="Eval artifacts 输出目录"),
    ] = Path(".trace-agent/eval"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="以 JSON 输出评测报告"),
    ] = False,
) -> None:
    """查看一次 Eval Run 的评分报告。"""
    loaded = load_run_by_id(run_id, output_root)
    summary = summarize(loaded)
    if json_output:
        typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)

    typer.echo(f"Eval Run: {loaded.run_id}")
    typer.echo(f"总体评分: {summary['overall_score']}")
    for case_id, score in sorted(loaded.case_scores.items()):
        typer.echo(f"  {case_id}: {score}")
    _echo_memory(summary.get("memory"))


@eval_app.command("compare")
def compare(
    run_a: Annotated[str, typer.Argument(help="基线 Eval run id")],
    run_b: Annotated[str, typer.Argument(help="对比 Eval run id")],
    output_root: Annotated[
        Path,
        typer.Option("--output-root", help="Eval artifacts 输出目录"),
    ] = Path(".trace-agent/eval"),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="以 JSON 输出对比结果"),
    ] = False,
) -> None:
    """对比两个 Eval Run 的评分差异。"""
    a = load_run_by_id(run_a, output_root)
    b = load_run_by_id(run_b, output_root)
    result = compare_runs(a, b)
    if json_output:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)

    typer.echo(f"{run_a} -> {run_b}: {result['overall_delta']:+}")
    for case_id, item in sorted(result["cases"].items()):
        typer.echo(f"  {case_id}: {item['delta']:+}")
    typer.echo("")
    _echo_ab(result)
