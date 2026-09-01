from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from trace_agent import __version__
from trace_agent.agent import DefaultAnalysisAgentFactory
from trace_agent.application import AnalyzeApplication
from trace_agent.cli_progress import CliProgressRenderer
from trace_agent.config import LlmRuntimeConfig, save_project_llm_config
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
    LlmProvider,
    ScenarioType,
)
from trace_agent.trace import HTraceAdapter
from trace_agent.web import serve as serve_web


app = typer.Typer(
    name="diting-agent",
    help="基于可追溯证据的性能 Trace 分析工具。",
    no_args_is_help=True,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="显示版本号。",
        ),
    ] = None,
) -> None:
    del version


@app.command()
def configure(
    provider: Annotated[
        LlmProvider,
        typer.Option("--provider", help="Qoder BYOK 模型 Provider。"),
    ] = LlmProvider.DEEPSEEK,
    model: Annotated[
        str | None,
        typer.Option("--model", help="可选 DeepSeek 模型覆盖。"),
    ] = None,
) -> None:
    api_key = typer.prompt(
        "DeepSeek API Key",
        hide_input=True,
    )
    try:
        path = save_project_llm_config(
            project_root=Path.cwd(),
            provider=provider,
            api_key=api_key,
            model=model,
        )
    except ValueError as exc:
        typer.secho(f"配置失败：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho("Qoder BYOK 配置完成", fg=typer.colors.GREEN)
    typer.echo(f"Provider: {provider.value}")
    typer.echo(f"配置文件: {path.resolve()}")


@app.command()
def serve(
    host: Annotated[
        str,
        typer.Option("--host", help="监听地址。"),
    ] = "127.0.0.1",
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="监听端口。被占用时自动选择可用端口。"),
    ] = 8080,
    results_dir: Annotated[
        Path,
        typer.Option(
            "--results-dir",
            help="分析结果目录，默认使用当前目录下的 results/。",
        ),
    ] = Path("results"),
    open_browser: Annotated[
        bool,
        typer.Option(
            "--open/--no-open",
            help="启动后自动打开浏览器。",
        ),
    ] = True,
) -> None:
    """启动 DitingAgent 本地 Web 面板。"""
    serve_web(
        host=host,
        port=port,
        results_dir=results_dir,
        open_browser=open_browser,
    )

