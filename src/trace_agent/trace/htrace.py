from __future__ import annotations

from pathlib import Path
from time import perf_counter

from trace_agent.models import (
    AnalyzeRequest,
    TraceCapability,
    TraceConversionRecord,
    TraceHandle,
)
from trace_agent.trace.cache import TraceDatabaseCache
from trace_agent.trace.trace_streamer import (
    ProcessRunner,
    TraceStreamerConverter,
    TraceStreamerLocator,
)


class HTraceAdapter:
    """Convert HTrace inputs into run-local, read-only SQLite databases."""

    def __init__(
        self,
        *,
        trace_streamer_path: Path | None = None,
        timeout_seconds: float = 600,
        locator: TraceStreamerLocator | None = None,
        runner: ProcessRunner | None = None,
        cache_dir: Path | None = None,
        refresh_cache: bool = False,
    ) -> None:
        self._locator = locator or TraceStreamerLocator(
            explicit_path=trace_streamer_path
        )
        self._timeout_seconds = timeout_seconds
        self._runner = runner
        self._cache_dir = (
            cache_dir.expanduser().resolve()
            if cache_dir is not None
            else None
        )
        self._refresh_cache = refresh_cache

    async def prepare(
        self,
        request: AnalyzeRequest,
        workspace: Path,
    ) -> TraceHandle:
        trace_path = request.trace_path.resolve()
        if not trace_path.is_file():
            raise FileNotFoundError(f"Trace 文件不存在：{trace_path}")

        baseline_path = None
        baseline_size = None
        if request.baseline_trace_path is not None:
            baseline_path = request.baseline_trace_path.resolve()
            if not baseline_path.is_file():
                raise FileNotFoundError(f"基线 Trace 文件不存在：{baseline_path}")
            baseline_size = baseline_path.stat().st_size

        workspace.mkdir(parents=True, exist_ok=True)
        executable = self._locator.resolve()
        converter = TraceStreamerConverter(
            executable,
            timeout_seconds=self._timeout_seconds,
            runner=self._runner,
        )
        version = converter.version(workspace)
        cache = (
            TraceDatabaseCache(
                self._cache_dir,
                refresh=self._refresh_cache,
            )
            if self._cache_dir is not None
            else None
        )

        current_database = workspace / "current.db"
        current_conversion, capabilities = self._convert(
            converter=converter,
            cache=cache,
            trace_path=trace_path,
            database_path=current_database,
            workspace=workspace,
            role="current",
            version=version,
        )

        conversions = [current_conversion]
        baseline_database = None
        baseline_capabilities: list[TraceCapability] = []
        if baseline_path is not None:
            baseline_database = workspace / "baseline.db"
            baseline_conversion, baseline_capabilities = self._convert(
                converter=converter,
                cache=cache,
                trace_path=baseline_path,
                database_path=baseline_database,
                workspace=workspace,
                role="baseline",
                version=version,
            )
            conversions.append(baseline_conversion)
            capabilities = sorted(
                {
                    *capabilities,
                    TraceCapability.BASELINE_METADATA,
                },
                key=lambda item: item.value,
            )

        return TraceHandle(
            trace_id=request.trace_id,
            trace_path=trace_path,
            format=trace_path.suffix.lower().lstrip(".") or "unknown",
            size_bytes=trace_path.stat().st_size,
            database_path=current_database,
            baseline_database_path=baseline_database,
            capabilities=capabilities,
            baseline_capabilities=baseline_capabilities,
            baseline_trace_path=baseline_path,
            baseline_size_bytes=baseline_size,
            conversions=conversions,
        )

    @staticmethod
    def _convert(
        *,
        converter: TraceStreamerConverter,
        cache: TraceDatabaseCache | None,
        trace_path: Path,
        database_path: Path,
        workspace: Path,
        role: str,
        version: str | None,
    ) -> tuple[TraceConversionRecord, list[TraceCapability]]:
        fingerprint = None
        if cache is not None:
            lookup_started = perf_counter()
            fingerprint = cache.fingerprint(
                trace_path,
                converter.executable,
            )
            hit = cache.restore(
                fingerprint,
                trace_path=trace_path,
                executable=converter.executable,
                version=version,
                database_path=database_path,
                workspace=workspace,
                role=role,
            )
            if hit is not None:
                record = hit.record.model_copy(
                    update={
                        "duration_ms": (
                            perf_counter() - lookup_started
                        )
                        * 1000
                    }
                )
                return record, hit.capabilities

        record, capabilities = converter.convert(
            trace_path=trace_path,
            database_path=database_path,
            workspace=workspace,
            role=role,
            version=version,
        )
        if cache is not None and fingerprint is not None:
            cached_database = cache.publish(
                fingerprint,
                database_path=database_path,
                trace_path=trace_path,
                executable=converter.executable,
                version=version,
                capabilities=capabilities,
            )
            record = record.model_copy(
                update={
                    "cache_key": fingerprint.key,
                    "source_database_path": str(
                        cached_database.resolve()
                    ),
                }
            )
        return record, capabilities
