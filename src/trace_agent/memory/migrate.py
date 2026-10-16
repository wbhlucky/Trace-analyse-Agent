"""Corpus migration between ``MemoryStore`` adapters.

Section 12 of the design doc requires the JSON -> SQLite move to be idempotent,
backed up, and verifiable. Those three properties are the whole reason this is a
module rather than a script:

* **Idempotent** - every write is an upsert keyed on ``(namespace, id)``, so a
  migration interrupted halfway can simply be re-run. ``memory_sources`` is
  derived from the record's ``run:`` provenance rather than copied, which means
  re-running cannot inflate ``distinct_runs`` (the section-1.2 defect-A shape).
* **Backed up** - unless explicitly disabled, the source corpus is exported to a
  timestamped bundle under ``<root>/backups/`` before anything is written. The
  bundle is a complete, store-agnostic snapshot, so it doubles as the restore
  path and as the offline hand-off format.
* **Verifiable** - after copying, the target's own ``stats()`` counts are
  compared against the number of distinct ids in the bundle. Equality is the
  acceptance check; an honest exception is episode collapse, which happens when
  a legacy corpus holds two projections of one run. That is reported separately
  rather than hidden, because ``UNIQUE(namespace, run_id)`` retiring the stale
  row is the intended behaviour, not data loss.

Governance travels with the records. Bundle version 2 carries ``versions`` (the
trust/content audit trail) and ``links`` (supports/contradicts/duplicate edges)
alongside the corpus, because a migration that copied every memory and dropped
its history would leave the target unable to answer section 7's "who vouched for
this, and when" - and the loss would only surface weeks later, at the moment
somebody needs it. Version-1 bundles still import; the two keys read as empty.

Trust does *not* travel unchanged, and that is section 12.4 rather than an
oversight. A record that reached ``active`` before the quality gate existed got
there by having ``occurrences`` incremented on every write, which is the
section-1.2 defect-A mechanism: one run, re-consolidated, reading as several
independent ones. Copying that status faithfully copies the lie, so a migration
demotes any trusted record that cannot account for its trust - no audit row
showing the transition, and not enough of its own evidence to earn it back.
Everything else is carried verbatim, and ``--keep-trust`` restores a byte-level
copy for the operator who is moving a corpus they have already governed.

Timestamps are preserved on import (``import_curated`` / ``import_session``).
``updated_at`` is a freshness ranking signal, so restamping the corpus during a
migration that is supposed to be transparent would reorder recall afterwards.
"""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trace_agent.memory.lifecycle.quality_gate import (
    DERIVED_DIGEST_KINDS,
    evaluate_trust,
    evidence_for,
)
from trace_agent.memory.models import (
    TRUSTED_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    MemoryStatus,
    SessionMemory,
)
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.store import build_memory_store
from trace_agent.models import utc_now

BACKUP_DIR_NAME = "backups"
# 2 adds the governance section (``versions`` / ``links``). A version-1 bundle
# still imports: the two keys are read with ``.get`` and treated as empty, which
# is the honest reading of a snapshot taken before the audit trail was carried.
BUNDLE_VERSION = 2

# Export caps. Governance rows are one per transition, not one per record, so a
# corpus has few of them; the limit exists to keep a pathological sidecar from
# turning the bundle into an unbounded file, not to trim a healthy one.
_GOVERNANCE_EXPORT_LIMIT = 100_000

# How many unreadable records ``mismatch`` names before it summarises the rest.
# A migration that lost 4 000 of 10 000 rows has one cause, not 4 000, and the
# operator reading the CLI needs the first few ids to go and look, not a wall.
_VERIFY_MISSING_LIMIT = 10

# Recorded in ``memory_versions.actor`` for a demotion, so a trust drop caused
# by the copy is distinguishable from one caused by the gate, the conflict
# detector or a person.
MIGRATE_ACTOR = "migrate"