@app.command()
def analyze(
    trace_path: Annotated[
        Path,
        typer.Argument(help="待分析的 Trace 文件。"),
    ],
    scenario_type: Annotated[
        ScenarioType,
        typer.Option(
            "--type",
            help="问题类型：冷启动、响应时延、完成时延或帧率/丢帧。",
        ),
    ],
    scenario: Annotated[
        str,
        typer.Option("--scenario", help="性能分析场景。"),
    ],
    symptom: Annotated[
        str,
        typer.Option("--symptom", help="观察到的问题现象。"),
    ],
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="结果输出目录。"),
    ],
    trace_id: Annotated[
        str | None,
        typer.Option("--trace-id", help="可选 Trace ID；默认使用文件名。"),
    ] = None,
    device: Annotated[
        str | None,
        typer.Option("--device", help="设备型号。"),
    ] = None,
    build: Annotated[
        str | None,
        typer.Option("--build", help="构建版本。"),
    ] = None,
    time_range: Annotated[
        str | None,
        typer.Option("--time-range", help="可选分析区间。"),
    ] = None,
    target_process: Annotated[
        str | None,
        typer.Option("--target-process", help="目标应用或进程提示。"),
    ] = None,
    operation_marker: Annotated[
        str | None,
        typer.Option(
            "--operation-marker",
            help=(
                "唯一且带 duration 的业务操作 Slice；其 ts 到 ts+dur "
                "可作为应用定义的完成时延区间。"
            ),
        ),
    ] = None,
    start_marker: Annotated[
        str | None,
        typer.Option("--start-marker", help="输入或启动起点 Marker 提示。"),
    ] = None,
    end_marker: Annotated[
        str | None,
        typer.Option(
            "--end-marker",
            help="应用自定义问题终点 Slice/Marker；与起点均唯一时组成区间。",
        ),
    ] = None,
    response_marker: Annotated[
        str | None,
        typer.Option(
            "--response-marker",
            help="第一帧有效响应 Marker 提示。",
        ),
    ] = None,
    completion_marker: Annotated[
        str | None,
        typer.Option(
            "--completion-marker",
            help="动作完成 Marker 提示。",
        ),
    ] = None,
    problem_duration_ms: Annotated[
        float | None,
        typer.Option(
            "--problem-duration-ms",
            help="已知问题持续时间；可与默认最后输入打点组合推导区间。",
            min=0.001,
            max=3_600_000,
        ),
    ] = None,
    refresh_rate: Annotated[
        float | None,
        typer.Option(
            "--refresh-rate",
            help="显示刷新率提示，单位 Hz。",
            min=1,
        ),
    ] = None,
    baseline: Annotated[
        Path | None,
        typer.Option("--baseline", help="可选基线 Trace。"),
    ] = None,
    agent: Annotated[
        AgentKind,
        typer.Option("--agent", help="分析 Agent。"),
    ] = AgentKind.LOCAL,
    provider: Annotated[
        LlmProvider | None,
        typer.Option(
            "--provider",
            help="Qoder BYOK 模型 Provider；默认读取项目 .env。",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="DeepSeek 模型；未指定时读取项目 .env。",
        ),
    ] = None,
    trace_streamer: Annotated[
        Path | None,
        typer.Option(
            "--trace-streamer",
            help="开发调试覆盖；默认使用工程内对应平台的二进制。",
        ),
    ] = None,
    trace_streamer_timeout: Annotated[
        float,
        typer.Option(
            "--trace-streamer-timeout",
            help="Trace 转 DB 超时秒数。",
            min=1,
        ),
    ] = 600,
    trace_cache: Annotated[
        bool,
        typer.Option(
            "--trace-cache/--no-trace-cache",
            help="复用相同 Trace 和 TraceStreamer 生成的 SQLite DB。",
        ),
    ] = True,
    trace_cache_dir: Annotated[
        Path | None,
        typer.Option(
            "--trace-cache-dir",
            help="Trace DB 缓存目录；默认使用工程 .trace-agent/cache。",
        ),
    ] = None,
    refresh_trace_cache: Annotated[
        bool,
        typer.Option(
            "--refresh-trace-cache",
            help="忽略已有缓存并重新转换后覆盖缓存。",
        ),
    ] = False,
    progress: Annotated[
        bool,
        typer.Option(
            "--progress/--no-progress",
            help="显示当前阶段、Agent 工具取证和耗时。",
        ),
    ] = True,
) -> None:
    llm_config: LlmRuntimeConfig | None = None
    if agent is AgentKind.QODER:
        try:
            llm_config = LlmRuntimeConfig.resolve(
                project_root=Path.cwd(),
                provider=provider,
                model=model,
            )
        except ValueError as exc:
            raise typer.BadParameter(
                str(exc),
                param_hint="--provider",
            ) from exc
        provider = llm_config.provider
        model = llm_config.model

    request = AnalyzeRequest(
        trace_id=trace_id or trace_path.stem,
        trace_path=trace_path,
        scenario_type=scenario_type,
        scenario=scenario,
        symptom=symptom,
        output_dir=output,
        device=device,
        build=build,
        time_range=time_range,
        target_process=target_process,
        operation_marker=operation_marker,
        start_marker=start_marker,
        end_marker=end_marker,
        response_marker=response_marker,
        completion_marker=completion_marker,
        problem_duration_ms=problem_duration_ms,
        refresh_rate_hz=refresh_rate,
        baseline_trace_path=baseline,
        agent=agent,
        provider=provider,
        model=model,
    )

    progress_renderer = CliProgressRenderer() if progress else None
    application = AnalyzeApplication(
        trace_adapter=HTraceAdapter(
            trace_streamer_path=trace_streamer,
            timeout_seconds=trace_streamer_timeout,
            cache_dir=(
                trace_cache_dir
                or Path.cwd() / ".trace-agent" / "cache" / "trace-db"
                if trace_cache
                else None
            ),
            refresh_cache=refresh_trace_cache,
        ),
        agent_factory=DefaultAnalysisAgentFactory(
            llm_config=llm_config,
        ),
        progress_callback=progress_renderer,
    )

    if progress_renderer is not None:
        progress_renderer.start()
    try:
        result = asyncio.run(application.run(request))
    except Exception as exc:
        typer.secho(f"分析失败：{exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        if progress_renderer is not None:
            progress_renderer.stop()

    typer.secho("分析完成", fg=typer.colors.GREEN)
    typer.echo(f"Run ID: {result.run_id}")
    typer.echo(f"报告: {result.report_path}")
