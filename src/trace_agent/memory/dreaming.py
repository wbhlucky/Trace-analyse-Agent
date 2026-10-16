"""The background beat: where memory governance actually runs.

Analysis completion enqueues a durable job; a later beat drains the queue. That
split is what keeps consolidation off the hot path, and it is also what makes
governance safe to be slow: a conflict sweep over a large corpus may take
seconds, which is unacceptable inside a run and free at 3am.

Eight job kinds, and their order inside one beat is deliberate:

``conflicts`` first, because it *produces* the ``contradicts`` links the gate
reads as evidence. ``gate_review``/``deeppromote`` next, which judge that
evidence. ``decay`` after, which retires what neither could save. ``summarize``
then mints the session digest, ``project`` rolls the whole namespace up into
its own, and ``embed`` runs last so it vectors whatever
this beat just wrote. Both digests sit after the governance jobs on purpose:
they report trust counts, and a digest rendered before the gate ran would
publish a distribution this very beat is about to change. Firing them out of
order still converges
on the next beat, but a single beat could then report a promotion that the very
next beat reverses.

Nothing here decides trust itself. Every verdict comes from
:mod:`trace_agent.memory.lifecycle` and is persisted through the promoter, so
section 7's rule - a transition is only ever made by the gate, and only ever
with a reason and an actor - has exactly one place to be violated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trace_agent.memory.consolidation import ConsolidationService
from trace_agent.memory.embedding.index import SemanticIndex
from trace_agent.memory.lifecycle.conflicts import ConflictDetector
from trace_agent.memory.lifecycle.promoter import Promoter, TrustTransition
from trace_agent.memory.lifecycle.retention import RetentionService
from trace_agent.memory.ports import MemoryStore
from trace_agent.memory.project import ProjectDigestService, ProjectOutcome
from trace_agent.memory.summarize import SessionSummarizer, SummaryOutcome

# Job kinds are wire-visible: they are persisted in the dream queue, so an
# upgraded process must still be able to drain a queue written by the previous
# release. Unknown kinds count as ``skipped`` rather than raising.
JOB_EPISODE = "episode"
JOB_GATE_REVIEW = "gate_review"
JOB_DEEP_PROMOTE = "deeppromote"
JOB_CONFLICTS = "conflicts"
JOB_DECAY = "decay"
JOB_SUMMARIZE = "summarize"
JOB_PROJECT = "project"
JOB_EMBED = "embed"

# Trust levels that mean "out of recall", used to split the archive counter.
_RETIRED_VALUES = frozenset({"archived", "superseded"})

# How many sessions one ``summarize`` sweep may digest. Bounded because the beat
# runs on a timer and a corpus with thousands of idle sessions should not turn
# one tick into a full scan.
_SUMMARIZE_LIMIT = 20


@dataclass(slots=True)
class DreamBatch:
    """What one beat did, split by the kind of change it made.

    The fields are separate because they answer separate operational questions:
    ``created`` is "did consolidation mint anything", ``transitions`` is "did any
    trust move, and on whose authority", ``conflicts``/``duplicates``/``supports``
    is "does the corpus disagree with, repeat, or corroborate itself",
    ``expired`` is "did retention pull stale priors out of recall". One combined
    counter would make all of them invisible, which is roughly how the P0 defect
    - a single run reaching ``active`` - survived as long as it did.
    """

    processed: int = 0
    created: list[str] = field(default_factory=list)
    skipped: int = 0
    errors: int = 0
    #: Memory ids whose trust moved. Kept as a flat list because that is what
    #: callers had before ``transitions`` existed; the structured form carries
    #: the from/to/reason/actor that observability needs.
    trust_changed: list[str] = field(default_factory=list)
    transitions: list[TrustTransition] = field(default_factory=list)
    archived: list[str] = field(default_factory=list)
    conflicts: int = 0
    duplicates: int = 0
    supports: int = 0
    superseded: list[str] = field(default_factory=list)
    expired: int = 0
    stamped: int = 0
    summarized: list[str] = field(default_factory=list)
    #: Namespaces whose project digest this beat rewrote. Kept apart from
    #: ``summarized`` because they answer different questions - "which
    #: conversations did we digest" versus "did the corpus roll-up move" - and a
    #: beat that refreshed only the project record still did work worth seeing.
    projects: list[str] = field(default_factory=list)
    #: Records this beat handed to the embedding provider. Kept apart from
    #: ``created`` because embedding is not authorship: a beat that only
    #: backfills vectors for an already-consolidated corpus still did work, and
    #: folding it into ``created`` would make "did we mint a memory" unreadable.
    embedded: int = 0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe projection for events and the dashboard."""
        return {
            "processed": self.processed,
            "created": list(self.created),
            "skipped": self.skipped,
            "errors": self.errors,
            "trust_changed": list(self.trust_changed),
            "transitions": [item.as_dict() for item in self.transitions],
            "archived": list(self.archived),
            "conflicts": self.conflicts,
            "duplicates": self.duplicates,
            "supports": self.supports,
            "superseded": list(self.superseded),
            "expired": self.expired,
            "stamped": self.stamped,
            "summarized": list(self.summarized),
            "projects": list(self.projects),
            "embedded": self.embedded,
        }

    def absorb(self, other: "DreamBatch") -> None:
        """Fold another batch into this one, counter by counter.

        :meth:`DreamingService.drain_inline` reports one number for two passes,
        and merging the dicts instead would lose exactly the split ``as_dict``
        exists to preserve - what the queue produced versus what the sweep
        produced. Hence the explicit field-by-field merge.
        """
        self.processed += other.processed
        self.created.extend(other.created)
        self.skipped += other.skipped
        self.errors += other.errors
        self.trust_changed.extend(other.trust_changed)
        self.transitions.extend(other.transitions)
        self.archived.extend(other.archived)
        self.conflicts += other.conflicts
        self.duplicates += other.duplicates
        self.supports += other.supports
        self.superseded.extend(other.superseded)
        self.expired += other.expired
        self.stamped += other.stamped
        self.summarized.extend(other.summarized)
        self.projects.extend(other.projects)
        self.embedded += other.embedded