# The reason string a demoted record carries. It names the section that requires
# it and the status it replaced, because "why is this ``candidate`` now" is
# exactly the question an operator asks of a corpus they did not hand-edit.
DEMOTION_REASON = "migrate: legacy trust not evidenced (section 12.4)"

@dataclass(slots=True)
class MigrationReport:
    """What a migration did, in the shape ``memory migrate`` prints.

    ``errors`` is a list rather than an exception because memory is best-effort
    infrastructure: one unreadable record must not abort a 10k-row migration.
    ``mismatch`` is kept separate from ``errors`` - a mismatch means the copy
    reported success but a record cannot be read back from the target, which is
    the signal an operator has to look at before flipping
    ``TRACE_AGENT_MEMORY_STORE``.
    """

    source_kind: str = ""
    target_kind: str = ""
    namespace: str = ""
    dry_run: bool = False
    episodes: int = 0
    memories: int = 0
    sessions: int = 0
    collapsed_runs: int = 0
    versions: int = 0
    links: int = 0
    #: Trusted records demoted to ``candidate`` on the way in, per section 12.4.
    #: Counted separately from ``memories`` because it is not a copy statistic -
    #: it is a governance decision the operator has to be told about, since it
    #: changes what the corpus will inject on the next run.
    demoted: int = 0
    backup: Path | None = None
    verified: bool = False
    mismatch: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """Whether it is safe to point the runtime at the new store."""
        return not self.errors and (self.dry_run or not self.mismatch)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "target_kind": self.target_kind,
            "namespace": self.namespace,
            "dry_run": self.dry_run,
            "episodes": self.episodes,
            "memories": self.memories,
            "sessions": self.sessions,
            "collapsed_runs": self.collapsed_runs,
            "versions": self.versions,
            "links": self.links,
            "demoted": self.demoted,
            "backup": str(self.backup) if self.backup else None,
            "verified": self.verified,
            "mismatch": list(self.mismatch),
            "errors": list(self.errors),
            "duration_ms": round(self.duration_ms, 1),
            "ok": self.ok,
        }


def export_bundle(store: MemoryStore) -> dict[str, Any]:
    """Serialise one namespace's corpus into a store-agnostic snapshot.

    Records are dumped through their Pydantic models rather than copied off disk
    or out of rows, so the bundle validates on the way back in regardless of
    which adapter produced it.

    The governance section rides along for the same reason the records do: a
    migration that copied memories but dropped their trust history would leave
    the target unable to answer section 7's "who vouched for this, and when".
    ``recent_versions`` returns newest first, so it is reversed into the
    chronological order the SQL adapters assign through ``seq`` and the file
    adapter through append order.
    """
    return {
        "version": BUNDLE_VERSION,
        "namespace": getattr(store, "namespace", ""),
        "store": getattr(store, "store_kind", type(store).__name__),
        "exported_at": utc_now().isoformat(),
        "episodes": [
            item.model_dump(mode="json") for item in store.list_episodes()
        ],
        "memories": [
            item.model_dump(mode="json")
            for item in store.list_curated(active_only=False)
        ],
        "sessions": [item.model_dump(mode="json") for item in store.list_sessions()],
        "versions": list(
            reversed(
                store.recent_versions(limit=_GOVERNANCE_EXPORT_LIMIT)
            )
        ),
        "links": store.list_links(limit=_GOVERNANCE_EXPORT_LIMIT),
    }


def write_export(store: MemoryStore, destination: Path | str) -> Path:
    """Write :func:`export_bundle` to ``destination``, creating parents."""
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    bundle = export_bundle(store)
    path.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def auto_backup(store: MemoryStore, root: Path | str) -> Path | None:
    """Snapshot the corpus before a destructive-ish operation.

    Returns ``None`` instead of raising when the backup cannot be written: a
    read-only mount should not make migration impossible, and the caller decides
    whether a missing backup is fatal.
    """
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    destination = (
        Path(root) / BACKUP_DIR_NAME / f"memory-export-{stamp}.json"
    )
    try:
        return write_export(store, destination)
    except OSError:
        return None


