from __future__ import annotations

import locale
import os
import platform
import sqlite3
import stat
import subprocess
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Protocol, Sequence

from trace_agent.database.trace_indexes import ensure_trace_indexes
from trace_agent.models import (
    TraceCapability,
    TraceConversionRecord,
)


class TraceStreamerError(RuntimeError):
    """Raised when the bundled Trace Streamer cannot produce a valid DB."""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    return_code: int
    stdout: bytes
    stderr: bytes


class ProcessRunner(Protocol):
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        """Run a process without a shell and capture byte output."""


class SubprocessRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            if os.name == "nt"
            else 0
        )
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
            shell=False,
            creationflags=creation_flags,
        )
        return ProcessResult(
            return_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class TraceStreamerLocator:
    """Resolve only the explicit override or project-bundled executable."""

    _TARGETS: dict[tuple[str, str], tuple[str, str]] = {
        ("windows", "x86_64"): (
            "windows-x86_64",
            "trace_streamer.exe",
        ),
        ("linux", "x86_64"): (
            "linux-x86_64",
            "trace_streamer",
        ),
        ("darwin", "aarch64"): (
            "darwin-aarch64",
            "trace_streamer",
        ),
    }

    def __init__(
        self,
        *,
        explicit_path: Path | None = None,
        bundle_roots: Sequence[Path] | None = None,
        system: str | None = None,
        machine: str | None = None,
    ) -> None:
        self._explicit_path = explicit_path
        self._system = (system or platform.system()).lower()
        self._machine = self._detect_machine(machine)
        self._bundle_roots = list(
            bundle_roots or self._default_bundle_roots()
        )

    def resolve(self) -> Path:
        if self._explicit_path is not None:
            explicit = self._explicit_path.expanduser().resolve()
            if not explicit.is_file():
                raise FileNotFoundError(
                    f"指定的 trace_streamer 不存在：{explicit}"
                )
            return self._ensure_executable(explicit)

        target = self._TARGETS.get((self._system, self._machine))
        if target is None:
            supported = ", ".join(
                f"{system}-{machine}"
                for system, machine in sorted(self._TARGETS)
            )
            raise TraceStreamerError(
                "工程内没有适用于当前平台的 trace_streamer："
                f"{self._system}-{self._machine}。"
                f"内置平台：{supported}；可使用 --trace-streamer 覆盖。"
            )

        directory, filename = target
        candidates = [
            root / directory / filename
            for root in self._bundle_roots
        ]
        for candidate in candidates:
            if candidate.is_file():
                return self._ensure_executable(candidate.resolve())

        searched = "\n".join(str(path) for path in candidates)
        raise FileNotFoundError(
            "工程内缺少 trace_streamer 二进制，已检查：\n"
            f"{searched}"
        )

    @staticmethod
    def _normalize_machine(machine: str) -> str:
        value = machine.lower()
        if value in {"amd64", "x64", "x86_64"}:
            return "x86_64"
        if value in {"arm64", "aarch64"}:
            return "aarch64"
        return value

    @classmethod
    def _detect_machine(cls, machine: str | None) -> str:
        detected = machine if machine is not None else platform.machine()
        if detected:
            return cls._normalize_machine(detected)

        platform_tag = sysconfig.get_platform().lower()
        if any(
            alias in platform_tag
            for alias in ("amd64", "x86_64", "x64")
        ):
            return "x86_64"
        if any(
            alias in platform_tag
            for alias in ("arm64", "aarch64")
        ):
            return "aarch64"
        return ""

    @staticmethod
    def _default_bundle_roots() -> list[Path]:
        source_root = (
            Path(__file__).resolve().parents[3]
            / "vendor"
            / "trace_streamer"
        )
        installed_root = (
            Path(__file__).resolve().parents[1]
            / "runtime_tools"
            / "trace_streamer"
        )
        return [source_root, installed_root]

    @staticmethod
    def _ensure_executable(path: Path) -> Path:
        if os.name == "nt" or os.access(path, os.X_OK):
            return path
        try:
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        except OSError as exc:
            raise TraceStreamerError(
                f"trace_streamer 没有执行权限且无法修正：{path}"
            ) from exc
        return path


class TraceDatabaseInspector:
    _CAPABILITY_TABLES: dict[TraceCapability, frozenset[str]] = {
        TraceCapability.PROCESSES: frozenset({"process"}),
        TraceCapability.THREADS: frozenset({"thread"}),
        TraceCapability.SLICES: frozenset({"callstack", "slice"}),
        TraceCapability.CPU_SCHEDULING: frozenset(
            {"sched_slice", "thread_state"}
        ),
        TraceCapability.IO_EVENTS: frozenset(
            {
                "diskio",
                "file_system_sample",
                "bio_latency_sample",
                "syscall",
            }
        ),
        TraceCapability.FRAME_EVENTS: frozenset(
            {
                "frame_slice",
                "actual_frame_timeline_slice",
                "expected_frame_timeline_slice",
            }
        ),
        TraceCapability.MARKERS: frozenset(
            {"instant", "app_startup", "task_pool", "callstack"}
        ),
    }
    _ROW_CAPABILITY_TABLES: dict[TraceCapability, str] = {
        TraceCapability.APP_STARTUP_STAGES: "app_startup",
        TraceCapability.PERF_SAMPLES: "perf_sample",
    }

    def inspect(self, database_path: Path) -> list[TraceCapability]:
        try:
            uri = f"{database_path.resolve().as_uri()}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table', 'view')"
                ).fetchall()
                tables = {str(row[0]).lower() for row in rows}
                row_capabilities = {
                    capability
                    for capability, table in (
                        self._ROW_CAPABILITY_TABLES.items()
                    )
                    if table in tables
                    and connection.execute(
                        f'SELECT EXISTS('
                        f'SELECT 1 FROM "{table}" LIMIT 1'
                        f')'
                    ).fetchone()[0]
                    == 1
                }
        except sqlite3.DatabaseError as exc:
            raise TraceStreamerError(
                f"trace_streamer 输出不是有效 SQLite DB：{database_path}"
            ) from exc

        capabilities = {
            TraceCapability.FILE_METADATA,
            TraceCapability.TRACE_DATABASE,
        }
        for capability, candidates in self._CAPABILITY_TABLES.items():
            if tables & candidates:
                capabilities.add(capability)
        capabilities.update(row_capabilities)
        return sorted(capabilities, key=lambda item: item.value)


