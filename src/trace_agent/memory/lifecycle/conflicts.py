"""Detecting that two memories disagree, and that two are the same.

Consolidation deduplicates by exact content hash, which is enough to stop one
run writing the same sentence twice and nowhere near enough to notice that two
runs reached *different* conclusions about the same symptom. Left alone, both
records stay recallable forever and the agent is handed a contradiction it has
no way to see - the failure mode section 5.3 exists to close.

Three relations come out of a sweep, split by how alike two same-topic records
are and whether their runs are independent:

``duplicate-of``
    Same scope, related topics, near-identical content. The weaker
    record is superseded and recall stops paying for it. Nothing is deleted:
    the row and its version history stay, so a bad merge is reversible.

``contradicts``
    Same scope, related topics, **disjoint source runs**, and an actual
    disagreement: either one side rejects the cause while the other asserts it, or
    the two share so little substance that they are describing different
    mechanisms. Two independent analyses disagreeing is evidence; one run
    disagreeing with itself is a wording difference. Each new refutation halves
    the weaker memory's confidence, and the quality gate archives it once
    ``MAX_CONTRADICTIONS`` accumulate.

    Similarity alone used to decide this, and it was wrong in the direction that
    hurts most. An LLM restating the same conclusion in a fresh run produces long
    Chinese sentences whose bigram sets barely line up - 0.37 Jaccard for a pair
    with a byte-identical topic - so the sweep read corroboration as refutation,
    halved the confidence of both records and blocked the promotion the evidence
    had earned. A lexical measure cannot tell a paraphrase from a rebuttal; a
    polarity signal or a near-empty overlap can.

``supports``
    Same scope, related topics, **disjoint source runs**, and no
    disagreement - similar enough to agree, not similar enough to fold into one
    record. Two analyses that reached the same conclusion in different words are
    corroboration, not a paraphrase: the link lets the quality gate count the
    other record's runs toward this one's ``candidate -> active`` threshold
    without superseding either, and read the other side's ``confirmed`` finding
    as evidence for a conclusion both sides reached. No confidence change, no
    retirement. This is the default reading of a related pair, which is the
    point: agreement is common, refutation needs evidence.

Which pairs are compared at all is a separate question from how they are judged,
and it used to be answered wrongly. Records were bucketed by an *exact* topic
fingerprint: a correct blocking function and a useless one, because the subject
line is the part of a finding an LLM most reliably rewords between runs. On the
real corpus that put 53 records into 53 singleton buckets, so the sweep ran to
completion, compared nothing, and reported success while every memory stayed a
single-run ``candidate``. :func:`salient_tokens` now blocks on shared rare topic
tokens instead, and :func:`related_topics` is the floor underneath it - blocking
decides what is cheap to find, relatedness decides what is actually about the
same thing, and only then do the three relations above get a vote.

The replacement had the same failure mode wearing a different knob: a
document-frequency cap that drops tokens shared by "too many" records discards
*every* token of a small scope whose records share one subject, and the sweep
again compared nothing while reporting success. So the cap is a preference and
never a veto - :func:`_blocks` guarantees a record with a subject lands in some
block, and that invariant is what the tests pin.

The sweep is idempotent. Links upsert, and the confidence penalty is applied
only when the contradiction count actually grew, so running it every beat cannot
decay a memory towards zero on its own.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Sequence

from trace_agent.memory.flags import (
    memory_conflict_similarity,
    memory_conflict_overlap,
    memory_duplicate_similarity,
    memory_related_overlap,
    memory_related_similarity,
)
from trace_agent.memory.lifecycle.promoter import (
    TrustTransition,
    commit_gate,
    contradictions_for,
)
from trace_agent.memory.models import (
    RETIRED_STATUSES,
    CuratedMemory,
    MemoryStatus,
    Provenance,
)
from trace_agent.memory.ports import MemoryRelation, MemoryStore
from trace_agent.memory.rerank import max_severity
from trace_agent.memory.text import (
    overlap_coefficient,
    polarity_conflict,
    similarity,
    tokenize,
)

# Recorded in ``memory_versions.actor`` for every edit this module makes.
CONFLICT_ACTOR = "conflict-detector"

# Section 5.2: a contradiction halves the confidence of the weaker side.
CONFLICT_CONFIDENCE_PENALTY = 0.5

# Guard against a pathological corpus where one token covers thousands of
# records. Comparisons inside a block are quadratic; capping the block keeps a
# background beat bounded, and the records it skips are the oldest ones, which
# the next sweep re-examines as the block churns.
_MAX_GROUP_SIZE = 64

# A token appearing in more than this share of a scope is boilerplate rather than
# a subject: "首屏" and "主线程" describe every record in a white-screen corpus
# and block nothing. ``_MIN_BLOCK_DF`` keeps small corpora workable, because a
# 10% cap over five records is one and would discard every shared word.
#
# The cap is a preference, not a filter with veto power: a record whose every
# salient token exceeds it is still blocked on its rarest one, so no subject can
# be declared too common to compare. See :func:`_blocks`.
_MAX_BLOCK_DF_RATIO = 0.10
_MIN_BLOCK_DF = 2

# Ceiling on comparisons per sweep. Blocks are examined newest first, so a corpus
# too large to sweep in one beat still links the records that were just written -
# the only ones whose promotion is actually waiting on a link.
_MAX_PAIRS_PER_SWEEP = 2048

# Shorter tokens carry no subject. Single CJK characters and single ASCII letters
# are what the bigram tokenizer leaves at the edges of a run, and blocking on
# them would pair up most of the corpus.
_MIN_BLOCK_TOKEN_LEN = 2

# Higher wins a duplicate merge. Trust first, because superseding a verified
# memory in favour of a candidate would be exactly backwards.
_TRUST_RANK = {
    MemoryStatus.VERIFIED: 4,
    MemoryStatus.CONSOLIDATED: 4,
    MemoryStatus.ACTIVE: 3,
    MemoryStatus.CANDIDATE: 2,
    MemoryStatus.SUPERSEDED: 1,
    MemoryStatus.ARCHIVED: 0,
}


@dataclass(frozen=True, slots=True)
class ConflictPair:
    """One detected relation between two records, before it is persisted."""

    left: str
    right: str
    relation: MemoryRelation
    similarity: float
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left,
            "right": self.right,
            "relation": self.relation.value,
            "similarity": round(self.similarity, 4),
            "reason": self.reason,
        }


@dataclass(slots=True)
class ConflictReport:
    """What one sweep found and what it changed."""

    scanned: int = 0
    groups: int = 0
    contradictions: list[ConflictPair] = field(default_factory=list)
    duplicates: list[ConflictPair] = field(default_factory=list)
    supports: list[ConflictPair] = field(default_factory=list)
    superseded: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    transitions: list[TrustTransition] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "groups": self.groups,
            "contradictions": [pair.as_dict() for pair in self.contradictions],
            "duplicates": [pair.as_dict() for pair in self.duplicates],
            "supports": [pair.as_dict() for pair in self.supports],
            "superseded": list(self.superseded),
            "demoted": list(self.demoted),
            "transitions": [item.as_dict() for item in self.transitions],
        }


class ConflictDetector:
    """Pairwise conflict sweep over one scope at a time."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        duplicate_similarity: float | None = None,
        conflict_similarity: float | None = None,
        conflict_overlap: float | None = None,
        related_overlap: float | None = None,
        related_similarity: float | None = None,
        actor: str = CONFLICT_ACTOR,
    ) -> None:
        self._store = store
        self._duplicate_similarity = duplicate_similarity
        self._conflict_similarity = conflict_similarity
        self._conflict_overlap = conflict_overlap
        self._related_overlap = related_overlap
        self._related_similarity = related_similarity
        self._actor = actor

    def scan(self) -> ConflictReport:
        """Find relations without writing anything.

        Split from :meth:`resolve` so ``memory conflicts --dry-run`` and the
        dashboard can show an operator what a sweep *would* do before it hides a
        record from recall.
        """
        report = ConflictReport()
        duplicate_floor = (
            memory_duplicate_similarity()
            if self._duplicate_similarity is None
            else self._duplicate_similarity
        )
        conflict_ceiling = (
            memory_conflict_similarity()
            if self._conflict_similarity is None
            else self._conflict_similarity
        )
        overlap_floor = (
            memory_conflict_overlap()
            if self._conflict_overlap is None
            else self._conflict_overlap
        )
        related_overlap_floor = (
            memory_related_overlap()
            if self._related_overlap is None
            else self._related_overlap
        )
        related_similarity_floor = (
            memory_related_similarity()
            if self._related_similarity is None
            else self._related_similarity
        )

        for left, right in self._candidate_pairs(report):
            if not related_topics(
                left,
                right,
                overlap_floor=related_overlap_floor,
                similarity_floor=related_similarity_floor,
            ):
                # Sharing a rare token is how the pair was *found*; it is not
                # evidence the two records are about the same thing. Without this
                # gate a pair that shares one symptom word reaches
                # ``_disagreement``, scores below the overlap floor and gets
                # recorded as a refutation - inventing the contradictions the
                # sweep exists to find.
                continue
            score = similarity(left.content, right.content)
            # Two *analyses* are required before a disagreement is evidence. An
            # empty run set is disjoint from everything, so without this guard an
            # imported record with no provenance would contradict every neighbour
            # it shares a subject with.
            independent = bool(
                left.source_run_ids
                and right.source_run_ids
                and left.source_run_ids.isdisjoint(right.source_run_ids)
            )
            # Checked before the duplicate floor on purpose: "可排除" and "确认为"
            # differ by a few characters, so a pair that flatly disagrees can
            # otherwise score above 0.92 and be folded into one record, deleting
            # the contradiction from the corpus.
            disagreement = (
                _disagreement(
                    left,
                    right,
                    score=score,
                    conflict_ceiling=conflict_ceiling,
                    overlap_floor=overlap_floor,
                )
                if independent
                else None
            )
            if disagreement is not None:
                report.contradictions.append(
                    ConflictPair(
                        left=left.memory_id,
                        right=right.memory_id,
                        relation=MemoryRelation.CONTRADICTS,
                        similarity=score,
                        reason=disagreement,
                    )
                )
                continue
            if score >= duplicate_floor:
                report.duplicates.append(
                    ConflictPair(
                        left=left.memory_id,
                        right=right.memory_id,
                        relation=MemoryRelation.DUPLICATE_OF,
                        similarity=score,
                        reason=(
                            f"same topic, content similarity {score:.2f} "
                            f">= {duplicate_floor:.2f}"
                        ),
                    )
                )
                continue
            if independent:
                # Everything that survived the checks above: related subjects,
                # disjoint runs, not alike enough to fold, no signal that the two
                # actually disagree. That is corroboration, and the ``supports``
                # link is what lets the gate count the *other* record's runs
                # toward this one's promotion threshold.
                report.supports.append(
                    ConflictPair(
                        left=left.memory_id,
                        right=right.memory_id,
                        relation=MemoryRelation.SUPPORTS,
                        similarity=score,
                        reason=(
                            "independent runs agree on the same topic "
                            f"(similarity {score:.2f}, overlap "
                            f"{overlap_coefficient(left.content, right.content):.2f}, "
                            f"no disagreement signal)"
                        ),
                    )
                )
        return report

    def resolve(self, report: ConflictReport) -> ConflictReport:
        """Persist links and apply supersession/demotion for a scanned report."""
        for pair in report.duplicates:
            self._supersede(pair, report)
        for pair in report.contradictions:
            self._contradict(pair, report)
        for pair in report.supports:
            self._support(pair, report)
        return report

    def run(self) -> ConflictReport:
        """Scan and resolve in one call; the shape the dreaming beat uses."""
        return self.resolve(self.scan())

    # ------------------------------------------------------------------ internals
    def _candidate_pairs(
        self, report: ConflictReport | None = None
    ) -> list[tuple[CuratedMemory, CuratedMemory]]:
        """Pairs worth judging, found by blocking instead of by a full scan.

        Retired records are excluded: a superseded or archived memory is already
        out of recall, and comparing against it would only manufacture
        contradictions out of history.

        The result is ordered by memory id so one corpus always yields one report.
        Set iteration order is not stable across processes - string hashing is
        randomised - and the block caps below keep a prefix, so an unordered
        traversal would make *which* records got compared depend on the seed.
        """
        live: list[CuratedMemory] = []
        for memory in self._store.list_curated(active_only=False):
            if memory.status in RETIRED_STATUSES:
                continue
            if report is not None:
                report.scanned += 1
            live.append(memory)

        scopes: dict[str, list[CuratedMemory]] = {}
        for memory in live:
            scopes.setdefault(memory.scenario_type or "", []).append(memory)

        blocks: list[list[CuratedMemory]] = []
        for scope in scopes.values():
            blocks.extend(_blocks(scope))
        # Newest block first, so ``_MAX_PAIRS_PER_SWEEP`` is spent on records that
        # were written recently and are still waiting on corroboration rather than
        # on ones that settled beats ago.
        blocks.sort(key=lambda block: _recency_key(block[0]), reverse=True)

        found: dict[tuple[str, str], tuple[CuratedMemory, CuratedMemory]] = {}
        for block in blocks:
            if report is not None:
                report.groups += 1
            for index, left in enumerate(block):
                for right in block[index + 1 :]:
                    if len(found) >= _MAX_PAIRS_PER_SWEEP:
                        return _ordered_pairs(found)
                    first, second = (
                        (left, right)
                        if left.memory_id < right.memory_id
                        else (right, left)
                    )
                    found.setdefault(
                        (first.memory_id, second.memory_id), (first, second)
                    )
        return _ordered_pairs(found)

    def _supersede(self, pair: ConflictPair, report: ConflictReport) -> None:
        """Fold the weaker of two near-identical records into the stronger one.

        Merging, not just hiding. Section 5.3 asks for the evidence to be
        combined, and two independent runs that reached the same conclusion *are*
        corroboration: the loser's runs move to the winner, the winner is
        re-gated on that enlarged evidence, and only then is the loser retired.

        The order is the point. Gating the loser first let it be promoted to
        ``active`` - it has evidence, so the verdict said so - and then
        immediately overwritten by ``superseded``, leaving a version history that
        claims the corpus changed its mind twice inside one sweep.
        """
        left = self._store.load_curated(pair.left)
        right = self._store.load_curated(pair.right)
        if left is None or right is None:
            return
        if left.status in RETIRED_STATUSES or right.status in RETIRED_STATUSES:
            return
        winner, loser = (left, right) if _outranks(left, right) else (right, left)
        evidence = json.dumps(pair.as_dict(), ensure_ascii=False)

        self._store.link(
            winner.memory_id,
            loser.memory_id,
            MemoryRelation.SUPERSEDES.value,
            evidence=evidence,
        )
        self._store.link(
            loser.memory_id,
            winner.memory_id,
            MemoryRelation.DUPLICATE_OF.value,
            evidence=evidence,
        )
        self._absorb(winner, loser, report)
        self._retire(loser, winner.memory_id, pair.reason, report)

    def _absorb(
        self,
        winner: CuratedMemory,
        loser: CuratedMemory,
        report: ConflictReport,
    ) -> None:
        """Move the loser's evidence onto the winner, then re-gate the winner.

        Idempotent by construction: once the loser's references are a subset of
        the winner's, ``merged == winner`` and nothing is written, so a sweep
        that re-finds the same pair (it cannot, because the loser is retired -
        but a partially applied sweep can) costs nothing.
        """
        merged = winner.model_copy(
            update={
                "provenance": Provenance(
                    source_type=winner.provenance.source_type,
                    references=list(
                        dict.fromkeys(
                            [
                                *winner.provenance.references,
                                *loser.provenance.references,
                            ]
                        )
                    ),
                    created_by=winner.provenance.created_by,
                    validated_by=(
                        winner.provenance.validated_by
                        or loser.provenance.validated_by
                    ),
                ),
                "confirmed_run_ids": list(
                    dict.fromkeys(
                        [*winner.confirmed_run_ids, *loser.confirmed_run_ids]
                    )
                ),
                "confidence": max(winner.confidence, loser.confidence),
                "severity": max_severity(winner.severity, loser.severity),
            }
        )
        if merged == winner:
            return
        # ``revise`` rather than ``write_curated``: absorbing a duplicate is a
        # governance edit, and the version row has to say so.
        self._store.revise(
            merged,
            reason=f"merge: absorbed duplicate {loser.memory_id}",
            actor=self._actor,
        )
        current = self._store.load_curated(winner.memory_id)
        if current is None:
            return
        transition = commit_gate(self._store, current, actor=self._actor)
        if transition is not None:
            report.transitions.append(transition)

    def _retire(
        self,
        loser: CuratedMemory,
        winner_id: str,
        reason: str,
        report: ConflictReport,
    ) -> None:
        """Point a record at its successor and take it out of recall."""
        if loser.superseded_by != winner_id:
            # ``import_curated`` because pointing at a successor is metadata: it
            # must not restamp ``updated_at`` (freshness is a ranking signal) and
            # must not add a version row that says nothing changed.
            self._store.import_curated(
                loser.model_copy(update={"superseded_by": winner_id})
            )
        if loser.status is MemoryStatus.SUPERSEDED:
            return
        if not self._store.set_trust(
            loser.memory_id,
            MemoryStatus.SUPERSEDED,
            reason=f"duplicate of {winner_id} ({reason})",
            actor=self._actor,
        ):
            return
        # The gate never retires a record, so there is no verdict to copy here.
        # The transition is reported explicitly to keep section 7's rule
        # checkable: every trust change reaches ``report.transitions``, and from
        # there the ``memory.trust`` event and ``run.json``.
        report.transitions.append(
            TrustTransition(
                memory_id=loser.memory_id,
                previous=loser.status,
                current=MemoryStatus.SUPERSEDED,
                reason=f"duplicate of {winner_id}",
                actor=self._actor,
            )
        )
        if loser.memory_id not in report.superseded:
            report.superseded.append(loser.memory_id)

    def _contradict(self, pair: ConflictPair, report: ConflictReport) -> None:
        """Record a refutation and let the gate decide what it costs."""
        evidence = json.dumps(pair.as_dict(), ensure_ascii=False)
        for memory_id in (pair.left, pair.right):
            before = contradictions_for(self._store, memory_id)
            other = pair.right if memory_id == pair.left else pair.left
            self._store.link(
                memory_id, other, MemoryRelation.CONTRADICTS.value, evidence=evidence
            )
            after = contradictions_for(self._store, memory_id)
            if after > before:
                self._penalise(memory_id, other, report)

        for memory_id in (pair.left, pair.right):
            current = self._store.load_curated(memory_id)
            if current is None:
                continue
            transition = commit_gate(self._store, current, actor=self._actor)
            if transition is not None:
                report.transitions.append(transition)

    def _support(self, pair: ConflictPair, report: ConflictReport) -> None:
        """Record corroboration between two independent records, then re-gate.

        A ``supports`` link is the band between a paraphrase (folded as a
        duplicate) and a disagreement (recorded as a contradiction): same topic,
        materially similar content, disjoint runs. Neither supersession nor a
        confidence penalty applies - the two records stay separate and keep
        their own confidence - but the link is the evidence the gate reads to
        count the *other* record's runs toward this one's promotion threshold.
        That is what lets two analyses which worded the same conclusion
        differently corroborate each other instead of both lingering as
        single-run ``candidate`` records forever.

        Bidirectional, so the sweep can gate either side on the next beat even
        if only one of them is re-examined. Idempotent: ``link`` upserts and
        ``commit_gate`` is a no-op once the verdict has settled.
        """
        evidence = json.dumps(pair.as_dict(), ensure_ascii=False)
        self._store.link(
            pair.left, pair.right, MemoryRelation.SUPPORTS.value, evidence=evidence
        )
        self._store.link(
            pair.right, pair.left, MemoryRelation.SUPPORTS.value, evidence=evidence
        )
        for memory_id in (pair.left, pair.right):
            current = self._store.load_curated(memory_id)
            if current is None:
                continue
            transition = commit_gate(self._store, current, actor=self._actor)
            if transition is not None:
                report.transitions.append(transition)

    def _penalise(
        self, memory_id: str, other_id: str, report: ConflictReport
    ) -> None:
        """Halve the weaker side's confidence on a *newly recorded* refutation.

        Only the weaker record pays, so a well-corroborated pattern is not
        punished for one dissenting run - but it does get a contradiction link,
        and a second dissent archives it through the gate.
        """
        memory = self._store.load_curated(memory_id)
        other = self._store.load_curated(other_id)
        if memory is None or other is None:
            return
        if _outranks(memory, other):
            # This is the stronger side, so it keeps its confidence. The pair is
            # reviewed from both directions in :meth:`_contradict`, and that is
            # what makes exactly one side pay. The comparison used to be written
            # the other way round, which halved the *corroborated* record and
            # left the single-run guess at full confidence - the precise inversion
            # of what section 5.2 asks for.
            return
        if not _outranks(other, memory) and memory.memory_id < other.memory_id:
            # Total tie. Unreachable while the rank key keeps its id tiebreak,
            # but if it ever is reachable the larger id must pay, exactly once.
            return
        penalised = memory.model_copy(
            update={
                "confidence": round(
                    memory.confidence * CONFLICT_CONFIDENCE_PENALTY, 6
                )
            }
        )
        self._store.revise(
            penalised,
            reason=f"conflict: contradicted by {other_id}",
            actor=self._actor,
        )
        if memory_id not in report.demoted:
            report.demoted.append(memory_id)


