"""Bounded reranking signals for memory recall.

The previous scorer summed a token-coverage ratio, a phrase bonus and a keyword
bonus, then clamped the result with ``min(score, 1.0)``. The clamp made many
weakly-related hits tie at exactly ``1.0``, so top-k ordering became arbitrary
and nothing else - confidence, recency, trust - could break the tie.

This module replaces that with a weighted sum of signals that are each already
normalised to ``[0, 1]``. The total is therefore bounded without a clamp, ties
require every signal to be maximal, and a new signal (embeddings in P3) can be
added by giving it weight instead of rewriting the scorer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from trace_agent.memory.flags import memory_half_life_days
from trace_agent.memory.models import (
    CORROBORATED_FINDING_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    MemoryStatus,
)
from trace_agent.memory.text import tokenize
from trace_agent.models import FindingSeverity, utc_now

_SECONDS_PER_DAY = 86_400.0
_WHITESPACE_RE = re.compile(r"\s+")

# Relevance sub-weights. Coverage dominates because it is the only component
# that measures "is this memory about the same thing as the query".
_COVERAGE_WEIGHT = 0.70
_KEYWORD_WEIGHT = 0.20
# Public because it bounds what an FTS index can safely miss: a phrase-only
# match scores exactly this much, so any relevance floor above it guarantees
# every surviving hit shares a real token with the query. Callers use that to
# decide whether index pushdown may replace a full scan.
PHRASE_WEIGHT = 0.10

# How much a trust level is worth as a prior. ``candidate`` is deliberately far
# below ``active``: a single-run guess must not outrank a corroborated pattern.
TRUST_PRIOR: dict[MemoryStatus, float] = {
    MemoryStatus.VERIFIED: 1.0,
    MemoryStatus.CONSOLIDATED: 1.0,
    MemoryStatus.ACTIVE: 0.70,
    MemoryStatus.CANDIDATE: 0.25,
    MemoryStatus.SUPERSEDED: 0.0,
    MemoryStatus.ARCHIVED: 0.0,
}

# An episode records that something happened, not that an interpretation is
# right, so it sits between ``candidate`` and ``active``.
EPISODIC_TRUST_PRIOR = 0.50

# Stand-in for the ``importance`` column until P2 maintains it from decay and
# promotion history. Severity is the strongest signal available today;
# corroboration adds the rest with diminishing returns, so one very common
# pattern cannot dominate every query.
_SEVERITY_IMPORTANCE = {
    FindingSeverity.LOW.value: 0.35,
    FindingSeverity.MEDIUM.value: 0.55,
    FindingSeverity.HIGH.value: 0.75,
    FindingSeverity.CRITICAL.value: 0.95,
}
DEFAULT_IMPORTANCE = 0.50

# Ordering used when two records have to be merged and only one severity can
# survive. Kept next to the importance table because both answer "how much does
# this label matter", and a third private copy in a lifecycle module would
# eventually disagree with one of them.
_SEVERITY_RANK = {
    FindingSeverity.LOW.value: 0,
    FindingSeverity.MEDIUM.value: 1,
    FindingSeverity.HIGH.value: 2,
    FindingSeverity.CRITICAL.value: 3,
}


def max_severity(
    left: FindingSeverity | None,
    right: FindingSeverity | None,
) -> FindingSeverity | None:
    """The more severe of two labels, treating ``None`` as "unknown".

    Shared by consolidation (merging new evidence into one memory) and the
    conflict detector (absorbing a duplicate into its winner). Which of two
    near-identical records survives must not depend on which code path did the
    merging, so there is one definition.
    """
    if left is None:
        return right
    if right is None:
        return left
    if _SEVERITY_RANK.get(right.value, 0) > _SEVERITY_RANK.get(left.value, 0):
        return right
    return left


@dataclass(frozen=True, slots=True)
class RecallWeights:
    """Weight table for the reranker; all values sum to 1.0.

    ``DEFAULT`` is the lexical-only profile used until embeddings land. The
    semantic weight is zero there, so its share is redistributed to lexical
    rather than lost - otherwise every score would be capped at 0.5 and the
    ranking would compress.
    """

    semantic: float = 0.00
    lexical: float = 0.50
    confidence: float = 0.15
    importance: float = 0.10
    freshness: float = 0.10
    trust: float = 0.15


DEFAULT_WEIGHTS = RecallWeights()

# Profile to switch to once an embedding store is wired up (P3).
SEMANTIC_WEIGHTS = RecallWeights(
    semantic=0.35,
    lexical=0.25,
    confidence=0.10,
    importance=0.10,
    freshness=0.10,
    trust=0.10,
)


@dataclass(frozen=True, slots=True)
class ScoreSignals:
    """Normalised inputs to the reranker for one (query, memory) pair."""

    lexical: float = 0.0
    semantic: float = 0.0
    confidence: float = 0.0
    importance: float = 0.5
    freshness: float = 1.0
    trust_prior: float = 0.0

    @property
    def relevance(self) -> float:
        """Query match quality, ignoring how much the memory is trusted.

        This is the admission filter. Trust and recency may reorder results but
        must never let an unrelated memory in, which is why they are excluded
        here.
        """
        return max(self.lexical, self.semantic)


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def _normalise(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def trust_prior(status: MemoryStatus) -> float:
    """Prior weight for a curated memory at the given trust level."""
    return TRUST_PRIOR.get(status, 0.0)


def lexical_score(
    query: str,
    text: str,
    keywords: Sequence[str] = (),
) -> float:
    """Token-overlap relevance in ``[0, 1]``.

    Both sides go through :func:`trace_agent.memory.text.tokenize`, so CJK is
    compared as bigrams. That is what lets ``"冷启动 白屏"`` and
    ``"启动白屏问题排查"`` match a memory stored as ``"冷启动白屏"`` - with the old
    whole-run token, only a byte-identical sentence could ever match.
    """
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return 0.0

    text_tokens = set(tokenize(text))
    coverage = len(query_tokens & text_tokens) / len(query_tokens)

    keyword_tokens = {token for keyword in keywords for token in tokenize(keyword)}
    overlap = (
        len(query_tokens & keyword_tokens) / len(keyword_tokens)
        if keyword_tokens
        else 0.0
    )

    normalised_query = _normalise(query)
    phrase = 1.0 if normalised_query and normalised_query in _normalise(text) else 0.0

    return _clamp(
        _COVERAGE_WEIGHT * coverage
        + _KEYWORD_WEIGHT * overlap
        + PHRASE_WEIGHT * phrase
    )


def freshness(
    created_at: datetime,
    *,
    now: datetime | None = None,
    half_life_days: float | None = None,
) -> float:
    """Exponential recency in ``[0, 1]`` using a true half-life.

    ``0.5 ** (age / half_life)`` means a memory exactly one half-life old
    contributes half the recency signal, which is what the config name implies.
    """
    half_life = memory_half_life_days() if half_life_days is None else half_life_days
    if half_life <= 0:
        return 1.0
    reference = now or utc_now()
    stamp = _as_utc(created_at)
    age_days = max(0.0, (reference - stamp).total_seconds() / _SECONDS_PER_DAY)
    return 0.5 ** (age_days / half_life)


def _as_utc(value: datetime) -> datetime:
    # Legacy JSON records may predate tz-aware timestamps.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def weighted_score(
    signals: ScoreSignals,
    weights: RecallWeights = DEFAULT_WEIGHTS,
) -> float:
    """Combine normalised signals into one bounded, discriminative score."""
    total = (
        weights.semantic * _clamp(signals.semantic)
        + weights.lexical * _clamp(signals.lexical)
        + weights.confidence * _clamp(signals.confidence)
        + weights.importance * _clamp(signals.importance)
        + weights.freshness * _clamp(signals.freshness)
        + weights.trust * _clamp(signals.trust_prior)
    )
    # Rounded so float noise cannot make two genuinely-equal hits sort apart
    # between runs; recall ordering must be deterministic.
    return round(_clamp(total), 6)


def importance_for(memory: CuratedMemory) -> float:
    """Normalised ``importance`` for a curated memory.

    Lives here rather than in :mod:`trace_agent.memory.recall` because the SQL
    store denormalises it into the ``importance`` column, and two definitions of
    one signal would eventually disagree. The column is a cache for SQL-side
    filtering and analytics; scoring always calls this function, so a stale
    column cannot change a ranking.
    """
    base = (
        _SEVERITY_IMPORTANCE.get(memory.severity.value, DEFAULT_IMPORTANCE)
        if memory.severity is not None
        else DEFAULT_IMPORTANCE
    )
    corroboration = min(1.0, max(0, memory.distinct_runs - 1) / 4.0)
    return _clamp(0.7 * base + 0.3 * corroboration)


def episode_confidence(episode: EpisodicMemory) -> float:
    """Mean confidence across an episode's findings, in ``[0, 1]``.

    An episode carries no single confidence of its own, so the findings are
    averaged. Malformed or missing values are skipped rather than treated as
    zero, and an episode with no usable finding falls back to
    :data:`DEFAULT_IMPORTANCE`.
    """
    values: list[float] = []
    for finding in episode.findings:
        raw = finding.get("confidence")
        try:
            values.append(min(1.0, max(0.0, float(raw))))
        except (TypeError, ValueError):
            continue
    return sum(values) / len(values) if values else DEFAULT_IMPORTANCE


def episode_is_corroborated(episode: EpisodicMemory) -> bool:
    """Whether an episode carries evidence rather than only a guess.

    Two ways in, both of them external to the run that wrote the episode:
    a finding the analysis itself marked ``confirmed``/``observed``, or a pass
    from the deterministic eval grader (section 8.5). An episode whose every
    finding is ``suspected`` is one run's untested hypothesis, which is exactly
    what section 10 forbids injecting as a prior.
    """
    if episode.graded_pass is True:
        return True
    return any(
        str(finding.get("status") or "").strip().lower()
        in CORROBORATED_FINDING_STATUSES
        for finding in episode.findings
    )


def episode_trust(episode: EpisodicMemory) -> MemoryStatus:
    """The trust level an episode is recalled and rendered under.

    Episodes have no ``status`` column of their own - they are projections of a
    finished run, not records the gate promotes - so recall used to give every
    one of them the same mid-range prior and the composer filed them all under
    "historical similar case". That made a single-run guess reach the prompt
    wearing the same heading as a corroborated pattern, which is the
    self-contamination path section 10 exists to close, only through the episode
    channel instead of the curated one.

    Deriving the level from the episode's own evidence keeps the two channels
    under one rule. It is deliberately capped at ``ACTIVE``: ``verified`` means
    "may reorder an investigation", and section 5.2 makes ``active`` the rung
    every automatic ``verified`` decision stands on, so one graded run cannot
    skip it.
    """
    if episode_is_corroborated(episode):
        return MemoryStatus.ACTIVE
    return MemoryStatus.CANDIDATE


def episode_trust_prior(episode: EpisodicMemory) -> float:
    """Prior weight for an episode, discounted when it is only a guess.

    A corroborated episode keeps :data:`EPISODIC_TRUST_PRIOR`. An uncorroborated
    one drops to the ``candidate`` prior, so even when candidate recall is
    explicitly enabled it ranks below a real pattern instead of beside it.
    """
    if episode_is_corroborated(episode):
        return EPISODIC_TRUST_PRIOR
    return TRUST_PRIOR[MemoryStatus.CANDIDATE]
