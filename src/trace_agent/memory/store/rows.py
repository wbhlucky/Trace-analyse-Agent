"""Row-level helpers shared by every SQL adapter.

These helpers are the reason two SQL stores can claim to return *equal* objects
for equal writes. Both adapters keep the full Pydantic dump in a
``payload`` column and rebuild reads from it, so "how a column becomes JSON" and
"how JSON becomes a model" must be defined once: a store that serialised with
``ensure_ascii=True`` would round-trip CJK through escape sequences, and one
that raised on a corrupt row would turn a single bad record into an empty recall.

Anything dialect-specific - placeholder syntax, row access, ranking functions -
stays in the adapter, because that is exactly what differs between SQLite and
PostgreSQL.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any


def dumps_json(value: Any) -> str:
    """Compact JSON for a column. ``ensure_ascii=False`` keeps CJK readable."""
    return json.dumps(value, ensure_ascii=False, default=str)


def as_float(value: Any) -> float:
    """Float for a numeric column whose value crossed a serialisation boundary.

    ``confidence`` reaches a governance row three ways - off a model, out of a
    migration bundle's JSON, and back from a driver that may hand over a
    ``Decimal``. An unparseable value degrades to ``0.0`` rather than raising:
    one malformed number in an audit trail must not abort the copy of the corpus
    it belongs to.
    """
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def iso_stamp(value: datetime | None) -> str | None:
    """ISO-8601 text for a timestamp column, or ``None`` for a missing one.

    Timestamps are stored as text rather than a SQL datetime type on purpose:
    both adapters must agree byte for byte, and SQLite has no datetime storage
    class to agree with. Ordering is lexicographic over ISO stamps, which is
    chronological for the UTC-aware values :func:`trace_agent.models.utc_now`
    produces.
    """
    return value.isoformat() if value is not None else None


def validate_payload(model: type[Any], payload: Any) -> Any | None:
    """Rebuild a model from its ``payload`` column, skipping corrupt rows.

    Mirrors the JSON backend, which ignores unreadable files instead of failing
    the whole listing. Memory is best-effort: one bad row must not empty recall.
    """
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray, memoryview)):
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(payload)
        return model.model_validate(data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def record_id(record: Any) -> str:
    """Stable tie-breaker for a scored row, whichever record shape it holds.

    Sorting by similarity alone leaves equal scores in whatever order the scan
    produced, so a corpus edited between two runs would reorder its hits. Recall
    has to be deterministic, and the id is the one thing that always is.
    """
    return str(
        getattr(record, "memory_id", None)
        or getattr(record, "episode_id", "")
        or ""
    )


def optional_int(value: Any) -> int | None:
    """Integer for a nullable counter column, ``None`` when absent or unusable.

    Governance counters (``version``, ``distinct_runs``) are read back onto a
    record whose field is optional, and a driver may hand back ``None``, a
    string or a ``Decimal`` depending on the column type. Raising here would
    turn one legacy row into a failed listing, so the value degrades to
    ``None`` - "no governance metadata recorded" - exactly as a row written
    before the column existed does.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_stamp(value: Any) -> datetime | None:
    """Datetime for a text timestamp column, ``None`` when absent or malformed.

    The inverse of :func:`iso_stamp`. Tolerant on purpose: a naive stamp (a row
    written by hand, or by a driver that dropped the offset) still parses, and
    an unparseable one yields ``None`` rather than raising, because access time
    is metadata and a corrupt column must not cost a recall.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    # ``fromisoformat`` before 3.11 rejects a trailing ``Z``; a corpus exported
    # from PostgreSQL carries one.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def stored_validator(raw: Any) -> str | None:
    """``provenance.validated_by`` as persisted in a ``provenance`` column.

    Read out of the JSON blob rather than a dedicated column because the
    signature lives on the record, and duplicating it into the schema would give
    the two somewhere to disagree. Unparseable or missing provenance yields
    ``None``, i.e. "nobody has signed this", which is the safe reading.

    Shared by both SQL adapters because it drives the ``changed`` test in
    ``_put_curated``: an expert sign-off changes what a record *claims* without
    touching its text, and section 7 requires that to be auditable. Two adapters
    disagreeing about whether a sign-off happened would mean the same corpus has
    two different audit trails depending on which store it lives in.
    """
    if isinstance(raw, (bytes, bytearray, memoryview)):
        # A BYTEA/JSONB column can come back as bytes depending on the driver.
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not raw:
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("validated_by")
    return value.strip() if isinstance(value, str) and value.strip() else None


def state_key(
    topic: Any, content: Any, trust: Any, confidence: Any
) -> tuple[Any, ...]:
    """Comparable identity of one record state.

    Used by ``_put_curated`` to recognise a *re-import*: a migration loads the
    audit trail before the records, so an insert can arrive with history that
    already ends at exactly this state. Recording a version row there would
    duplicate the tail and make the corpus look edited by the migration itself.

    Normalised rather than compared field by field because the stored side comes
    from a driver - ``confidence`` may be a ``Decimal`` on PostgreSQL and a float
    on SQLite, and ``trust`` may be ``None`` on a row written before the column
    existed. Rounding keeps a float round-trip through text from reading as a
    change, which would be a fabricated version row per migration.
    """
    try:
        conf = round(float(confidence or 0.0), 9)
    except (TypeError, ValueError):
        conf = 0.0
    return (str(topic or ""), str(content or ""), str(trust or ""), conf)


class VersionResolver:
    """Stable ordinal for audit rows that arrive without one.

    ``memory_versions`` is keyed by ``UNIQUE(namespace, memory_id, version)``,
    and that constraint is the entire reason ``import_governance`` is idempotent.
    But a row exported by the file adapter's sidecar records the *transition* and
    not its ordinal, so one has to be derived - and the obvious derivation,
    ``MAX(version) + 1``, is not stable across replays. The second import of the
    same bundle computes a different ordinal, the conflict never fires, and every
    re-run of a migration grows the audit trail by one duplicate per row. That
    breaks section 12.2's idempotence in the JSON -> SQL direction, which is the
    one direction a rollback rehearsal actually exercises.

    So the derivation is keyed on the row's own identity instead: the same four
    fields the file adapter deduplicates sidecar rows on. A transition already
    present reuses the ordinal it was given the first time, the insert conflicts,
    and nothing is added.

    ``__init__`` takes the rows already in the table so both adapters load them
    with one ``SELECT`` rather than one per record; ``ordinal`` then answers in
    constant time and remembers what it handed out, so two identical rows inside
    a *single* bundle also collapse to one.
    """

    __slots__ = ("_known", "_highest")

    def __init__(
        self, existing: Iterable[tuple[Any, Any, Any, Any, Any]] = ()
    ) -> None:
        #: ``(memory_id, trust, reason, created_at)`` -> the ordinal it already has.
        self._known: dict[tuple[str, str, str, str], int] = {}
        #: ``memory_id`` -> its highest ordinal, the seed for a genuinely new row.
        self._highest: dict[str, int] = {}
        for memory_id, version, trust, reason, created_at in existing:
            ordinal = optional_int(version) or 0
            ident = str(memory_id or "")
            self._known.setdefault(
                (ident, str(trust or ""), str(reason or ""), str(created_at or "")),
                ordinal,
            )
            if ordinal > self._highest.get(ident, 0):
                self._highest[ident] = ordinal

    def ordinal(
        self,
        *,
        memory_id: str,
        version: Any = None,
        trust: str = "",
        reason: str = "",
        created_at: str = "",
    ) -> int:
        """The ordinal to insert under, reusing an existing one when it matches.

        An explicit ``version`` always wins: a SQL -> SQL copy is already
        ordinal-bearing and must land on the same row it came from, not on a
        renumbered one.
        """
        explicit = optional_int(version)
        if explicit is not None:
            return explicit
        key = (memory_id, str(trust or ""), str(reason or ""), str(created_at or ""))
        found = self._known.get(key)
        if found is not None:
            return found
        derived = self._highest.get(memory_id, 0) + 1
        self._known[key] = derived
        self._highest[memory_id] = derived
        return derived


__all__ = [
    "VersionResolver",
    "as_float",
    "dumps_json",
    "iso_stamp",
    "optional_int",
    "parse_stamp",
    "record_id",
    "state_key",
    "stored_validator",
    "validate_payload",
]
