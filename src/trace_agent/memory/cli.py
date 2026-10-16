"""``diting-agent memory`` - operational surface for the long-term corpus.

Section 11 of the design doc puts ``memory migrate/reindex/stats`` in P1, and the
reason is operational rather than cosmetic: once recall depends on an index, an
operator needs to answer "is the corpus actually populated, on which adapter, and
does the index agree with the rows" without reading SQLite by hand. Each command
here is a thin shell over a store method - no logic lives in this file - so the
same operations remain callable from tests and from the dashboard.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer

from trace_agent.cli_text import display_width, pad, rpad
from trace_agent.memory.backup import (
    KIND_BUNDLE,
    KIND_FILE_TREE,
    KIND_SQLITE_SNAPSHOT,
    backup_corpus,
)
from trace_agent.memory.dreaming import DreamingService
from trace_agent.memory.embedding import SemanticIndex, build_embedding_provider
from trace_agent.memory.flags import (
    MEMORY_STORE_KINDS,
    memory_embedding_dim,
    memory_store_kind,
)
from trace_agent.memory.lifecycle import revoke_sign_off, sign_off
from trace_agent.memory.migrate import (
    MigrationReport,
    import_bundle,
    migrate_store,
    read_bundle,
    write_export,
)
from trace_agent.memory.paths import resolve_memory_root
from trace_agent.memory.store import build_memory_store, store_diagnostics

memory_app = typer.Typer(
    name="memory",
    help="长期记忆的迁移、索引与统计",
    no_args_is_help=True,
)

RootOption = Annotated[
    Path | None,
    typer.Option(
        "--root",
        help="记忆根目录（含 memory.db 与 curated/ 等），默认由环境变量或 cwd 推导",
    ),
]
NamespaceOption = Annotated[
    str | None,
    typer.Option("--namespace", help="隔离域，默认取 TRACE_AGENT_MEMORY_NAMESPACE"),
]
StoreOption = Annotated[
    str | None,
    typer.Option(
        "--store",
        help="存储适配器：sqlite / json / postgres，默认取环境变量",
    ),
]


def _root(root: Path | None) -> Path:
    """Resolve the memory root.

    An explicit ``--root`` *is* the memory root, not a project root. Reusing
    :func:`resolve_memory_root` for it would append ``.trace-agent/memory`` and
    silently operate on an empty corpus next to the one the operator meant - the
    worst possible failure mode for a migration command.
    """
    if root is not None:
        return Path(root).expanduser().resolve()
    return resolve_memory_root(Path.cwd())


def _semantic_index(backend: Any) -> SemanticIndex:
    """Vector index over ``backend``, using the configured embedding provider.

    Built from the same factory the runtime uses, so ``memory stats``/``maintain``
    report and backfill against the recipe recall actually queries with. When
    embeddings are off - or the adapter cannot hold vectors - ``available`` is
    False and every index method degrades to a no-op, which is what keeps these
    commands safe to run on a JSON rollback corpus.
    """
    return SemanticIndex(backend, build_embedding_provider().provider)


def _echo_report(report: MigrationReport) -> None:
    typer.echo(f"来源     : {report.source_kind}")
    typer.echo(f"目标     : {report.target_kind}")
    typer.echo(f"命名空间 : {report.namespace}")
    if report.dry_run:
        typer.secho("模式     : dry-run（未写入）", fg=typer.colors.YELLOW)
    typer.echo(f"记忆     : {report.memories}")
    typer.echo(f"片段     : {report.episodes}")
    typer.echo(f"会话     : {report.sessions}")
    if report.collapsed_runs:
        typer.echo(
            f"同 run 合并: {report.collapsed_runs}"
            "（episodes 表 UNIQUE(namespace, run_id) 去重，属预期）"
        )
    # Printed only when non-zero, for the same reason as ``审计轨迹``: a corpus
    # whose trust was all earned (or all already ``candidate``) has nothing to
    # report here, and ``信任降级 : 0`` would read as "the gate ran and found
    # nothing" rather than "there was nothing to demote".
    if report.demoted:
        typer.secho(
            f"信任降级 : {report.demoted}"
            "（旧 trust 既无审计轨迹佐证、也不满足当前 gate，降为 candidate；"
            "跑 memory maintain 可凭新证据重新晋升）",
            fg=typer.colors.YELLOW,
        )
    # Printed only when non-zero: a version-1 bundle carries no governance
    # section, and a target without ``import_governance`` copies none. Showing
    # ``审计轨迹 : 0`` there would read as "the history was dropped" rather than
    # "there was nothing to carry".
    if report.versions:
        typer.echo(f"审计轨迹 : {report.versions}")
    if report.links:
        typer.echo(f"记忆关系 : {report.links}")
    if report.backup:
        typer.echo(f"备份     : {report.backup}")
    if report.verified:
        # ``verified`` records that the read-back ran, not that it came out
        # clean; saying so only when nothing is missing keeps this line from
        # contradicting the ``校验未通过`` lines printed straight below it.
        if report.mismatch:
            typer.echo("校验     : 已按 id 回读目标语料库，存在未命中项（见下）")
        else:
            typer.echo("校验     : 已按 id 回读目标语料库，全部命中")
    for line in report.mismatch:
        typer.secho(f"校验未通过: {line}", fg=typer.colors.YELLOW)
    for line in report.errors:
        typer.secho(f"错误     : {line}", fg=typer.colors.RED)
    typer.echo(f"耗时     : {report.duration_ms:.1f} ms")


@memory_app.command("stats")
def stats_command(
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出，便于脚本与面板消费")
    ] = False,
) -> None:
    """打印当前记忆库的适配器、规模与 trust 分布。"""
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    try:
        payload = store_diagnostics(backend)
        summary = backend.stats()
        embedding = _semantic_index(backend).coverage()
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if as_json:
        typer.echo(
            json.dumps(
                {**payload, "stats": asdict(summary), "embedding": embedding},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    typer.echo(f"存储     : {payload['kind']}")
    typer.echo(f"命名空间 : {payload['namespace']}")
    typer.echo(f"根目录   : {payload['root']}")
    if payload["db_path"]:
        typer.echo(f"数据库   : {payload['db_path']}")
    typer.echo(f"索引下推 : {'是' if payload['pushdown'] else '否（全量扫描）'}")
    if payload["degraded_reason"]:
        typer.secho(
            f"降级原因 : {payload['degraded_reason']}", fg=typer.colors.YELLOW
        )
    typer.echo(f"记忆     : {summary.memories}")
    for label, buckets in (
        ("trust", summary.by_trust),
        ("type", summary.by_type),
        ("kind", summary.by_kind),
    ):
        if buckets:
            detail = " ".join(f"{key}={value}" for key, value in sorted(buckets.items()))
            typer.echo(f"  {label:<6}: {detail}")
    typer.echo(f"片段     : {summary.episodes}")
    typer.echo(f"会话     : {summary.sessions}")
    typer.echo(f"版本     : {summary.versions}")
    typer.echo(f"关联     : {summary.links}（矛盾 {summary.contradictions}）")
    typer.echo(f"已过期   : {summary.expired}")


@memory_app.command("migrate")
def migrate_command(
    source: Annotated[
        str, typer.Option("--from", help="来源适配器：json / sqlite")
    ] = "json",
    target: Annotated[
        str, typer.Option("--to", help="目标适配器：sqlite / json")
    ] = "sqlite",
    root: RootOption = None,
    namespace: NamespaceOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只统计将要迁移的记录，不写入")
    ] = False,
    no_backup: Annotated[
        bool, typer.Option("--no-backup", help="跳过迁移前的自动导出备份")
    ] = False,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="跳过迁移后的回读校验")
    ] = False,
    keep_trust: Annotated[
        bool,
        typer.Option(
            "--keep-trust",
            help="原样保留旧语料的 trust，不做降级（默认会降级无佐证的历史 trust）",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出迁移报告")
    ] = False,
) -> None:
    """在存储适配器之间迁移记忆库，幂等可重跑。

    旧语料里那些「单次 run 就写成 active」的记录，trust 是不可信的（历史
    ``occurrences`` 按写入次数自增）。默认按迁移与兼容第 4 条把它们降回
    ``candidate``，让当前 gate 凭新证据重新晋升；有审计轨迹佐证、或凭记录
    自身证据仍能过 gate 的，原样保留。``--keep-trust`` 关闭这一步。
    """
    for kind in (source, target):
        if kind.strip().lower() not in MEMORY_STORE_KINDS:
            typer.secho(
                f"未知适配器: {kind}（可选 {', '.join(sorted(MEMORY_STORE_KINDS))}）",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=2)

    resolved = _root(root)
    report = migrate_store(
        resolved,
        source_kind=source,
        target_kind=target,
        namespace=namespace,
        dry_run=dry_run,
        backup=not no_backup,
        verify=not no_verify,
        keep_trust=keep_trust,
    )
    if as_json:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        _echo_report(report)
    if not report.ok:
        typer.secho(
            "迁移未通过校验，请勿切换 TRACE_AGENT_MEMORY_STORE",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if not dry_run:
        # sqlite is already the default, so on a current checkout the usual
        # outcome of this command is a corpus the runtime reads with no further
        # step. Telling that operator to set a variable they already have invites
        # them to change a working configuration.
        if memory_store_kind() == target:
            typer.secho(
                f"完成。当前 TRACE_AGENT_MEMORY_STORE 已是 {target}，无需切换",
                fg=typer.colors.GREEN,
            )
        else:
            typer.secho(
                f"完成。切换方式：设置 TRACE_AGENT_MEMORY_STORE={target}",
                fg=typer.colors.GREEN,
            )


@memory_app.command("reindex")
def reindex_command(
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
) -> None:
    """重建全文索引。分词规则或索引字段变更后使用。"""
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    try:
        indexed = backend.reindex()
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    if not indexed:
        typer.secho(
            f"{getattr(backend, 'store_kind', '?')} 适配器无需索引（0 行）",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(f"已重建索引：{indexed} 行", fg=typer.colors.GREEN)


@memory_app.command("embed")
def embed_command(
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    limit: Annotated[
        int | None,
        typer.Option(
            "--limit",
            help="本次最多回填多少条，默认取 TRACE_AGENT_MEMORY_EMBED_LIMIT",
        ),
    ] = None,
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="先清空全部向量再用当前模型重建（更换 embedding 配方后使用）",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出覆盖率，便于脚本与面板消费")
    ] = False,
) -> None:
    """为缺失向量的记忆回填 embedding，让语义召回真正生效。

    正常由 dreaming beat 自动完成；迁移、导入或首次开启 embedding 之后，用这条
    命令立即回填，而不必等下一个 tick。``--rebuild`` 用于更换模型：不同宽度的
    向量余弦无意义，必须整体重建而不是让两种配方混在同一个语料里。
    """
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    setup = build_embedding_provider()
    index = SemanticIndex(backend, setup.provider)
    try:
        if not index.available:
            reason = setup.reason or "当前存储适配器不支持向量"
            if as_json:
                typer.echo(
                    json.dumps(
                        {"embedded": 0, "available": False, "reason": reason},
                        ensure_ascii=False,
                    )
                )
            else:
                typer.secho(f"embedding 不可用：{reason}", fg=typer.colors.YELLOW)
            return
        count = index.rebuild(limit=limit) if rebuild else index.backfill(limit=limit)
        coverage = index.coverage()
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if as_json:
        typer.echo(
            json.dumps(
                {"embedded": count, "rebuild": rebuild, **coverage},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    verb = "重建" if rebuild else "回填"
    typer.secho(f"已{verb}向量：{count} 条", fg=typer.colors.GREEN)
    typer.echo(f"模型     : {coverage.get('model')}（{coverage.get('kind')}）")
    typer.echo(
        f"记忆向量 : {coverage.get('memories_embedded', 0)}"
        f"/{coverage.get('memories', 0)}"
    )
    typer.echo(
        f"片段向量 : {coverage.get('episodes_embedded', 0)}"
        f"/{coverage.get('episodes', 0)}"
    )
    if coverage.get("unembedded"):
        typer.echo(f"待回填   : {coverage['unembedded']}")
    if coverage.get("stale"):
        typer.secho(
            f"陈旧向量 : {coverage['stale']}（模型不一致，建议 --rebuild）",
            fg=typer.colors.YELLOW,
        )
    if coverage.get("last_error"):
        typer.secho(f"最近错误 : {coverage['last_error']}", fg=typer.colors.RED)


@memory_app.command("vector-index")
def vector_index_command(
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    dim: Annotated[
        int | None,
        typer.Option(
            "--dim",
            help="锁定的向量宽度，默认取当前 embedding 配方的维度",
        ),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出报告，便于脚本与面板消费")
    ] = False,
) -> None:
    """把 embedding 列收窄到定宽并建 HNSW 索引（PostgreSQL + pgvector）。

    设计稿 §10 把目标态写成 ``vector(dim)``，而 schema 建的是**无界** ``vector``：宽度
    是配方的属性，钉进 DDL 会让一次 ``TRACE_AGENT_MEMORY_EMBEDDING_DIM`` 改动变成写入
    错误。所以收窄是一条显式的运维命令，在语料确定了配方之后跑一次；HNSW 只能建在
    定宽列上，这一步同时把「拉回全部向量在 Python 里算余弦」换成服务器内的近似最近邻。

    收窄之后换配方要先 ``memory embed --rebuild``：不同宽度的向量之间余弦无意义，而
    定宽列会直接拒绝写入——报错，而不是静默截断一个语料库。
    """
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    # The width has to be the one the recipe that *fills* this column produces,
    # so it comes from the same factory ``memory embed`` uses; ``--dim`` is the
    # escape hatch for a corpus being converted ahead of a config change.
    width = int(dim or build_embedding_provider().dim or memory_embedding_dim())
    build = getattr(backend, "vector_index", None)
    try:
        if not callable(build):
            report: dict[str, Any] = {
                "supported": False,
                "dim": width,
                "errors": [
                    f"{getattr(backend, 'store_kind', type(backend).__name__)} "
                    "适配器没有向量索引：向量在应用层精确计算，不需要 ANN 索引"
                ],
            }
        else:
            report = {"supported": True, **build(width)}
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if as_json:
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        kind = getattr(backend, "store_kind", type(backend).__name__)
        typer.echo(f"存储     : {kind}")
        typer.echo(f"向量宽度 : {report.get('dim', width)}")
        if report.get("supported"):
            # Meaningless on an adapter that has no vector column at all, where
            # "否" would read as "pgvector is missing" rather than "not applicable".
            typer.echo(f"pgvector : {'是' if report.get('pgvector') else '否'}")
        if report.get("narrowed"):
            typer.echo(f"已收窄   : {', '.join(report['narrowed'])}")
        if report.get("indexes"):
            typer.echo(f"HNSW 索引: {', '.join(report['indexes'])}")
        for line in report.get("errors", []):
            # An adapter with no ANN index is explaining itself, not failing;
            # a Postgres report with errors is a build that did not happen.
            if report.get("supported"):
                typer.secho(f"错误     : {line}", fg=typer.colors.RED)
            else:
                typer.secho(f"说明     : {line}", fg=typer.colors.YELLOW)
    if not report.get("supported"):
        # Not a failure: the adapter has no ANN index to build, and saying so is
        # the whole answer. Exit 0 keeps this usable in a shared provisioning
        # script that runs against whichever adapter a deployment picked.
        return
    if report.get("errors") or not report.get("indexes"):
        raise typer.Exit(code=1)
    typer.secho("向量索引就绪。", fg=typer.colors.GREEN)


@memory_app.command("export")
def export_command(
    destination: Annotated[
        Path, typer.Option("--out", help="导出文件路径（JSON 快照）")
    ],
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
) -> None:
    """导出整个命名空间的记忆快照，用于备份或跨环境搬运。"""
    resolved = _root(root)
    backend = build_memory_store(
        resolved, namespace=namespace, kind=store or memory_store_kind()
    )
    try:
        path = write_export(backend, destination)
        bundle: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    typer.secho(
        "已导出 "
        f"{len(bundle.get('memories') or [])} 条记忆 / "
        f"{len(bundle.get('episodes') or [])} 个片段 / "
        f"{len(bundle.get('sessions') or [])} 个会话 -> {path}",
        fg=typer.colors.GREEN,
    )


@memory_app.command("import")
def import_command(
    bundle_path: Annotated[
        Path, typer.Option("--bundle", help="由 memory export 生成的 JSON 快照")
    ],
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="只统计，不写入")
    ] = False,
) -> None:
    """从快照导入记忆，按 id upsert，可安全重跑。

    与 ``memory migrate`` 的区别是有意的：这里**不做** trust 降级。快照是它自己
    那份语料的权威副本，把它放回去的操作者要的就是备份里的内容；降级只属于
    「旧语料换底座」这一种迁移场景（见 ``memory migrate --keep-trust``）。
    """
    if not bundle_path.is_file():
        typer.secho(f"快照不存在: {bundle_path}", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    resolved = _root(root)
    backend = build_memory_store(
        resolved, namespace=namespace, kind=store or memory_store_kind()
    )
    try:
        report = import_bundle(backend, read_bundle(bundle_path), dry_run=dry_run)
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()
    _echo_report(report)
    if report.errors:
        raise typer.Exit(code=1)


@memory_app.command("maintain")
def maintain_command(
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="以 JSON 输出治理报告，便于脚本与面板消费"),
    ] = False,
) -> None:
    """立即跑一轮记忆治理：冲突检测、信任跃迁、过期归档与会话摘要。

    后台 dreaming beat 会周期性地做同样的事，但迁移、导入、或人工刚发现两条
    记忆互相矛盾之后，不该等到下一个 tick。这条命令让治理可以按需触发，输出
    与 beat 完全一致，因为调用的是同一个 ``DreamingService.maintain()``。
    """
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    try:
        batch = DreamingService(
            backend, semantic=_semantic_index(backend)
        ).maintain()
        summary = backend.stats()
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    payload = batch.as_dict()
    if as_json:
        typer.echo(
            json.dumps(
                {**payload, "stats": asdict(summary)},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
        return

    typer.echo(f"矛盾链接 : {payload['conflicts']}")
    typer.echo(f"重复归并 : {payload['duplicates']}")
    typer.echo(f"信任跃迁 : {len(payload['transitions'])}")
    for item in payload["transitions"]:
        typer.echo(
            f"  {item['memory_id']}: {item['from']} -> {item['to']}"
            f"（{item['reason'] or '未说明'}，by {item['actor']}）"
        )
    typer.echo(f"已归档   : {len(payload['archived'])}")
    typer.echo(f"过期清理 : {payload['expired']}")
    typer.echo(f"时效标记 : {payload['stamped']}")
    typer.echo(f"会话摘要 : {len(payload['summarized'])}")
    # Printed unconditionally, unlike the migration report's ``信任降级``: a
    # maintain run that refreshed nothing is the normal outcome and worth seeing
    # as a zero, because a missing line would be indistinguishable from a build
    # where the project job does not run at all.
    typer.echo(f"项目摘要 : {len(payload['projects'])}")
    typer.echo(f"记忆总数 : {summary.memories}")
    if summary.by_trust:
        detail = " ".join(
            f"{key}={value}" for key, value in sorted(summary.by_trust.items())
        )
        typer.echo(f"  trust  : {detail}")
    if batch.errors:
        typer.secho(f"错误     : {batch.errors}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    typer.secho("治理完成。", fg=typer.colors.GREEN)


@memory_app.command("verify")
def verify_command(
    memory_id: Annotated[
        str, typer.Argument(help="要签署（或撤回签署）的记忆 id")
    ],
    by: Annotated[
        str,
        typer.Option(
            "--by",
            help="签署人身份：真实姓名/工号/邮箱，pending 之类占位符会被拒绝",
        ),
    ],
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    note: Annotated[
        str, typer.Option("--note", help="签署理由，一并写进审计版本行")
    ] = "",
    revoke: Annotated[
        bool,
        typer.Option("--revoke", help="撤回签署，并把信任级别交回证据重新判定"),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出签署结果")
    ] = False,
) -> None:
    """人工把一条记忆签署为 verified，或撤回已有签署。

    第 5.2 节把人工签署列为 ``active -> verified`` 的三条通道之一，第 13 节把它
    列为"闸门过严"的缓解手段。后者才是它真正的用途：真实语料里结论大多是
    ``suspected``，自动闸门拿不到 confirmed 证据，记忆会永远停在 candidate，
    而看起来唯一的出路是调低阈值——那会同时削弱整个记忆库的抗自污染能力。签署
    让人可以在不动阈值的前提下为一条结论背书。

    签署写的只是 ``provenance.validated_by``，信任级别仍然由闸门判定，所以两次
    独立反证照样会把它归档：人工背书不能推翻证据。撤回之后同样交回证据，五个
    独立 run 一致的结论会留在 verified，单次 run 的猜测会掉回 candidate。
    """
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    try:
        outcome = (
            revoke_sign_off(backend, memory_id, revoked_by=by, note=note)
            if revoke
            else sign_off(backend, memory_id, validated_by=by, note=note)
        )
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if as_json:
        typer.echo(
            json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2, default=str)
        )
        if not outcome.accepted:
            raise typer.Exit(code=1)
        return

    typer.echo(f"记忆     : {outcome.memory_id}")
    typer.echo(f"操作     : {'撤回签署' if revoke else '人工签署'}")
    typer.echo(f"身份     : {outcome.validated_by or '-'}")
    if note:
        # Echoed back because the note lands in a version row the operator cannot
        # see from here, and a signature whose justification silently failed to
        # persist is worse than one never written.
        typer.echo(f"理由     : {note}")
    typer.echo(
        f"信任级别 : {outcome.status.value if outcome.status is not None else '-'}"
    )
    transition = outcome.transition
    if transition is not None:
        typer.echo(
            f"信任跃迁 : {transition.previous.value} -> {transition.current.value}"
        )
        typer.echo(f"依据     : {transition.reason or '未说明'}")
        typer.echo(f"审计者   : {transition.actor}")
    typer.echo(f"说明     : {outcome.detail or '-'}")
    if not outcome.accepted:
        typer.secho("未生效，记忆库没有改动。", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if transition is not None:
        typer.secho("完成，信任跃迁已写入审计版本行。", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"完成，信任级别保持 {outcome.status.value if outcome.status else '-'}。",
            fg=typer.colors.GREEN,
        )


@memory_app.command("namespaces")
def namespaces_command(
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出，便于脚本与面板消费")
    ] = False,
) -> None:
    """列出同一存储上的所有隔离域及其规模。

    第 8 节的命名空间隔离只有在可被检查时才是可被执行的：运维需要看到
    ``eval:*`` 域确实存在、长到了多大、最后一次写入是什么时候，而不必去查询
    那个本该与之隔离的记忆库。``*`` 标记当前 ``--namespace`` 指向的域。
    """
    resolved = _root(root)
    backend = build_memory_store(resolved, namespace=namespace, kind=store)
    try:
        reader = getattr(backend, "list_namespaces", None)
        rows = list(reader()) if callable(reader) else []
        payload = {
            "store": str(getattr(backend, "store_kind", "unknown")),
            "current": str(getattr(backend, "namespace", "")),
            "degraded_reason": getattr(backend, "degraded_reason", None),
            "namespaces": rows,
        }
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    typer.echo(f"存储     : {payload['store']}")
    typer.echo(f"当前域   : {payload['current']}")
    if payload["degraded_reason"]:
        typer.secho(
            f"降级原因 : {payload['degraded_reason']}", fg=typer.colors.YELLOW
        )
    if not rows:
        typer.secho(
            "没有可列出的隔离域（记忆库为空，或当前适配器不支持多域枚举）",
            fg=typer.colors.YELLOW,
        )
        return

    names = [str(row.get("namespace", "")) for row in rows]
    width = max(display_width(name) for name in names) + 3
    typer.echo(
        pad("隔离域", width)
        + rpad("记忆", 6)
        + rpad("片段", 6)
        + rpad("会话", 6)
        + "  最近更新"
    )
    for row, name in zip(rows, names, strict=True):
        mark = "*" if row.get("current") else " "
        typer.echo(
            f"{mark}{pad(name, width - 1)}"
            f"{int(row.get('memories') or 0):>6}"
            f"{int(row.get('episodes') or 0):>6}"
            f"{int(row.get('sessions') or 0):>6}"
            f"  {row.get('last_updated') or '-'}"
        )
    total = sum(int(row.get("memories") or 0) for row in rows)
    typer.echo(f"合计     : {len(rows)} 个隔离域 / {total} 条记忆")


@memory_app.command("backup")
def backup_command(
    out: Annotated[
        Path, typer.Option("--out", help="备份输出目录（不存在会自动创建）")
    ],
    root: RootOption = None,
    namespace: NamespaceOption = None,
    store: StoreOption = None,
    bundle: Annotated[
        bool,
        typer.Option(
            "--bundle",
            help="额外导出逻辑快照，可用 memory import 跨适配器恢复",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="以 JSON 输出备份报告")
    ] = False,
) -> None:
    """为当前命名空间生成一次可校验的备份。

    备份的价值等于它的恢复路径，所以每种适配器都按它真正能被恢复的形态快照：
    SQLite 用 ``VACUUM INTO`` 取事务一致的单文件副本（直接拷贝 ``memory.db``
    会和 WAL 竞争）；JSON 适配器整棵命名空间目录复制；PostgreSQL 没有本地数据
    目录可碰，产出的是 ``memory import`` 能恢复的逻辑快照。三种情况都会写一份
    manifest，记录条数、校验和与当时的 ``stats()``——这让"备份里的记录数和语料
    一致"成为一条运维能自己跑的断言。
    """
    resolved = _root(root)
    backend = build_memory_store(
        resolved, namespace=namespace, kind=store or memory_store_kind()
    )
    try:
        report = backup_corpus(backend, out, include_bundle=bundle)
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()

    if as_json:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
        if not report.artifacts:
            raise typer.Exit(code=1)
        return

    typer.echo(f"存储     : {report.store_kind}")
    typer.echo(f"命名空间 : {report.namespace}")
    typer.echo(f"目录     : {report.destination}")
    for item in report.artifacts:
        typer.echo(
            f"产物     : {item.path.name}（{item.kind}，{item.records} 条记录，"
            f"{item.files} 个文件，{item.bytes / 1024:.1f} KB）"
        )
        typer.echo(f"校验和   : {item.sha256 or '-'}")
        hint = _restore_hint(item.kind)
        if hint:
            typer.echo(f"恢复方式 : {hint}")
    if report.manifest:
        typer.echo(f"清单     : {report.manifest.name}")
    for note in report.notes:
        typer.secho(f"说明     : {note}", fg=typer.colors.YELLOW)
    if not report.artifacts:
        typer.secho("备份没有产出任何文件", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("备份完成。", fg=typer.colors.GREEN)


_RESTORE_HINTS = {
    KIND_SQLITE_SNAPSHOT: "停写后用该文件替换记忆根下的 SQLite 数据库文件",
    KIND_FILE_TREE: "把目录内容拷回记忆根（或 --root 指向它）",
    KIND_BUNDLE: "diting-agent memory import --bundle <该文件>",
}


def _restore_hint(kind: str) -> str:
    """How an operator gets this artifact back into a live corpus."""
    return _RESTORE_HINTS.get(kind, "")


__all__ = ["memory_app"]