def _recency_key(memory: CuratedMemory) -> tuple[str, str]:
    return (memory.updated_at.isoformat(), memory.memory_id)


def salient_tokens(memory: CuratedMemory) -> set[str]:
    """Tokens identifying what a record is *about*, for blocking only.

    Taken from the topic rather than the content: the topic is the claim's
    subject, while the content is where wording drifts most between runs, and
    blocking on drifting text is how two records about different things get
    compared at all. A record with no topic falls back to its content, so it
    stays visible to the sweep instead of becoming permanently uncomparable.
    """
    source = memory.topic or memory.content
    return {
        token for token in tokenize(source) if len(token) >= _MIN_BLOCK_TOKEN_LEN
    }


def related_topics(
    left: CuratedMemory,
    right: CuratedMemory,
    *,
    overlap_floor: float,
    similarity_floor: float,
) -> bool:
    """Whether two records share a subject closely enough to be judged together.

    Both floors must hold, and they fail in opposite directions. The overlap
    coefficient asks whether the shorter subject is largely covered by the longer
    one, which is the question a paraphrase answers. Jaccard asks whether the two
    are substantially alike, which a two-token topic contained in a paragraph
    cannot fake: it scores 1.0 on overlap and almost nothing on Jaccard.

    An exact topic match scores 1.0 on both, so every pair the old fingerprint
    blocking admitted is still admitted here. This widens the sweep; it does not
    redirect it.
    """
    left_topic = left.topic or left.content
    right_topic = right.topic or right.content
    return (
        overlap_coefficient(left_topic, right_topic) >= overlap_floor
        and similarity(left_topic, right_topic) >= similarity_floor
    )