class DreamingService:
    """Delayed background consolidation and governance for the memory loop."""

    def __init__(
        self,
        backend: MemoryStore,
        *,
        semantic: SemanticIndex | None = None,
    ) -> None:
        self._backend = backend
        # The vector index is shared with recall: ``MemoryRuntime`` builds one
        # ``SemanticIndex`` and hands it to both this beat and the recall
        # service, so a query vector cached on the read side is embedded with
        # the same recipe the beat backfills the corpus with. ``None`` - or an
        # index whose ``available`` is False - makes every embed job a no-op and
        # leaves recall exactly the lexical recall it was before P3.
        self._semantic = semantic
        self._consolidation = ConsolidationService(backend)
        self._promoter = Promoter(backend)
        self._conflicts = ConflictDetector(backend)
        self._retention = RetentionService(backend)
        self._summarizer = SessionSummarizer(backend)
        self._projects = ProjectDigestService(backend)

    @property
    def semantic(self) -> SemanticIndex | None:
        """The embedding index this beat backfills, when one is wired."""
        return self._semantic

    # ------------------------------------------------------------------ queue
    def enqueue_episode(self, episode_id: str) -> None:
        """Queue candidate production for one episode."""
        self._backend.append_dream_job(
            {
                "kind": JOB_EPISODE,
                "episode_id": episode_id,
            }
        )

    def enqueue_gate_review(self, episode_id: str) -> None:
        """Queue a trust re-evaluation for memories derived from one episode.

        Unlike :meth:`enqueue_episode` this produces no candidates and cannot
        inflate ``occurrences``; it only asks the quality gate to re-read the
        evidence already on disk. That is what makes a beat fired immediately
        after consolidation safe, where a second ``episode`` job used to look
        like an independent confirmation of the same run.
        """
        self._backend.append_dream_job(
            {
                "kind": JOB_GATE_REVIEW,
                "episode_id": episode_id,
            }
        )

    def enqueue_deep_promote(self, *, scenario_type: str | None = None) -> None:
        """Queue a corpus-wide gate sweep, optionally narrowed to one scenario."""
        self._backend.append_dream_job(
            {
                "kind": JOB_DEEP_PROMOTE,
                "scenario_type": scenario_type,
            }
        )

    def enqueue_conflicts(self) -> None:
        """Queue a duplicate/contradiction sweep."""
        self._backend.append_dream_job({"kind": JOB_CONFLICTS})

    def enqueue_decay(self) -> None:
        """Queue retention: stamp deadlines, then archive what passed them."""
        self._backend.append_dream_job({"kind": JOB_DECAY})

    def enqueue_summarize(self, session_id: str | None = None) -> None:
        """Queue session digests - one session, or every stale one."""
        self._backend.append_dream_job(
            {"kind": JOB_SUMMARIZE, "session_id": session_id}
        )

    def enqueue_project_digest(self) -> None:
        """Queue a refresh of this namespace's project digest.

        There is no argument to narrow it: a store is bound to one namespace, so
        "the project" is unambiguous, and a beat that swept every namespace would
        be reading corpora the run it belongs to has no business touching.
        """
        self._backend.append_dream_job({"kind": JOB_PROJECT})

    def enqueue_embed(self, *, limit: int | None = None) -> None:
        """Queue an embedding backfill for records missing a vector.

        Enqueued after a run so the episode and candidates it just wrote become
        semantically recallable by the next analysis, without putting a provider
        call on the path the user is waiting on. When the semantic index is
        unavailable the job still drains - as a no-op - which keeps the queue
        shape stable across a deployment that toggles embeddings on and off.
        """
        self._backend.append_dream_job({"kind": JOB_EMBED, "limit": limit})

    # ------------------------------------------------------------------- runs
    def maintain(self) -> DreamBatch:
        """Run the full governance chain once, bypassing the queue.

        This is what ``memory maintain`` and the dashboard's periodic tick call.
        It exists separately from :meth:`run_once` because governance has to be
        runnable on demand - after a migration, after an import, when an operator
        notices two memories disagreeing - and "wait until somebody enqueues the
        right job" is not on demand.
        """
        batch = DreamBatch()
        self._run_conflicts(batch)
        self._run_gate(batch)
        self._run_decay(batch)
        self._run_summarize(batch, session_id=None)
        self._run_project(batch)
        self._run_embed(batch)
        batch.processed += 1
        return batch

    def run_once(self, *, limit: int = 20) -> DreamBatch:
        """Drain up to ``limit`` queued jobs. Idempotent and best-effort."""
        batch = DreamBatch()
        jobs = self._backend.read_dream_jobs(limit=limit)
        if not jobs:
            return batch
        self._backend.clear_dream_jobs()

        for job in jobs:
            try:
                kind = job.get("kind")
                if kind == JOB_EPISODE:
                    self._run_episode(batch, str(job.get("episode_id") or ""))
                elif kind == JOB_DEEP_PROMOTE:
                    raw = job.get("scenario_type")
                    self._run_gate(
                        batch,
                        scenario_type=str(raw) if raw else None,
                    )
                    batch.processed += 1
                elif kind == JOB_GATE_REVIEW:
                    self._run_gate(
                        batch, episode_id=str(job.get("episode_id") or "")
                    )
                    batch.processed += 1
                elif kind == JOB_CONFLICTS:
                    self._run_conflicts(batch)
                    batch.processed += 1
                elif kind == JOB_DECAY:
                    self._run_decay(batch)
                    batch.processed += 1
                elif kind == JOB_SUMMARIZE:
                    raw = job.get("session_id")
                    self._run_summarize(
                        batch, session_id=str(raw) if raw else None
                    )
                    batch.processed += 1
                elif kind == JOB_PROJECT:
                    self._run_project(batch)
                    batch.processed += 1
                elif kind == JOB_EMBED:
                    raw = job.get("limit")
                    self._run_embed(
                        batch, limit=int(raw) if isinstance(raw, int) else None
                    )
                    batch.processed += 1
                else:
                    batch.skipped += 1
            except Exception:
                # One bad job must not strand the rest of the queue: the beat
                # has already cleared it, so anything not counted here is lost.
                batch.errors += 1
        return batch

    def drain_inline(self, *, limit: int = 20, maintain: bool = True) -> DreamBatch:
        """Governance for a process that has no background beat.

        The dashboard drains the queue every 30s and sweeps the corpus every
        ``memory_maintain_every()`` ticks. A CLI or eval run has neither, so the
        jobs it enqueued waited for a process that never came, and a corpus built
        that way stayed permanently ``candidate`` - not because the evidence was
        missing, but because nothing ever read it. Two runs that agreed on a
        conclusion were both single-run guesses forever, since the ``supports``
        link that would have promoted them is produced by the sweep.

        Order matters for the same reason it does inside a beat: draining first
        gates *this* run's episode on the evidence already stored, and the sweep
        afterwards links it to earlier runs, which is the half that can actually
        move trust. Both halves are best-effort, and ``maintain=False`` leaves the
        cheap queue drain available on its own.
        """
        batch = DreamBatch()
        try:
            batch.absorb(self.run_once(limit=limit))
        except Exception:
            batch.errors += 1
        if not maintain:
            return batch
        try:
            batch.absorb(self.maintain())
        except Exception:
            batch.errors += 1
        return batch

    # ------------------------------------------------------------------- jobs
    def _run_episode(self, batch: DreamBatch, episode_id: str) -> None:
        episode = self._backend.load_episode(episode_id)
        if episode is None:
            batch.skipped += 1
            return
        created = self._consolidation.consolidate(episode)
        batch.created.extend(item.memory_id for item in created)
        batch.processed += 1

    def _run_gate(
        self,
        batch: DreamBatch,
        *,
        episode_id: str | None = None,
        scenario_type: str | None = None,
    ) -> None:
        """Persist gate verdicts through the audited path.

        Delegated to :class:`~trace_agent.memory.lifecycle.promoter.Promoter`
        instead of computed here because the promoter is the only component
        allowed to move trust. This method used to recompute the verdict and
        save it with a plain ``write_curated``, which recorded every transition
        as ``"consolidate"`` - correct decisions, unauditable ones.
        """
        try:
            transitions = (
                self._promoter.review_episode(episode_id)
                if episode_id
                else self._promoter.sweep(scenario_type=scenario_type)
            )
        except Exception:
            batch.errors += 1
            return
        self._absorb_transitions(batch, transitions)

    def _run_conflicts(self, batch: DreamBatch) -> None:
        try:
            report = self._conflicts.run()
        except Exception:
            batch.errors += 1
            return
        batch.conflicts += len(report.contradictions)
        batch.duplicates += len(report.duplicates)
        batch.supports += len(report.supports)
        batch.superseded.extend(report.superseded)
        self._absorb_transitions(batch, report.transitions)

    def _run_decay(self, batch: DreamBatch) -> None:
        try:
            report = self._retention.run()
        except Exception:
            batch.errors += 1
            return
        batch.expired += report.expired
        batch.stamped += len(report.stamped)
        self._absorb_transitions(batch, report.transitions)

    def _run_summarize(
        self, batch: DreamBatch, *, session_id: str | None
    ) -> None:
        try:
            outcomes: list[SummaryOutcome | None] = (
                [self._summarizer.summarize(session_id)]
                if session_id
                else self._summarizer.sweep(limit=_SUMMARIZE_LIMIT)
            )
        except Exception:
            batch.errors += 1
            return
        for outcome in outcomes:
            if outcome is None:
                batch.skipped += 1
                continue
            if outcome.changed:
                batch.summarized.append(outcome.session_id)
                batch.created.append(outcome.memory_id)

    def _run_project(self, batch: DreamBatch) -> None:
        """Refresh this namespace's project digest.

        No sweep and no limit, unlike ``summarize``: a store is bound to one
        namespace, so there is exactly one digest to consider. What stops a beat
        fired every few minutes from rewriting it is the idempotence check inside
        :class:`~trace_agent.memory.project.ProjectDigestService`, which compares
        rendered text rather than trusting a timestamp.
        """
        try:
            outcome: ProjectOutcome | None = self._projects.digest()
        except Exception:
            batch.errors += 1
            return
        if outcome is None:
            # An empty corpus has nothing to say, and writing an all-zeros digest
            # would be recalled as a prior. Counted as skipped, not as an error.
            batch.skipped += 1
            return
        if outcome.changed:
            batch.projects.append(outcome.namespace)
            batch.created.append(outcome.memory_id)

    def _run_embed(self, batch: DreamBatch, *, limit: int | None = None) -> None:
        """Backfill vectors for records the semantic channel is missing.

        Best-effort like every other job. :meth:`SemanticIndex.backfill` already
        swallows provider and store errors and returns a count, so the ``except``
        is only reachable on a bug in that contract - and even then one failed
        embed must not strand the rest of the beat, whose queue is already gone.
        """
        if self._semantic is None or not self._semantic.available:
            return
        try:
            batch.embedded += self._semantic.backfill(limit=limit)
        except Exception:
            batch.errors += 1

    def _absorb_transitions(
        self, batch: DreamBatch, transitions: list[TrustTransition]
    ) -> None:
        """Fold one component's verdicts into the batch counters."""
        for transition in transitions:
            batch.transitions.append(transition)
            batch.trust_changed.append(transition.memory_id)
            if transition.current.value in _RETIRED_VALUES:
                batch.archived.append(transition.memory_id)
