"""Corpus backup (P4): one command, three storage shapes.

A backup is only worth what its restore path is, so each adapter is snapshotted
in the form that adapter can actually be restored from:

SQLite
    ``VACUUM INTO``, a transaction-consistent copy in a single file. Copying
    ``memory.sqlite3`` with the filesystem instead would race the WAL and could
    produce a file that needs recovery before it can be opened.
JSON
    A copy of the namespace's directory tree. There is no transaction to be
    consistent with, so the copy is per file and the manifest records exactly
    what was captured.
PostgreSQL
    The logical bundle from :mod:`trace_agent.memory.migrate`. This process has
    no access to the server's data directory and should not pretend to; a
    physical dump is ``pg_dump``'s job. What can honestly be produced here is a
    store-agnostic snapshot that ``memory import`` can restore anywhere.

Every backup writes a manifest beside its artifacts holding counts, checksums and
the store's ``stats()`` at the time. That is what makes a restore verifiable:
"the backup holds the same number of records the corpus had" is a check an
operator can run, whereas a directory of files with no inventory is a hope.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from trace_agent.memory.migrate import export_bundle, write_export
from trace_agent.models import utc_now

# Artifact kinds named in the manifest. A restore reads these to decide whether
# it is copying a database file, copying a tree, or importing a bundle.
KIND_SQLITE_SNAPSHOT = "sqlite-snapshot"
KIND_FILE_TREE = "file-tree"
KIND_BUNDLE = "bundle"

_CHUNK = 1024 * 1024

# Directory names whose ``*.json`` files are corpus records, used to count what a
# file-tree backup captured without parsing any of it.
_RECORD_DIRS = frozenset({"curated", "episodes", "sessions"})

# Adapters whose corpus lives on a server rather than on this disk. They still
# have a local ``root`` - the dream queue is a file on every adapter, and a
# degraded run falls back to files - so probing for ``root`` alone would copy
# that directory and label it a corpus snapshot while holding none of the corpus.
# A backup that lies is worse than no backup: it is the thing an operator
# trusts when everything else has already gone wrong.
_REMOTE_KINDS = frozenset({"postgres"})


@dataclass(frozen=True, slots=True)
class BackupArtifact:
    """One thing a backup produced, described well enough to restore from."""

    path: Path
    kind: str
    bytes: int = 0
    files: int = 1
    sha256: str = ""
    records: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


@dataclass(frozen=True, slots=True)
class BackupReport:
    """What was backed up, where, and anything the operator should know."""

    destination: Path
    store_kind: str
    namespace: str
    created_at: str
    artifacts: tuple[BackupArtifact, ...] = ()
    manifest: Path | None = None
    stats: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "destination": str(self.destination),
            "store_kind": self.store_kind,
            "namespace": self.namespace,
            "created_at": self.created_at,
            "artifacts": [item.as_dict() for item in self.artifacts],
            "manifest": str(self.manifest) if self.manifest else None,
            "stats": self.stats,
            "notes": list(self.notes),
        }


def backup_corpus(
    store: Any,
    destination: Path | str,
    *,
    include_bundle: bool = False,
) -> BackupReport:
    """Snapshot one namespace's corpus into ``destination``.

    ``include_bundle`` adds the logical export beside the physical artifact. It
    is the only way to move a SQLite corpus into a different adapter, and it is
    off by default because it re-serialises every record the physical snapshot
    already captured.

    Raises ``OSError`` when nothing at all could be written: a backup that
    silently produced nothing is worse than one that fails loudly.
    """
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    created_at = utc_now()
    kind = str(getattr(store, "store_kind", "unknown"))
    namespace = str(getattr(store, "namespace", ""))
    stamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    label = _safe_label(namespace) or "corpus"

    artifacts: list[BackupArtifact] = []
    notes: list[str] = []

    # Every artifact of one backup shares a stem, and its disambiguating counter
    # is resolved once up front. Two backups inside the same second must not
    # share an artifact name (``VACUUM INTO`` and ``copytree`` both refuse to
    # overwrite) nor a manifest name: the second would erase the only inventory
    # describing the first, leaving an artifact nobody can verify.
    stem = f"memory-{label}-{stamp}"
    base = _unique_base(target, stem)
    counter = base[len(stem) :]

    physical = _physical_snapshot(store, target, base=base)
    if physical is not None:
        artifacts.append(physical)
        if physical.kind == KIND_SQLITE_SNAPSHOT:
            # The one artifact whose scope is wider than the namespace the
            # report is about. ``VACUUM INTO`` copies a database file, and one
            # SQLite file holds every namespace, so an operator comparing
            # ``records`` with ``stats`` would otherwise see two different
            # numbers for the same backup and reasonably suspect it is broken.
            notes.append(
                "SQLite 快照是整库副本，包含同一数据库内其他命名空间的记录；"
                f"产物 records={physical.records} 为全库计数，"
                "manifest.stats 为当前命名空间计数"
            )
    else:
        notes.append(
            f"{kind} 存储没有可复制的本地文件，已改为导出逻辑快照"
            "（可用 memory import 恢复）"
        )

    if include_bundle or not artifacts:
        bundle_path = _unique(target / f"memory-bundle-{label}-{stamp}{counter}.json")
        write_export(store, bundle_path)
        bundle = export_bundle(store)
        artifacts.append(
            BackupArtifact(
                path=bundle_path,
                kind=KIND_BUNDLE,
                bytes=bundle_path.stat().st_size,
                sha256=_sha256(bundle_path),
                records=(
                    len(bundle.get("memories") or [])
                    + len(bundle.get("episodes") or [])
                    + len(bundle.get("sessions") or [])
                ),
            )
        )

    report = BackupReport(
        destination=target,
        store_kind=kind,
        namespace=namespace,
        created_at=created_at.isoformat(),
        artifacts=tuple(artifacts),
        stats=_stats_dict(store),
        notes=tuple(notes),
    )
    manifest = _unique(target / f"manifest-{label}-{stamp}{counter}.json")
    manifest.write_text(
        json.dumps(report.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # The manifest path is only known after it is written, so the returned
    # report carries it while the file on disk does not mention itself.
    return BackupReport(
        destination=report.destination,
        store_kind=report.store_kind,
        namespace=report.namespace,
        created_at=report.created_at,
        artifacts=report.artifacts,
        manifest=manifest,
        stats=report.stats,
        notes=report.notes,
    )


def _physical_snapshot(
    store: Any, target: Path, *, base: str
) -> BackupArtifact | None:
    """The adapter-specific copy, or ``None`` when the corpus is not on this disk."""
    if str(getattr(store, "store_kind", "")) in _REMOTE_KINDS:
        return None
    db_path = getattr(store, "db_path", None)
    if db_path is not None and Path(db_path).is_file():
        return _sqlite_snapshot(Path(db_path), target / f"{base}.sqlite3")
    root = getattr(store, "root", None)
    if root is not None and Path(root).is_dir():
        return _copy_tree(Path(root), target / base)
    return None


def _sqlite_snapshot(
    db_path: Path, destination: Path
) -> BackupArtifact:
    """``VACUUM INTO`` a consistent single-file copy of the corpus.

    A separate connection rather than the store's own: the snapshot has to be
    taken while the dashboard may hold the corpus open, and ``VACUUM INTO`` on a
    WAL database already includes the committed WAL frames.
    """
    connection = sqlite3.connect(db_path, timeout=30.0)
    try:
        connection.execute("VACUUM INTO ?", (str(destination),))
    finally:
        connection.close()
    return BackupArtifact(
        path=destination,
        kind=KIND_SQLITE_SNAPSHOT,
        bytes=destination.stat().st_size,
        sha256=_sha256(destination),
        records=_sqlite_record_count(destination),
    )


def _sqlite_record_count(path: Path) -> int:
    """Records in a snapshot, read back so the manifest states a verified count.

    Whole-database rather than per namespace: the snapshot is a copy of the file,
    and one file holds every namespace. The caller records that in ``notes`` so
    the number is not mistaken for the backed-up namespace's own size.
    """
    connection = sqlite3.connect(path, timeout=10.0)
    try:
        total = 0
        for table in ("memories", "episodes", "sessions"):
            row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            total += int(row[0]) if row else 0
        return total
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _copy_tree(root: Path, destination: Path) -> BackupArtifact:
    """Copy one namespace's record directory.

    ``root`` is already the namespace's own directory - ``JsonStore`` resolves
    ``<base>/ns/<namespace>`` itself - so the copy holds exactly one domain's
    ``curated/`` ``episodes/`` ``sessions/`` trees and nothing from its
    neighbours. Restoring means pointing a store's root back at it.
    """
    shutil.copytree(root, destination)
    files = [path for path in destination.rglob("*") if path.is_file()]
    return BackupArtifact(
        path=destination,
        kind=KIND_FILE_TREE,
        bytes=sum(path.stat().st_size for path in files),
        files=len(files),
        sha256=_tree_digest(files, destination),
        records=sum(
            1
            for path in files
            if path.suffix == ".json" and path.parent.name in _RECORD_DIRS
        ),
    )


def _tree_digest(files: list[Path], root: Path) -> str:
    """One checksum for a whole tree, so a restore can be verified as a unit."""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: str(item)):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(_sha256(path).encode("utf-8"))
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_CHUNK), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _unique(path: Path) -> Path:
    """A path that does not exist yet.

    ``VACUUM INTO`` refuses to overwrite and ``copytree`` refuses an existing
    directory, and two backups inside the same second is a real possibility for
    a scripted operator, so the name gains a counter instead of failing.
    """
    if not path.exists():
        return path
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{utc_now().timestamp()}{suffix}")


def _unique_base(directory: Path, base: str) -> str:
    """A stem no existing artifact in ``directory`` starts with.

    Extension-agnostic on purpose: the same stem names a ``.sqlite3`` snapshot,
    a directory tree and a manifest, and all of them have to belong to the same
    backup for the manifest to describe what sits beside it.
    """
    existing = [path.name for path in directory.iterdir()] if directory.is_dir() else []
    if not any(name.startswith(base) for name in existing):
        return base
    for index in range(2, 1000):
        candidate = f"{base}-{index}"
        if not any(name.startswith(candidate) for name in existing):
            return candidate
    return f"{base}-{utc_now().timestamp()}"


def _safe_label(namespace: str) -> str:
    """Filesystem-safe namespace label for artifact names."""
    return "".join(
        char if char.isalnum() or char in "-_." else "_" for char in namespace
    ).strip("_")


def _stats_dict(store: Any) -> dict[str, Any]:
    try:
        summary = store.stats()
    except Exception:  # noqa: BLE001 - a backup must not fail on diagnostics
        return {}
    if hasattr(summary, "__dataclass_fields__"):
        return asdict(summary)
    if hasattr(summary, "model_dump"):
        return summary.model_dump(mode="json")
    return dict(summary)


__all__ = [
    "BackupArtifact",
    "BackupReport",
    "KIND_BUNDLE",
    "KIND_FILE_TREE",
    "KIND_SQLITE_SNAPSHOT",
    "backup_corpus",
]
