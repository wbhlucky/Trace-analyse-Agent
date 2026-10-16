"""Rolling project digest - the ``project`` job of the dreaming beat.

Section 2.3 lists ``PROJECT`` alongside ``SUMMARY`` as a memory kind, and until
now only ``SUMMARY`` existed: a session could answer "what did we get to", but
nothing could answer "what does this project's corpus look like". The gap is
visible from the run side - a fresh analysis has no way to recall that the last
twelve runs on this namespace all landed on the same device, the same build and
the same two root causes, which is exactly the prior worth injecting.

It is a *digest* and not a claim, so it inherits the two constraints
:mod:`trace_agent.memory.summarize` was built under, for the same reason: this
runs in a background beat, unattended.

*Deterministic.* No LLM, no timestamps, no iteration order that depends on the
adapter. Every line is a count or a truncated copy of stored text, and every
collection is sorted before it is rendered. A digest that drifted would be
recalled as a prior, so "it changed because the corpus changed" has to be the
only way it can change.

*Idempotent.* Re-digesting an unchanged corpus writes nothing. Without that, a
beat fired every few minutes would churn ``updated_at`` on the project record
forever, and since freshness is a ranking input it would pin the digest to the
top of every recall - a projection of the corpus outranking the corpus.

The record is exempt from the gate's corroboration rule via
:data:`~trace_agent.memory.lifecycle.quality_gate.DERIVED_DIGEST_KINDS`, exactly
like ``SUMMARY``: there is no second independent run that could corroborate a
projection of all the runs, and demanding one would strand the record as a
permanent ``candidate`` nobody can ever use.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from trace_agent.memory.lifecycle.quality_gate import apply_trust_gate
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryKind,
    MemoryStatus,
    Provenance,
)
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.text import tokenize

# Recorded in ``memory_versions.actor`` so a digest refresh is distinguishable
# from consolidation, the gate, retention, the conflict detector and the session
# summariser that runs alongside it in the same beat.
PROJECT_ACTOR = "dreaming:project"

# Same reasoning as ``SUMMARY_CONFIDENCE``: the digest is faithful to its source
# but asserts nothing about any trace, so it sits below the gate's confidence
# floor and can never talk its own way into ``verified``.
PROJECT_CONFIDENCE = 0.5

MAX_DIGEST_CHARS = 1200
MAX_TOPIC_CHARS = 60
MAX_KEYWORDS = 12
# How many of each breakdown to render. Bounded so a corpus with a hundred
# devices produces a readable digest rather than a census.
MAX_BREAKDOWN = 4
# Root causes are the one free-text line, so they get their own cap.
MAX_ROOT_CAUSES = 3
MAX_ROOT_CAUSE_CHARS = 72
# Provenance carries the runs that went into the digest, newest by id. Capped
# for the same reason as the breakdowns; the count line is the honest total.
MAX_RUN_REFS = 20

_SLUG_LIMIT = 24
_NON_SLUG = re.compile(r"[^A-Za-z0-9_-]+")

# ``_trust_mix`` renders in lifecycle order rather than by count, so it needs the
# enum's declaration order as a list.
_STATUS_ORDER = tuple(status.value for status in MemoryStatus)


@dataclass(frozen=True, slots=True)
class ProjectOutcome:
    """What one digest pass did.

    ``changed`` is False when the corpus has not moved since the last pass,
    which is the common case and the signal the beat reports as "nothing to do".
    """

    namespace: str
    memory_id: str
    content: str
    changed: bool
    episodes: int
    memories: int

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection for events and the dashboard."""
        return {
            "namespace": self.namespace,
            "memory_id": self.memory_id,
            "changed": self.changed,
            "episodes": self.episodes,
            "memories": self.memories,
            "chars": len(self.content),
        }


