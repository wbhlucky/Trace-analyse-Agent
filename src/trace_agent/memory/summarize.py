"""Rolling session digests - the ``summarize`` job of the dreaming beat.

Section 5.4 lists four background jobs; three of them govern memory that
already exists. This one produces the last missing kind: a ``SUMMARY`` record
that says what a session was about, so a later run can recall "we already
looked at this trace and got as far as X" without re-reading every turn.

Two properties are non-negotiable and both come from the same place - this runs
in a background beat, unattended:

*Deterministic.* No LLM. A digest that can drift is a digest that can invent a
conclusion the session never reached, and because it is written back as memory
it would then be recalled as a prior. Extractive compression cannot fabricate:
every line here is a truncated copy of a stored turn.

*Idempotent.* Re-summarising an unchanged session writes nothing. The beat may
fire as often as the dashboard likes, and a sweep that rewrote records on every
pass would churn ``updated_at``, refresh retention deadlines and reorder recall
by freshness for no reason.

The digest lands in two places with different lifecycles. ``SessionMemory.summary``
is session state and is always written - rendering a session's context must not
depend on trust. The ``SUMMARY`` memory is the recallable projection of the same
text, and it goes through :func:`~trace_agent.memory.lifecycle.quality_gate.apply_trust_gate`
like every other record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trace_agent.memory.lifecycle.quality_gate import apply_trust_gate
from trace_agent.memory.models import (
    CuratedMemory,
    MemoryKind,
    Provenance,
    SessionMemory,
    SessionRole,
)
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.text import tokenize

# Recorded in ``memory_versions.actor`` so a digest refresh is distinguishable
# from consolidation, the gate, retention and the conflict detector.
SUMMARIZE_ACTOR = "dreaming:summarize"

# Only the tail of a long session is digestible; older turns are what the
# session record itself is for.
MAX_TURNS = 12
# Per-turn truncation. A turn is usually a question or a one-line conclusion,
# so this keeps the digest readable rather than becoming a second transcript.
MAX_TURN_CHARS = 160
MAX_SUMMARY_CHARS = 900
MAX_TOPIC_CHARS = 60
MAX_KEYWORDS = 12

# A digest is faithful to its source but asserts nothing about the trace, so it
# sits deliberately below the gate's confidence floor: it can never talk its own
# way into ``verified``, and its ``active`` status comes from the gate's
# documented summary exemption rather than from this number.
SUMMARY_CONFIDENCE = 0.5

_ROLE_LABELS: dict[SessionRole, str] = {
    SessionRole.USER: "用户",
    SessionRole.ASSISTANT: "分析",
    SessionRole.SYSTEM: "系统",
}


@dataclass(frozen=True, slots=True)
class SummaryOutcome:
    """What one digest pass did.

    ``changed`` is False when the session was already up to date, which is the
    common case and the signal the dreaming beat reports as "nothing to do".
    """

    session_id: str
    summary: str
    memory_id: str
    changed: bool
    turns: int

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection for events and the dashboard."""
        return {
            "session_id": self.session_id,
            "memory_id": self.memory_id,
            "changed": self.changed,
            "turns": self.turns,
            "chars": len(self.summary),
        }


