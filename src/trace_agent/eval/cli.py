from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from trace_agent.agent import DefaultAnalysisAgentFactory, LocalAnalysisAgent
from trace_agent.application import AnalyzeApplication
from trace_agent.config import LlmRuntimeConfig
from trace_agent.eval import (
    EvalHarness,
    compare as compare_runs,
    iter_cases,
    load as load_run_by_id,
    save_run,
    summarize,
)
from trace_agent.models import AgentKind
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
) -> object:
    if agent == "local":
        return lambda: AnalyzeApplication(
            trace_adapter=HTraceAdapter(),
            agent=LocalAnalysisAgent(),
        )

    llm_config = LlmRuntimeConfig.resolve(project_root=project_root)
    return lambda: AnalyzeApplication(
        trace_adapter=HTraceAdapter(),
        agent_factory=DefaultAnalysisAgentFactory(llm_config=llm_config),
    )


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
    factory = _factory(agent=agent, project_root=project_root)

    run = asyncio.run(
        EvalHarness(
            application_factory=factory,
            output_root=output_root,
            trials=trials,
            agent=AgentKind(agent),
        ).run(cases)
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