class TraceStreamerConverter:
    def __init__(
        self,
        executable: Path,
        *,
        timeout_seconds: float = 600,
        runner: ProcessRunner | None = None,
        inspector: TraceDatabaseInspector | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("trace_streamer 超时必须大于 0")
        self._executable = executable.resolve()
        self._timeout_seconds = timeout_seconds
        self._runner = runner or SubprocessRunner()
        self._inspector = inspector or TraceDatabaseInspector()

    @property
    def executable(self) -> Path:
        return self._executable

    def version(self, workspace: Path) -> str | None:
        try:
            result = self._runner.run(
                [str(self._executable), "-v"],
                cwd=workspace,
                timeout_seconds=min(self._timeout_seconds, 10),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        output = self._decode(result.stdout or result.stderr).strip()
        return output or None

    def convert(
        self,
        *,
        trace_path: Path,
        database_path: Path,
        workspace: Path,
        role: str,
        version: str | None,
    ) -> tuple[TraceConversionRecord, list[TraceCapability]]:
        workspace.mkdir(parents=True, exist_ok=True)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path = workspace / f"{role}-trace-streamer.stdout.log"
        stderr_path = workspace / f"{role}-trace-streamer.stderr.log"
        command = [
            str(self._executable),
            str(trace_path.resolve()),
            "-e",
            str(database_path.resolve()),
        ]

        started = perf_counter()
        try:
            result = self._runner.run(
                command,
                cwd=workspace,
                timeout_seconds=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise TraceStreamerError(
                f"trace_streamer 转换超时（{self._timeout_seconds}s）："
                f"{trace_path}"
            ) from exc
        except OSError as exc:
            raise TraceStreamerError(
                f"无法启动 trace_streamer：{self._executable}"
            ) from exc
        duration_ms = (perf_counter() - started) * 1000

        stdout_path.write_text(
            self._decode(result.stdout),
            encoding="utf-8",
        )
        stderr_path.write_text(
            self._decode(result.stderr),
            encoding="utf-8",
        )

        record = TraceConversionRecord(
            role=role,
            executable=str(self._executable),
            version=version,
            command=command,
            database_path=str(database_path.resolve()),
            stdout_path=str(stdout_path.resolve()),
            stderr_path=str(stderr_path.resolve()),
            duration_ms=duration_ms,
            return_code=result.return_code,
        )
        if result.return_code != 0:
            raise TraceStreamerError(
                f"trace_streamer 转换失败，退出码 {result.return_code}；"
                f"日志：{stderr_path}"
            )
        if not database_path.is_file() or database_path.stat().st_size == 0:
            raise TraceStreamerError(
                f"trace_streamer 未生成有效 DB：{database_path}"
            )

        # TraceStreamer 导出不建索引；在缓存发布前补建辅助索引，
        # 避免仓库层查询对大表全表扫描触发 5 秒查询超时。
        try:
            ensure_trace_indexes(database_path)
        except (OSError, sqlite3.Error) as exc:
            raise TraceStreamerError(
                f"为 Trace DB 建立辅助索引失败：{database_path}：{exc}"
            ) from exc

        capabilities = self._inspector.inspect(database_path)
        return record, capabilities

    @staticmethod
    def _decode(data: bytes) -> str:
        if not data:
            return ""
        encodings = ["utf-8", locale.getpreferredencoding(False)]
        for encoding in dict.fromkeys(encodings):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")