class SessionSummarizer:
    """Extractive, idempotent session digests."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        max_turns: int = MAX_TURNS,
        max_chars: int = MAX_SUMMARY_CHARS,
    ) -> None:
        self._store = store
        self._max_turns = max(1, max_turns)
        self._max_chars = max(1, max_chars)

    def summarize(self, session_id: str) -> SummaryOutcome | None:
        """Digest one session. ``None`` means it does not exist or is empty."""
        if not session_id:
            return None
        try:
            session = self._store.load_session(session_id)
        except Exception:
            return None
        if session is None:
            return None
        return self._digest(session)

    def sweep(self, *, limit: int = 20) -> list[SummaryOutcome]:
        """Digest every session whose summary has gone stale.

        Ordered oldest-update-first so a beat that hits ``limit`` still makes
        progress across the corpus instead of revisiting the same busy sessions.
        """
        try:
            sessions = self._store.list_sessions()
        except Exception:
            return []
        stale = [
            session
            for session in sessions
            if self._digest_text(session) != (session.summary or "")
        ]
        stale.sort(key=lambda item: item.updated_at)
        outcomes: list[SummaryOutcome] = []
        for session in stale[: max(0, limit)]:
            outcome = self._digest(session)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------------ internals
    def _digest(self, session: SessionMemory) -> SummaryOutcome | None:
        digest = self._digest_text(session)
        memory_id = summary_memory_id(session.session_id)
        if not digest:
            return None
        if digest == (session.summary or ""):
            # Idempotence: same turns in, same text out, nothing written.
            return SummaryOutcome(
                session_id=session.session_id,
                summary=digest,
                memory_id=memory_id,
                changed=False,
                turns=len(session.messages),
            )

        self._write_session_summary(session, digest)
        self._write_memory(session, digest, memory_id)
        return SummaryOutcome(
            session_id=session.session_id,
            summary=digest,
            memory_id=memory_id,
            changed=True,
            turns=len(session.messages),
        )

    def _digest_text(self, session: SessionMemory) -> str:
        lines: list[str] = []
        used = 0
        for message in session.messages[-self._max_turns :]:
            label = _ROLE_LABELS.get(message.role, message.role.value)
            body = _first_line(message.content, MAX_TURN_CHARS)
            if not body:
                continue
            line = f"{label}: {body}"
            if used + len(line) > self._max_chars:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def _write_session_summary(self, session: SessionMemory, digest: str) -> None:
        try:
            self._store.write_session(
                session.model_copy(update={"summary": digest})
            )
        except Exception:
            # Best-effort like every other memory write: a failed digest must
            # not surface to the run that happens to own the beat.
            return

    def _write_memory(
        self, session: SessionMemory, digest: str, memory_id: str
    ) -> None:
        existing = None
        try:
            existing = self._store.load_curated(memory_id)
        except Exception:
            existing = None

        references = _references(session)
        topic = _topic(session)
        keywords = _keywords(session, digest)
        if existing is not None:
            merged = existing.model_copy(
                update={
                    "topic": topic,
                    "content": digest,
                    "keywords": keywords,
                    "scenario_type": session.scenario_type or existing.scenario_type,
                    "confidence": SUMMARY_CONFIDENCE,
                    "provenance": Provenance(
                        source_type="session",
                        references=list(
                            dict.fromkeys(
                                [*existing.provenance.references, *references]
                            )
                        ),
                        created_by=SUMMARIZE_ACTOR,
                        validated_by=existing.provenance.validated_by,
                    ),
                }
            )
        else:
            merged = CuratedMemory(
                memory_id=memory_id,
                kind=MemoryKind.SUMMARY,
                scenario_type=session.scenario_type,
                topic=topic,
                content=digest,
                keywords=keywords,
                confidence=SUMMARY_CONFIDENCE,
                provenance=Provenance(
                    source_type="session",
                    references=references,
                    created_by=SUMMARIZE_ACTOR,
                ),
            )
        gated = apply_trust_gate(merged)
        try:
            if existing is None:
                self._store.write_curated(gated)
            else:
                # ``revise`` rather than ``write_curated``: this is a governance
                # edit, and section 7 wants the reason in the version row.
                if not self._store.revise(
                    gated,
                    reason="summarize: refreshed session digest",
                    actor=SUMMARIZE_ACTOR,
                ):
                    self._store.write_curated(gated)
        except Exception:
            return


def summary_memory_id(session_id: str) -> str:
    """Stable id for a session's digest memory.

    Derived from the session id, not hashed from the content, so re-digesting
    updates one record instead of leaving a trail of superseded near-duplicates
    for the conflict detector to sweep up.
    """
    return f"mem-summary-{session_id}"


def _references(session: SessionMemory) -> list[str]:
    """Provenance for a digest: the session plus whatever its turns cite."""
    refs = [f"session:{session.session_id}"]
    for message in session.messages:
        refs.extend(message.references)
    return list(dict.fromkeys(refs))


def _topic(session: SessionMemory) -> str:
    for candidate in (session.title, session.topic):
        text = (candidate or "").strip()
        if text:
            return _first_line(text, MAX_TOPIC_CHARS)
    for message in session.messages:
        if message.role is SessionRole.USER:
            return _first_line(message.content, MAX_TOPIC_CHARS)
    return f"session {session.session_id}"


def _keywords(session: SessionMemory, digest: str) -> list[str]:
    """Tokens worth matching on, scenario/device/build first.

    Extracted from the digest with the same tokeniser recall uses, so the
    keywords a summary is found by are the keywords a query is split into -
    including the CJK bigrams, which is what makes a Chinese session title
    recallable at all.
    """
    seeds = [
        value
        for value in (session.scenario_type, session.device, session.build, session.topic)
        if value
    ]
    tokens = [token for token in tokenize(digest) if len(token) > 1]
    ordered = list(dict.fromkeys([*seeds, *tokens]))
    return ordered[:MAX_KEYWORDS]


def _first_line(text: str, limit: int) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line:
            return line[:limit]
    return ""


__all__ = [
    "MAX_SUMMARY_CHARS",
    "MAX_TURNS",
    "SUMMARIZE_ACTOR",
    "SUMMARY_CONFIDENCE",
    "SessionSummarizer",
    "SummaryOutcome",
    "summary_memory_id",
]
