from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from trace_agent.models import (
    TraceCapability,
    TraceConversionRecord,
)
from trace_agent.trace.trace_streamer import (
    TraceDatabaseInspector,
    TraceStreamerError,
)


_CACHE_FORMAT_VERSION = 1
_HASH_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TraceCacheFingerprint:
    key: str
    trace_sha256: str
    trace_size_bytes: int
    converter_sha256: str


@dataclass(frozen=True, slots=True)
class TraceCacheHit:
    record: TraceConversionRecord
    capabilities: list[TraceCapability]


class TraceDatabaseCache:
    """Content-addressed cache for immutable TraceStreamer databases."""

    def __init__(
        self,
        root: Path,
        *,
        refresh: bool = False,
        inspector: TraceDatabaseInspector | None = None,
    ) -> None:
        self._root = root.expanduser().resolve()
        self._refresh = refresh
        self._inspector = inspector or TraceDatabaseInspector()
        self._converter_hashes: dict[tuple[str, int, int], str] = {}

    def fingerprint(
        self,
        trace_path: Path,
        executable: Path,
    ) -> TraceCacheFingerprint:
        trace_path = trace_path.resolve()
        executable = executable.resolve()
        trace_stat = trace_path.stat()
        converter_stat = executable.stat()
        converter_identity = (
            str(executable),
            converter_stat.st_size,
            converter_stat.st_mtime_ns,
        )
        converter_sha256 = self._converter_hashes.get(converter_identity)
        if converter_sha256 is None:
            converter_sha256 = self._sha256(executable)
            self._converter_hashes[converter_identity] = converter_sha256
        trace_sha256 = self._sha256(trace_path)
        payload = {
            "cache_format_version": _CACHE_FORMAT_VERSION,
            "trace_sha256": trace_sha256,
            "trace_size_bytes": trace_stat.st_size,
            "converter_sha256": converter_sha256,
            "conversion_arguments": ["-e", "sqlite"],
        }
        key = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return TraceCacheFingerprint(
            key=key,
            trace_sha256=trace_sha256,
            trace_size_bytes=trace_stat.st_size,
            converter_sha256=converter_sha256,
        )

    def restore(
        self,
        fingerprint: TraceCacheFingerprint,
        *,
        trace_path: Path,
        executable: Path,
        version: str | None,
        database_path: Path,
        workspace: Path,
        role: str,
    ) -> TraceCacheHit | None:
        if self._refresh:
            return None
        started = perf_counter()
        entry = self._entry(fingerprint.key)
        cached_database = entry / "trace.db"
        metadata_path = entry / "metadata.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not self._metadata_matches(
                metadata,
                fingerprint,
                cached_database,
            ):
                return None
            capabilities = self._inspector.inspect(cached_database)
        except (
            OSError,
            ValueError,
            json.JSONDecodeError,
            TraceStreamerError,
        ):
            return None

        materialization = self._materialize(
            cached_database,
            database_path,
        )
        stdout_path = workspace / f"{role}-trace-streamer.stdout.log"
        stderr_path = workspace / f"{role}-trace-streamer.stderr.log"
        stdout_path.write_text(
            "Trace DB cache hit; TraceStreamer conversion skipped.\n",
            encoding="utf-8",
        )
        stderr_path.write_text("", encoding="utf-8")
        command = [
            str(executable.resolve()),
            str(trace_path.resolve()),
            "-e",
            str(database_path.resolve()),
        ]
        return TraceCacheHit(
            record=TraceConversionRecord(
                role=role,
                executable=str(executable.resolve()),
                version=version,
                command=command,
                database_path=str(database_path.resolve()),
                stdout_path=str(stdout_path.resolve()),
                stderr_path=str(stderr_path.resolve()),
                duration_ms=(perf_counter() - started) * 1000,
                return_code=0,
                cache_hit=True,
                cache_key=fingerprint.key,
                source_database_path=str(cached_database.resolve()),
                materialization=materialization,
            ),
            capabilities=capabilities,
        )

    def publish(
        self,
        fingerprint: TraceCacheFingerprint,
        *,
        database_path: Path,
        trace_path: Path,
        executable: Path,
        version: str | None,
        capabilities: list[TraceCapability],
    ) -> Path:
        entry = self._entry(fingerprint.key)
        entry.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        temporary_database = entry / f"trace.db.{token}.tmp"
        temporary_metadata = entry / f"metadata.json.{token}.tmp"
        cached_database = entry / "trace.db"
        metadata_path = entry / "metadata.json"
        try:
            self._link_or_copy(database_path, temporary_database)
            metadata = {
                "cache_format_version": _CACHE_FORMAT_VERSION,
                "key": fingerprint.key,
                "trace_sha256": fingerprint.trace_sha256,
                "trace_size_bytes": fingerprint.trace_size_bytes,
                "trace_path_at_creation": str(trace_path.resolve()),
                "converter_sha256": fingerprint.converter_sha256,
                "converter_path_at_creation": str(executable.resolve()),
                "converter_version": version,
                "database_size_bytes": temporary_database.stat().st_size,
                "capabilities": [item.value for item in capabilities],
            }
            temporary_metadata.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_database, cached_database)
            os.replace(temporary_metadata, metadata_path)
        finally:
            for temporary in (temporary_database, temporary_metadata):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return cached_database

    def _entry(self, key: str) -> Path:
        return self._root / f"v{_CACHE_FORMAT_VERSION}" / key

    @staticmethod
    def _metadata_matches(
        metadata: object,
        fingerprint: TraceCacheFingerprint,
        database_path: Path,
    ) -> bool:
        if not isinstance(metadata, dict):
            return False
        if not database_path.is_file() or database_path.stat().st_size == 0:
            return False
        return (
            metadata.get("cache_format_version") == _CACHE_FORMAT_VERSION
            and metadata.get("key") == fingerprint.key
            and metadata.get("trace_sha256") == fingerprint.trace_sha256
            and metadata.get("trace_size_bytes")
            == fingerprint.trace_size_bytes
            and metadata.get("converter_sha256")
            == fingerprint.converter_sha256
            and metadata.get("database_size_bytes")
            == database_path.stat().st_size
        )

    @staticmethod
    def _materialize(source: Path, destination: Path) -> str:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.unlink(missing_ok=True)
        try:
            os.link(source, destination)
            return "hardlink"
        except OSError:
            shutil.copy2(source, destination)
            return "copy"

    @staticmethod
    def _link_or_copy(source: Path, destination: Path) -> None:
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()