def read_bundle(path: Path | str) -> dict[str, Any]:
    """Load a bundle written by :func:`write_export`."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"not a memory bundle: {path}")
    return payload


def earned_trusts(versions: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Trust levels each record can *show* it moved into, from the audit trail.

    A row counts only when it is a transition: ``from_trust`` present and
    different from ``trust``. Rows that merely restate the state a copy landed
    in are not evidence of anything, and treating them as such would make every
    migration self-certifying - the record's own copy artefact would be the
    reason it keeps a status nobody ever awarded it.
    """
    earned: dict[str, set[str]] = {}
    for row in versions:
        memory_id = str(row.get("memory_id") or "")
        trust = str(row.get("trust") or "")
        previous = row.get("from_trust")
        if not memory_id or not trust or previous is None:
            continue
        if str(previous) == trust:
            continue
        earned.setdefault(memory_id, set()).add(trust)
    return earned


def demote_unexplained_trust(
    memory: CuratedMemory,
    earned: dict[str, set[str]] | None = None,
) -> tuple[CuratedMemory, bool]:
    """Section 12.4: pull an unevidenced legacy trust level back to ``candidate``.

    Returns the record to import and whether it was demoted. A record keeps its
    trust when either of two things can account for it:

    *the audit trail* - a version row shows the transition into that status, so
    a governed corpus migrates verbatim and an expert sign-off survives the move;
    *its own evidence* - re-running the gate's decision from nothing but the
    record's carried provenance, confidence and confirmed runs still reaches
    that status, so the status is one the current rules would award anyway.

    Everything else is the defect-A shape: a single run that reached ``active``
    because ``occurrences`` was incremented per write. Copying that status
    faithfully would copy the lie, and the lie is load-bearing - it decides what
    gets injected as a prior.

    Derived digests are exempt for the same reason the gate exempts them: a
    session or project digest has no "second run" that could corroborate it, so
    demoting one here would strand a record the gate itself keeps ``active``.
    """
    if memory.status not in TRUSTED_STATUSES:
        return memory, False
    if memory.kind in DERIVED_DIGEST_KINDS:
        return memory, False
    if memory.status.value in (earned or {}).get(memory.memory_id, set()):
        return memory, False
    # ``CANDIDATE`` is passed as the current status so the gate's "verification
    # is sticky" short-circuit cannot keep a record verified on the strength of
    # the very status under review.
    verdict, _ = evaluate_trust(
        MemoryStatus.CANDIDATE, evidence_for(memory)
    )
    if verdict is not MemoryStatus.CANDIDATE:
        return memory, False
    demoted = memory.model_copy(
        update={
            "status": MemoryStatus.CANDIDATE,
            "status_reason": f"{DEMOTION_REASON}, was {memory.status.value}",
            "occurrences": memory.distinct_runs,
        }
    )
    return demoted, True


def _import_curated(
    target: MemoryStore,
    memory: CuratedMemory,
    *,
    reason: str = "migrate",
    actor: str | None = MIGRATE_ACTOR,
) -> None:
    """Write a memory without restamping ``updated_at``, if the store can.

    Every row a migration writes is attributed to :data:`MIGRATE_ACTOR`. That is
    not decoration: section 14 scores the corpus on "every trust transition
    carries reason + actor", and the section 12.4 demotion is exactly such a
    transition. Naming the actor is also what lets an operator tell a demotion
    apart from an ordinary copy when both arrive with a ``migrate``-flavoured
    reason, and lets a later ``memory maintain`` sweep re-promote the record
    without anyone having to guess why it fell.
    """
    importer = getattr(target, "import_curated", None)
    if callable(importer):
        importer(memory, reason=reason, actor=actor)
        return
    target.write_curated(memory)


def _import_session(target: MemoryStore, session: SessionMemory) -> None:
    importer = getattr(target, "import_session", None)
    if callable(importer):
        importer(session)
        return
    target.write_session(session)


