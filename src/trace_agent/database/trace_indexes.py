from __future__ import annotations

import sqlite3
from pathlib import Path

# (index_name, table, column)
#
# TraceStreamer 4.3.7 的 `-e` 导出不创建任何索引，仓库层查询会在运行时
# 对 callstack / sched_slice / instant 等大表触发自动临时索引或全表扫描，
# 超过 SQLiteTraceRepository 的查询超时。这里补建代码中实际 JOIN/过滤
# 使用的辅助索引，全部使用 CREATE INDEX IF NOT EXISTS，幂等可重复执行。
_TRACE_INDEXES: tuple[tuple[str, str, str], ...] = (
    # 冷启动候选：callstack JOIN thread(itid = callid)，56 万行表，最大受益
    ("idx_callstack_callid", "callstack", "callid"),
    # 时间范围扫描（slice/instant 窗口查询）
    ("idx_callstack_ts", "callstack", "ts"),
    # thread JOIN process
    ("idx_thread_ipid", "thread", "ipid"),
    ("idx_thread_itid", "thread", "itid"),
    # process 主键查找
    ("idx_process_ipid", "process", "ipid"),
    # 帧候选/时间线查询
    ("idx_frame_slice_ipid", "frame_slice", "ipid"),
    # frame_maps 双向帧映射
    ("idx_frame_maps_src", "frame_maps", "src_row"),
    ("idx_frame_maps_dst", "frame_maps", "dst_row"),
    # instant 线程点事件查询（ref_type='itid'）
    ("idx_instant_ref", "instant", "ref"),
    # 线程执行分析（sched_slice 按 itid 聚合）
    ("idx_sched_slice_itid", "sched_slice", "itid"),
)


def ensure_trace_indexes(database_path: Path) -> int:
    """为 TraceStreamer 导出的 SQLite DB 补建分析所需的辅助索引。

    幂等：重复执行不会重复建索引，也不影响缓存有效性判断依赖的
    ``metadata.json``（调用方需在数据库尺寸变化时同步更新它）。
    表不存在时跳过对应索引；返回本次新建的索引数量。
    """
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Trace 数据库不存在：{database_path}")

    created = 0
    with sqlite3.connect(database_path) as connection:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table'"
            ).fetchall()
        }
        existing_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index'"
            ).fetchall()
        }
        for name, table, column in _TRACE_INDEXES:
            if table not in existing_tables:
                continue
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {name} "
                f"ON {table}({column})"
            )
            if name not in existing_indexes:
                created += 1
        # 让查询规划器基于真实行数分布选择索引
        connection.execute("ANALYZE")
        connection.commit()
    return created