def _blocks(scope: Sequence[CuratedMemory]) -> list[list[CuratedMemory]]:
    """Salient-token blocks for one scope, each recency-ordered and capped.

    One invariant outranks every tuning knob here: **a record with a subject is
    never left out of the sweep.** Blocking decides what is *cheap* to compare,
    not what is compared at all, and both defects this function has had were
    silent violations of that - exact topic fingerprints putting 53 records into
    53 singleton buckets, and then a document-frequency cap discarding the one
    subject three records shared. Neither raised, neither changed a verdict the
    sweep did reach; both made the sweep report success while comparing nothing,
    which is why the rule is stated rather than left to the caps below.
    """
    if len(scope) < 2:
        return []
    tokens = {memory.memory_id: salient_tokens(memory) for memory in scope}
    frequency: Counter[str] = Counter()
    for token_set in tokens.values():
        frequency.update(token_set)
    max_df = max(_MIN_BLOCK_DF, math.ceil(_MAX_BLOCK_DF_RATIO * len(scope)))
    by_id = {memory.memory_id: memory for memory in scope}

    index: dict[str, list[str]] = {}
    for memory_id, token_set in sorted(tokens.items()):
        usable = sorted(
            token for token in token_set if frequency[token] <= max_df
        )
        if usable:
            for token in usable:
                index.setdefault(token, []).append(memory_id)
        elif token_set:
            # Every subject word is "boilerplate" by the cap above, which in a
            # small or homogeneous scope means it is simply *the* subject. Index
            # the rarest one anyway: the cap exists to keep a token that covers
            # the whole scope from pairing everything with everything, and a
            # record that would otherwise be invisible is not that problem. The
            # ``(frequency, token)`` key is a total order, so which token that is
            # never depends on set iteration order.
            rarest = min(token_set, key=lambda token: (frequency[token], token))
            index.setdefault(rarest, []).append(memory_id)

    blocks: list[list[CuratedMemory]] = []
    for member_ids in index.values():
        if len(member_ids) < 2:
            continue
        members = [by_id[member_id] for member_id in member_ids]
        members.sort(key=_recency_key, reverse=True)
        blocks.append(members[:_MAX_GROUP_SIZE])
    return blocks