def _batched(target: MemoryStore) -> Any:
    """One transaction for the whole copy, when the adapter supports it.

    Probed rather than assumed: the JSON adapter has no transactions, and a
    migration that only worked on SQLite would not be much of a migration.
    """
    batcher = getattr(target, "batch", None)
    if callable(batcher):
        return batcher()
    return nullcontext()


def _import_governance(
    target: MemoryStore,
    versions: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> tuple[int, int]:
    """Copy the audit trail and edges, if the target knows how to receive them.

    Probed with ``getattr`` rather than declared on :class:`MemoryStore`: this is
    a migration-only entry point, and widening the Port for it would oblige every
    future adapter to implement a method nothing else calls. A target without it
    reports ``(0, 0)``, which the CLI prints as "no governance copied" instead of
    silently pretending the history arrived.
    """
    importer = getattr(target, "import_governance", None)
    if not callable(importer):
        return 0, 0
    added = importer(versions, links)
    try:
        return int(added[0]), int(added[1])
    except (TypeError, ValueError, IndexError):
        return 0, 0


def import_bundle(
    target: MemoryStore,
    bundle: dict[str, Any],
    *,
    dry_run: bool = False,
    demote_legacy: bool = False,
) -> MigrationReport:
    """Load a bundle into ``target``, counting what would change.

    Records that fail validation are counted in ``errors`` and skipped. The
    alternative - aborting - would leave the target half-migrated because of one
    legacy row, and the bundle still holds the source of truth either way.

    Ordering is load-bearing: the audit trail goes in *before* the records. Each
    adapter's ``_put_curated`` recognises a re-import by checking whether the
    version history it just loaded already ends at the state being written, and
    only records a transition when it does not. Import the memories first and that
    check has nothing to compare against, so every re-run migration appends a
    duplicate tail and the corpus looks edited by the copy that was supposed to
    leave it untouched.

    ``demote_legacy`` is off by default, and that is what keeps ``memory import``
    a restore rather than a governance event: the bundle is the source of truth
    for a snapshot, and an operator putting a backup back expects the backup.
    :func:`migrate_corpus` turns it on, because there the bundle is a *legacy
    corpus* whose trust levels predate the gate. The same ordering argument
    applies - the audit trail has to be in hand before the records are judged
    against it, which is why the demotion reads ``earned`` rather than querying
    the target.
    """
    started = time.perf_counter()
    report = MigrationReport(
        source_kind=str(bundle.get("store", "")),
        target_kind=str(getattr(target, "store_kind", type(target).__name__)),
        namespace=str(getattr(target, "namespace", "")),
        dry_run=dry_run,
    )
    versions = [
        row for row in (bundle.get("versions") or []) if isinstance(row, dict)
    ]
    links = [row for row in (bundle.get("links") or []) if isinstance(row, dict)]
    raw_memories = [
        raw for raw in (bundle.get("memories") or []) if isinstance(raw, dict)
    ]
    earned = earned_trusts(versions) if demote_legacy else {}
    if dry_run:
        report.episodes = len(bundle.get("episodes") or [])
        report.memories = len(raw_memories)
        report.sessions = len(bundle.get("sessions") or [])
        report.collapsed_runs = _collapsed_runs(bundle)
        report.versions = len(versions)
        report.links = len(links)
        if demote_legacy:
            for raw in raw_memories:
                try:
                    memory = CuratedMemory.model_validate(raw)
                except ValueError:
                    continue
                if demote_unexplained_trust(memory, earned)[1]:
                    report.demoted += 1
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    with _batched(target):
        for raw in bundle.get("episodes") or []:
            try:
                target.write_episode(EpisodicMemory.model_validate(raw))
                report.episodes += 1
            except ValueError as exc:
                report.errors.append(f"episode {raw.get('episode_id', '?')}: {exc}")
        report.versions, report.links = _import_governance(target, versions, links)
        for raw in raw_memories:
            try:
                memory = CuratedMemory.model_validate(raw)
                # Guarded by the flag, not just by ``earned`` being empty: with
                # ``demote_legacy`` off this is a restore, and running the gate
                # here anyway would quietly turn ``memory import`` into the
                # governance event it is documented not to be.
                demoted = False
                if demote_legacy:
                    memory, demoted = demote_unexplained_trust(memory, earned)
                _import_curated(
                    target,
                    memory,
                    reason=DEMOTION_REASON if demoted else "migrate",
                )
                report.memories += 1
                report.demoted += int(demoted)
            except ValueError as exc:
                report.errors.append(f"memory {raw.get('memory_id', '?')}: {exc}")
        for raw in bundle.get("sessions") or []:
            try:
                _import_session(target, SessionMemory.model_validate(raw))
                report.sessions += 1
            except ValueError as exc:
                report.errors.append(f"session {raw.get('session_id', '?')}: {exc}")

    report.collapsed_runs = _collapsed_runs(bundle)
    report.duration_ms = (time.perf_counter() - started) * 1000
    return report


def _collapsed_runs(bundle: dict[str, Any]) -> int:
    """How many episodes share a ``run_id`` with another episode.

    ``episodes`` carries ``UNIQUE(namespace, run_id)``, so a legacy corpus with
    two projections of one run lands as a single row. Reporting the difference
    explicitly is what keeps ``verify`` from crying wolf about intended dedup.
    """
    seen: set[str] = set()
    collapsed = 0
    for raw in bundle.get("episodes") or []:
        run_id = str(raw.get("run_id") or "")
        if not run_id:
            continue
        if run_id in seen:
            collapsed += 1
        seen.add(run_id)
    return collapsed


def migrate_corpus(
    *,
    source: MemoryStore,
    target: MemoryStore,
    dry_run: bool = False,
    backup: bool = True,
    verify: bool = True,
    keep_trust: bool = False,
) -> MigrationReport:
    """Copy one namespace from ``source`` to ``target``.

    The bundle round-trip is deliberate even when both stores are in-process:
    going through serialisation and revalidation is what proves the two adapters
    agree on the record schema, which is the property section 3.2 promises when
    it says PostgreSQL is "the same Port, second Adapter".

    ``keep_trust=False`` (the default) applies section 12.4: trusted records that
    cannot account for their trust land as ``candidate`` and have to earn it back
    through the gate. Pass ``True`` for a verbatim copy - the right choice when
    the source is already governed, or when the operator wants the migration to
    change nothing but the storage format.
    """
    if getattr(source, "namespace", "") != getattr(target, "namespace", ""):
        raise ValueError(
            "refusing to migrate across namespaces: "
            f"{getattr(source, 'namespace', '')!r} -> "
            f"{getattr(target, 'namespace', '')!r}"
        )

    report = MigrationReport(
        source_kind=str(getattr(source, "store_kind", type(source).__name__)),
        target_kind=str(getattr(target, "store_kind", type(target).__name__)),
        namespace=str(getattr(source, "namespace", "")),
        dry_run=dry_run,
    )
    started = time.perf_counter()

    if backup and not dry_run:
        report.backup = auto_backup(source, Path(source.root))

    try:
        bundle = export_bundle(source)
    except Exception as exc:  # pragma: no cover - store failure is host specific
        report.errors.append(f"export failed: {exc}")
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    copied = import_bundle(
        target, bundle, dry_run=dry_run, demote_legacy=not keep_trust
    )
    report.episodes = copied.episodes
    report.memories = copied.memories
    report.sessions = copied.sessions
    report.collapsed_runs = copied.collapsed_runs
    report.versions = copied.versions
    report.links = copied.links
    report.demoted = copied.demoted
    report.errors.extend(copied.errors)

    if verify and not dry_run and not copied.errors:
        _verify(target, report, bundle)

    report.duration_ms = (time.perf_counter() - started) * 1000
    return report


def _verify(
    target: MemoryStore, report: MigrationReport, bundle: dict[str, Any]
) -> None:
    """Read every copied record back from the target, by id.

    Counting the target cannot verify an upsert. Section 12.2 requires the
    JSON -> SQLite move to be idempotent and re-runnable, so the ordinary second
    run - and any target that has been in use since P1 - already holds records
    the source does not. There ``target.stats()`` exceeds what this run copied,
    a count comparison reports a mismatch for a migration that succeeded, and
    the CLI turns that mismatch into "do not switch store": the opposite of the
    truth, delivered at the moment an operator is least equipped to argue with
    it. The production corpus hit exactly this - 180 episodes written through
    SQLite since P1, 70 more arriving from the legacy tree, and a verdict of
    ``episodes: target=250 copied=70``.

    Identity is the stronger check as well as the correct one. It catches a
    record that landed under a different id, which counting waves through
    whenever something else is missing to balance it.

    Episodes are matched on ``episode_id`` *or* ``run_id`` because ``episodes``
    carries ``UNIQUE(namespace, run_id)``: a legacy corpus holding two
    projections of one run lands as a single row, and the id that lost is not a
    defect. That is the fact ``collapsed_runs`` reports in aggregate, applied
    per record.

    Trust is deliberately *not* compared. Section 12.4 demotes unevidenced
    legacy trust on the way in, so the target's status is expected to differ
    from the source's; a check that required them to match would report every
    governed migration as lossy. What the source *said* is preserved in the
    demoted record's ``status_reason``, and ``report.demoted`` counts it.
    """
    missing: list[str] = []

    for raw in bundle.get("memories") or []:
        memory_id = str(raw.get("memory_id") or "")
        if not memory_id:
            continue
        try:
            found = target.load_curated(memory_id) is not None
        except Exception as exc:  # pragma: no cover - store failure is host specific
            report.errors.append(f"verify failed on memory {memory_id}: {exc}")
            return
        if not found:
            missing.append(f"memory {memory_id}")

    for raw in bundle.get("sessions") or []:
        session_id = str(raw.get("session_id") or "")
        if not session_id:
            continue
        try:
            found = target.load_session(session_id) is not None
        except Exception as exc:  # pragma: no cover - store failure is host specific
            report.errors.append(f"verify failed on session {session_id}: {exc}")
            return
        if not found:
            missing.append(f"session {session_id}")

    # One listing rather than one point read per episode: the Port has no
    # "load episode by run_id", and answering the collapse case needs the
    # run_id coverage anyway. Migration is an offline command, so reading the
    # target's episodes once is a fair price for an exact verdict.
    episode_ids: set[str] = set()
    run_ids: set[str] = set()
    try:
        for episode in target.list_episodes():
            episode_ids.add(episode.episode_id)
            if episode.run_id:
                run_ids.add(episode.run_id)
    except Exception as exc:  # pragma: no cover - store failure is host specific
        report.errors.append(f"verify failed listing episodes: {exc}")
        return
    for raw in bundle.get("episodes") or []:
        episode_id = str(raw.get("episode_id") or "")
        run_id = str(raw.get("run_id") or "")
        if episode_id in episode_ids or (run_id and run_id in run_ids):
            continue
        missing.append(f"episode {episode_id or run_id or '?'}")

    _verify_governance(target, report, bundle, missing)

    report.verified = True
    report.mismatch.extend(missing[:_VERIFY_MISSING_LIMIT])
    extra = len(missing) - _VERIFY_MISSING_LIMIT
    if extra > 0:
        report.mismatch.append(f"... and {extra} more")


def _verify_governance(
    target: MemoryStore,
    report: MigrationReport,
    bundle: dict[str, Any],
    missing: list[str],
) -> None:
    """Confirm the audit trail and edges landed, not just the records.

    A migration that copied every memory and dropped its trust history still
    passes a record check, and the loss only surfaces later as section 7's "who
    vouched for this" going unanswerable. Edges are verified by identity against
    ``list_links``; versions by a count floor, because the Port exposes them
    newest-first with a limit and cannot answer "is this exact row present".

    Skipped entirely when the target has no ``import_governance``: there is
    nothing to verify against, and reporting a mismatch for a capability the
    adapter never claimed would be the same false alarm the record check above
    was rewritten to avoid.
    """
    if not callable(getattr(target, "import_governance", None)):
        return

    expected_links = {
        (
            str(row.get("memory_id") or ""),
            str(row.get("related_memory_id") or ""),
            str(row.get("relation") or ""),
        )
        for row in (bundle.get("links") or [])
        if isinstance(row, dict)
    }
    # The importer rejects an edge with a missing endpoint or relation, so
    # holding it against the target would flag a row that was never copyable.
    expected_links = {key for key in expected_links if all(key)}
    if expected_links:
        try:
            present = {
                (
                    str(row.get("memory_id") or ""),
                    str(row.get("related_memory_id") or ""),
                    str(row.get("relation") or ""),
                )
                for row in target.list_links(limit=_GOVERNANCE_EXPORT_LIMIT)
            }
        except Exception as exc:  # pragma: no cover - store failure is host specific
            report.errors.append(f"verify failed listing links: {exc}")
            return
        for key in sorted(expected_links - present):
            missing.append(f"link {key[0]}->{key[1]} ({key[2]})")

    expected_versions = sum(
        1
        for row in (bundle.get("versions") or [])
        if isinstance(row, dict) and str(row.get("memory_id") or "")
    )
    if expected_versions:
        try:
            held = int(target.stats().versions)
        except Exception as exc:  # pragma: no cover - store failure is host specific
            report.errors.append(f"verify failed counting versions: {exc}")
            return
        if held < expected_versions:
            missing.append(
                f"versions target={held} exported={expected_versions}"
            )


def migrate_store(
    root: Path | str,
    *,
    source_kind: str | None = None,
    target_kind: str | None = None,
    namespace: str | None = None,
    dry_run: bool = False,
    backup: bool = True,
    verify: bool = True,
    keep_trust: bool = False,
) -> MigrationReport:
    """CLI entry point: migrate one root between two store kinds.

    Both adapters are built over the *same* root. That is safe by construction -
    SQLite owns ``<root>/memory.db`` while the file adapter owns
    ``<root>/{curated,episodes,sessions}`` - and it is what makes
    ``TRACE_AGENT_MEMORY_STORE=json`` a real rollback: the JSON records are still
    there, untouched, unless the migration was JSON -> SQLite and the operator
    then kept writing to SQLite.
    """
    source = build_memory_store(root, namespace=namespace, kind=source_kind or "json")
    target = build_memory_store(
        root, namespace=namespace, kind=target_kind or "sqlite"
    )
    try:
        if getattr(source, "store_kind", "") == getattr(target, "store_kind", ""):
            report = MigrationReport(
                source_kind=str(source.store_kind),
                target_kind=str(target.store_kind),
                namespace=str(getattr(source, "namespace", "")),
                dry_run=dry_run,
            )
            report.errors.append(
                f"source and target are both {source.store_kind!r}; nothing to do"
            )
            return report
        return migrate_corpus(
            source=source,
            target=target,
            dry_run=dry_run,
            backup=backup,
            verify=verify,
            keep_trust=keep_trust,
        )
    finally:
        for store in (source, target):
            close = getattr(store, "close", None)
            if callable(close):
                close()


__all__ = [
    "BACKUP_DIR_NAME",
    "BUNDLE_VERSION",
    "DEMOTION_REASON",
    "MIGRATE_ACTOR",
    "MigrationReport",
    "auto_backup",
    "demote_unexplained_trust",
    "earned_trusts",
    "export_bundle",
    "import_bundle",
    "migrate_corpus",
    "migrate_store",
    "read_bundle",
    "write_export",
]
