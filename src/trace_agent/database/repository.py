from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any, Sequence


class TraceQueryError(RuntimeError):
    """Raised when a Trace DB query is invalid, unsafe, or cannot complete."""


@dataclass(frozen=True, slots=True)
class TraceQueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    returned_rows: int
    truncated: bool
    duration_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "returned_rows": self.returned_rows,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
        }


class SQLiteTraceRepository:
    """Execute bounded, read-only SQL against one Trace Streamer database."""

    _ALLOWED_FIRST_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN"})
    _DENIED_ACTIONS = frozenset(
        {
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_CREATE_VTABLE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_DROP_VTABLE,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_PRAGMA,
            sqlite3.SQLITE_TRANSACTION,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_ANALYZE,
            sqlite3.SQLITE_SAVEPOINT,
        }
    )
    _DENIED_FUNCTIONS = frozenset(
        {"load_extension", "readfile", "writefile"}
    )
    _FIRST_KEYWORD = re.compile(r"^\s*([A-Za-z]+)")

    def __init__(
        self,
        database_path: Path,
        *,
        query_timeout_seconds: float = 5,
        default_max_rows: int = 100,
        hard_max_rows: int = 500,
        max_result_bytes: int = 256 * 1024,
        max_sql_chars: int = 32_000,
        max_parameters: int = 100,
    ) -> None:
        if not database_path.is_file():
            raise FileNotFoundError(f"Trace 数据库不存在：{database_path}")
        if query_timeout_seconds <= 0:
            raise ValueError("SQL 查询超时必须大于 0")
        if not 0 < default_max_rows <= hard_max_rows:
            raise ValueError("默认最大行数必须在硬限制范围内")
        if max_result_bytes <= 0:
            raise ValueError("SQL 结果字节限制必须大于 0")

        self._database_path = database_path.resolve()
        self._query_timeout_seconds = query_timeout_seconds
        self._default_max_rows = default_max_rows
        self._hard_max_rows = hard_max_rows
        self._max_result_bytes = max_result_bytes
        self._max_sql_chars = max_sql_chars
        self._max_parameters = max_parameters

    def query(
        self,
        sql: str,
        parameters: Sequence[str | int | float | bool | None] | None = None,
        *,
        max_rows: int | None = None,
    ) -> TraceQueryResult:
        statement = self._validate_sql(sql)
        values = self._validate_parameters(parameters or [])
        row_limit = self._validate_max_rows(max_rows)
        deadline = monotonic() + self._query_timeout_seconds
        started = perf_counter()

        uri = f"{self._database_path.as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=1) as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.set_authorizer(self._authorize)
                connection.set_progress_handler(
                    lambda: int(monotonic() >= deadline),
                    10_000,
                )
                cursor = connection.execute(statement, values)
                columns = self._unique_column_names(cursor.description)
                raw_rows = cursor.fetchmany(row_limit + 1)
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise TraceQueryError(
                    "Trace SQL 查询超过执行时间限制"
                ) from exc
            raise TraceQueryError(f"Trace SQL 查询失败：{exc}") from exc

        truncated = len(raw_rows) > row_limit
        rows = self._bounded_rows(
            columns,
            raw_rows[:row_limit],
        )
        if len(rows) < min(len(raw_rows), row_limit):
            truncated = True

        return TraceQueryResult(
            columns=columns,
            rows=rows,
            returned_rows=len(rows),
            truncated=truncated,
            duration_ms=(perf_counter() - started) * 1000,
        )

    def _validate_sql(self, sql: str) -> str:
        if not isinstance(sql, str) or not sql.strip():
            raise TraceQueryError("sql 必须是非空字符串")
        if len(sql) > self._max_sql_chars:
            raise TraceQueryError(
                f"SQL 长度超过限制：{self._max_sql_chars} 字符"
            )
        match = self._FIRST_KEYWORD.match(sql)
        if match is None:
            raise TraceQueryError("无法识别 SQL 语句类型")
        keyword = match.group(1).upper()
        if keyword not in self._ALLOWED_FIRST_KEYWORDS:
            allowed = ", ".join(sorted(self._ALLOWED_FIRST_KEYWORDS))
            raise TraceQueryError(f"只允许只读查询：{allowed}")
        return sql.strip()

    def _validate_parameters(
        self,
        parameters: Sequence[str | int | float | bool | None],
    ) -> tuple[str | int | float | bool | None, ...]:
        if isinstance(parameters, (str, bytes, bytearray)):
            raise TraceQueryError("parameters 必须是参数数组")
        if len(parameters) > self._max_parameters:
            raise TraceQueryError(
                f"SQL 参数数量超过限制：{self._max_parameters}"
            )
        values: list[str | int | float | bool | None] = []
        for value in parameters:
            if value is not None and not isinstance(
                value,
                (str, int, float, bool),
            ):
                raise TraceQueryError(
                    "SQL 参数只允许字符串、数字、布尔值或 null"
                )
            values.append(value)
        return tuple(values)

    def _validate_max_rows(self, max_rows: int | None) -> int:
        if max_rows is None:
            return self._default_max_rows
        if isinstance(max_rows, bool) or not isinstance(max_rows, int):
            raise TraceQueryError("max_rows 必须是整数")
        if not 1 <= max_rows <= self._hard_max_rows:
            raise TraceQueryError(
                f"max_rows 必须在 1 到 {self._hard_max_rows} 之间"
            )
        return max_rows

    def _authorize(
        self,
        action: int,
        argument1: str | None,
        argument2: str | None,
        database: str | None,
        trigger: str | None,
    ) -> int:
        del database, trigger
        if action in self._DENIED_ACTIONS:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            function_name = (argument2 or argument1 or "").lower()
            if function_name in self._DENIED_FUNCTIONS:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @staticmethod
    def _unique_column_names(
        description: Sequence[Sequence[Any]] | None,
    ) -> list[str]:
        if not description:
            return []
        counts: dict[str, int] = {}
        columns: list[str] = []
        for item in description:
            original = str(item[0])
            counts[original] = counts.get(original, 0) + 1
            occurrence = counts[original]
            columns.append(
                original if occurrence == 1 else f"{original}_{occurrence}"
            )
        return columns

    def _bounded_rows(
        self,
        columns: list[str],
        raw_rows: Sequence[Sequence[Any]],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        total_bytes = 0
        for raw_row in raw_rows:
            row = {
                column: self._json_value(value)
                for column, value in zip(columns, raw_row, strict=True)
            }
            encoded_size = len(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            if total_bytes + encoded_size > self._max_result_bytes:
                break
            rows.append(row)
            total_bytes += encoded_size
        return rows

    @staticmethod
    def _json_value(value: Any) -> Any:
        if isinstance(value, bytes):
            preview = value[:64]
            return {
                "type": "bytes",
                "hex": preview.hex(),
                "size_bytes": len(value),
                "truncated": len(preview) < len(value),
            }
        if value is None or isinstance(value, (str, int, float)):
            return value
        return str(value)