def _ordered_pairs(
    found: dict[tuple[str, str], tuple[CuratedMemory, CuratedMemory]]
) -> list[tuple[CuratedMemory, CuratedMemory]]:
    return sorted(
        found.values(),
        key=lambda pair: (pair[0].memory_id, pair[1].memory_id),
    )


def _disagreement(
    left: CuratedMemory,
    right: CuratedMemory,
    *,
    score: float,
    conflict_ceiling: float,
    overlap_floor: float,
) -> str | None:
    """Why two independent same-topic records disagree, or ``None`` if they do not.

    Two signals, either one sufficient:

    *Polarity* is the strong one and ignores similarity entirely. "主线程阻塞可
    排除" against "根因确认为主线程阻塞" is a refutation no matter how much text
    the two share, and treating it as a near-duplicate would erase it.

    *Substance* is the fallback, and it is deliberately two-tiered. Records above
    the conflict ceiling are saying similar things, so they are not read as a
    disagreement even if the wording drifted. Below it, the overlap coefficient
    decides: two conclusions that share almost nothing are describing different
    mechanisms (main-thread block versus GPU crash), while two that share most of
    the shorter one are the same conclusion written twice. Only the first is
    evidence against a memory.

    Returning the reason rather than a bool keeps the explanation that lands in
    ``memory_versions`` and the ``memory conflicts`` output next to the decision
    that produced it.
    """
    if polarity_conflict(left.content, right.content):
        return (
            "independent runs take opposite sides on the same topic "
            f"(one rules the cause out, the other asserts it; similarity {score:.2f})"
        )
    if score >= conflict_ceiling:
        return None
    overlap = overlap_coefficient(left.content, right.content)
    if overlap < overlap_floor:
        return (
            "independent runs describe different mechanisms for the same topic "
            f"(similarity {score:.2f}, overlap {overlap:.2f} < {overlap_floor:.2f})"
        )
    return None


def _outranks(left: CuratedMemory, right: CuratedMemory) -> bool:
    """Whether ``left`` should survive a duplicate merge.

    Ordered by trust, then corroboration, then confidence, then recency, with
    the id as a final tiebreak so the outcome never depends on listing order.
    """
    return _rank_key(left) > _rank_key(right)


def _rank_key(memory: CuratedMemory) -> tuple[int, int, float, str, str]:
    return (
        _TRUST_RANK.get(memory.status, 0),
        memory.distinct_runs,
        memory.confidence,
        memory.updated_at.isoformat(),
        # Negated lexicographically: on a total tie the *smaller* id wins, so
        # the key inverts the string ordering to keep ``>`` meaningful.
        "".join(chr(0x10FFFF - ord(char)) for char in memory.memory_id[:32]),
    )


__all__ = [
    "CONFLICT_ACTOR",
    "CONFLICT_CONFIDENCE_PENALTY",
    "ConflictDetector",
    "ConflictPair",
    "ConflictReport",
    "related_topics",
    "salient_tokens",
]