class ProjectDigestService:
    """Extractive, idempotent per-namespace project digests."""

    def __init__(self, store: MemoryStore, *, max_chars: int = MAX_DIGEST_CHARS) -> None:
        self._store = store
        self._max_chars = max(1, max_chars)

    def digest(self) -> ProjectOutcome | None:
        """Refresh this namespace's project record.

        ``None`` means there is nothing to say yet: an empty corpus would
        otherwise produce a record whose every line reads ``0``, and recalling
        that as a prior is worse than recalling nothing.
        """
        namespace = _namespace(self._store)
        memory_id = project_memory_id(namespace)
        try:
            episodes = self._store.list_episodes()
            curated = self._store.list_curated()
        except Exception:
            # Best-effort like every other memory write: a failed digest must
            # not surface to the run that happens to own the beat.
            return None
        if not episodes and not curated:
            return None

        content = self._digest_text(episodes, curated, namespace)
        if not content:
            return None

        existing = None
        try:
            existing = self._store.load_curated(memory_id)
        except Exception:
            existing = None
        if existing is not None and (existing.content or "") == content:
            return ProjectOutcome(
                namespace=namespace,
                memory_id=memory_id,
                content=content,
                changed=False,
                episodes=len(episodes),
                memories=len(curated),
            )

        self._write(existing, content, memory_id, namespace, episodes)
        return ProjectOutcome(
            namespace=namespace,
            memory_id=memory_id,
            content=content,
            changed=True,
            episodes=len(episodes),
            memories=len(curated),
        )

    # ---------------------------------------------------------------- internals
    def _digest_text(
        self,
        episodes: list[EpisodicMemory],
        curated: list[CuratedMemory],
        namespace: str,
    ) -> str:
        """Render the digest. Sorted everywhere, timestamped nowhere.

        Line order is also priority order, because :func:`_fit` drops the tail
        when the budget runs out: what a run needs as a prior - how big the
        corpus is, what it covers, what keeps breaking - comes before what an
        operator merely finds interesting.
        """
        # The digest is a projection of the corpus, so the digest record itself
        # is excluded: counting it would make "how many memories does this
        # project have" drift by one the first time the beat runs, and the
        # second run would then see a different corpus than the first did.
        own_id = project_memory_id(namespace)
        records = [item for item in curated if item.memory_id != own_id]

        lines: list[str] = [f"namespace: {namespace or '(default)'}"]
        lines.append(
            f"runs: {len(episodes)} | memories: {len(records)}"
            f" | sessions: {_count(self._store, 'list_sessions')}"
        )
        lines.append(f"scenarios: {_breakdown(e.scenario_type for e in episodes)}")
        lines.append(f"devices: {_breakdown(e.device for e in episodes)}")
        lines.append(f"builds: {_breakdown(e.build for e in episodes)}")
        lines.append(
            f"processes: {_breakdown(e.target_process for e in episodes)}"
        )
        causes = _root_causes(episodes)
        if causes:
            lines.append("root causes:")
            lines.extend(f"  - {text}" for text in causes)
        trust = _trust_mix(records)
        if trust:
            lines.append(f"trust: {trust}")
        kinds = _breakdown(item.kind.value for item in records)
        if kinds:
            lines.append(f"kinds: {kinds}")
        graded = [e for e in episodes if e.graded_pass is not None]
        if graded:
            passed = sum(1 for e in graded if e.graded_pass)
            lines.append(f"eval: {passed}/{len(graded)} graded runs passed")

        return _fit(lines, self._max_chars)

    def _write(
        self,
        existing: CuratedMemory | None,
        content: str,
        memory_id: str,
        namespace: str,
        episodes: list[EpisodicMemory],
    ) -> None:
        references = _references(namespace, episodes)
        topic = _topic(namespace)
        keywords = _keywords(namespace, content)
        if existing is not None:
            merged = existing.model_copy(
                update={
                    "topic": topic,
                    "content": content,
                    "keywords": keywords,
                    "confidence": PROJECT_CONFIDENCE,
                    "provenance": Provenance(
                        source_type="project",
                        references=list(
                            dict.fromkeys(
                                [*existing.provenance.references, *references]
                            )
                        ),
                        created_by=PROJECT_ACTOR,
                        validated_by=existing.provenance.validated_by,
                    ),
                }
            )
        else:
            merged = CuratedMemory(
                memory_id=memory_id,
                kind=MemoryKind.PROJECT,
                # ``None``, like the ``profile`` record: a project digest is not
                # about one scenario, and a null scenario is what makes both
                # adapters match it against every query.
                scenario_type=None,
                topic=topic,
                content=content,
                keywords=keywords,
                confidence=PROJECT_CONFIDENCE,
                provenance=Provenance(
                    source_type="project",
                    references=references,
                    created_by=PROJECT_ACTOR,
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
                    reason="project: refreshed namespace digest",
                    actor=PROJECT_ACTOR,
                ):
                    self._store.write_curated(gated)
        except Exception:
            return


def project_memory_id(namespace: str) -> str:
    """Stable id for one namespace's project digest.

    Derived from the namespace rather than hashed from the content, so
    re-digesting updates one record instead of leaving a trail of superseded
    near-duplicates for the conflict detector to sweep up.

    Namespaces routinely contain ``:`` and other characters that are awkward in
    an id, so the readable part is slugged and truncated and a short hash of the
    *full* namespace is appended. The slug alone would collide - ``team:a`` and
    ``team:b`` both slug to ``team`` past the limit - and the hash alone would
    make ``memory stats`` output unreadable.
    """
    text = (namespace or "").strip()
    slug = _NON_SLUG.sub("-", text).strip("-")[:_SLUG_LIMIT].strip("-")
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"mem-project-{slug}-{digest}" if slug else f"mem-project-{digest}"


def _namespace(store: MemoryStore) -> str:
    """The store's namespace, without assuming the Protocol declares it."""
    return str(getattr(store, "namespace", "") or "")


def _count(store: MemoryStore, method: str) -> int:
    """Length of one listing, or 0 if the adapter cannot produce it."""
    try:
        return len(getattr(store, method)())
    except Exception:
        return 0


def _breakdown(values: Any) -> str:
    """``value=count`` for the top few non-empty values, sorted by rank.

    Sorted on ``(-count, value)`` rather than on count alone: ties are common in
    a small corpus, and an arbitrary tie order would make the digest text differ
    between two adapters that happen to list rows differently - which would look
    like a corpus change and defeat the idempotence check.
    """
    counted = Counter(
        str(value).strip() for value in values if value and str(value).strip()
    )
    if not counted:
        return "-"
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    shown = ranked[:MAX_BREAKDOWN]
    text = ", ".join(f"{name}={count}" for name, count in shown)
    if len(ranked) > len(shown):
        text += f", +{len(ranked) - len(shown)} more"
    return text


def _root_causes(episodes: list[EpisodicMemory]) -> list[str]:
    """Most-repeated root causes first, each truncated to one line."""
    counted: Counter[str] = Counter()
    for episode in episodes:
        for cause in episode.root_causes:
            text = " ".join(str(cause).split())[:MAX_ROOT_CAUSE_CHARS]
            if text:
                counted[text] += 1
    ranked = sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    return [
        f"{count}x {text}" if count > 1 else text
        for text, count in ranked[:MAX_ROOT_CAUSES]
    ]


def _trust_mix(records: list[CuratedMemory]) -> str:
    """``status=count`` over the corpus, in lifecycle order.

    Ordered by the enum rather than by count: the point of this line is "how
    governed is this corpus", and reading ``verified`` before ``archived``
    should not depend on which happens to be more numerous this week.
    """
    counted = Counter(item.status.value for item in records)
    if not counted:
        return ""
    ranked = sorted(
        counted.items(),
        key=lambda item: (
            _STATUS_ORDER.index(item[0])
            if item[0] in _STATUS_ORDER
            else len(_STATUS_ORDER),
            item[0],
        ),
    )
    return ", ".join(f"{name}={count}" for name, count in ranked)


def _references(namespace: str, episodes: list[EpisodicMemory]) -> list[str]:
    """Provenance: the namespace plus the runs that went into the digest."""
    runs = sorted({e.run_id for e in episodes if e.run_id})
    return [f"namespace:{namespace}", *(f"run:{run}" for run in runs[-MAX_RUN_REFS:])]


def _topic(namespace: str) -> str:
    return f"Project digest · {namespace or 'default'}"[:MAX_TOPIC_CHARS]


def _keywords(namespace: str, content: str) -> list[str]:
    """Tokens worth matching on, namespace first.

    Extracted with the same tokeniser recall uses, so the keywords this digest is
    found by are the keywords a query is split into - including the CJK bigrams,
    which is what makes a Chinese root cause in the digest recallable at all.
    """
    seeds = [token for token in tokenize(namespace) if token]
    tokens = [token for token in tokenize(content) if len(token) > 1]
    ordered = list(dict.fromkeys([*seeds, *tokens]))
    return ordered[:MAX_KEYWORDS]


def _fit(lines: list[str], limit: int) -> str:
    """Greedily keep whole lines until ``limit`` runs out.

    Slicing the joined text would cut the last line mid-word, and the digest is
    recalled verbatim into a prompt: half a root cause reads as a claim about
    something the corpus never said. The first line is always kept so a digest is
    never empty, and the trailing slice only ever fires on a pathological
    namespace longer than the whole budget.
    """
    kept: list[str] = []
    used = 0
    for line in lines:
        if kept and used + len(line) > limit:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)[:limit]


__all__ = [
    "MAX_DIGEST_CHARS",
    "PROJECT_ACTOR",
    "PROJECT_CONFIDENCE",
    "ProjectDigestService",
    "ProjectOutcome",
    "project_memory_id",
]
