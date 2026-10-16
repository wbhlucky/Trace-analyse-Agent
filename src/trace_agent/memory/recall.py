from __future__ import annotations

import hashlib
from dataclasses import asdict
from time import perf_counter
from typing import Any, Sequence

from trace_agent.memory.cache import RecallCache, cache_key
from trace_agent.memory.context.composer import (
    compose_memory,
    compose_memory_context,
    episode_body,
)
from trace_agent.memory.embedding.index import SemanticIndex
from trace_agent.memory.flags import (
    memory_candidate_limit,
    memory_include_candidate,
    memory_min_score,
    memory_min_semantic,
    memory_type_quota,
)
from trace_agent.memory.models import (
    TRUSTED_STATUSES,
    CuratedMemory,
    EpisodicMemory,
    MemoryStatus,
    RecallHit,
    RecallResult,
)
from trace_agent.memory.ports import (
    MemoryQuery,
    MemoryStore,
    MemoryType,
    ScoredMemory,
    memory_type_for,
    store_supports_pushdown,
)
from trace_agent.memory.rerank import (
    DEFAULT_IMPORTANCE,
    DEFAULT_WEIGHTS,
    PHRASE_WEIGHT,
    RecallWeights,
    SEMANTIC_WEIGHTS,
    ScoreSignals,
    episode_confidence,
    episode_trust,
    episode_trust_prior,
    freshness,
    importance_for,
    lexical_score,
    trust_prior,
    weighted_score,
)
from trace_agent.memory.search_text import (
    curated_search_text,
    episode_search_text,
)


def _hash_text(*parts: str) -> str:
    seed = "\n".join(parts).encode("utf-8")
    return hashlib.sha1(seed).hexdigest()[:16]


def _hit_key(record: CuratedMemory | EpisodicMemory) -> tuple[str, str]:
    """Identity of a pooled candidate, qualified by which kind it is.

    Curated and episodic records are separate id spaces that share no prefix
    guarantee, and the merged pool has to deduplicate across both channels. A
    bare id would let an episode shadow a memory that happens to be named the
    same, which is a silent dropped hit rather than a visible one.
    """
    if isinstance(record, EpisodicMemory):
        return ("episode", record.episode_id)
    return ("memory", record.memory_id)


def _episode_admitted(
    episode: EpisodicMemory,
    allowed: frozenset[MemoryStatus],
) -> bool:
    """Whether an episode may enter the candidate pool at all.

    No store can answer this in SQL: an episode's trust is derived from its
    findings, not stored in a column, so the filter has to live on the read
    path. One shared predicate across the scan, the index pushdown and the
    vector channel is what makes "an uncorroborated episode stays out unless
    candidate recall is explicitly on" hold no matter which path a recall takes.
    """
    return episode_trust(episode) in allowed


class RecallService:
    """Deterministic conditional recall over curated and episodic memory.

    The service never raises and never performs blocking I/O outside the
    caller-provided backend reads. A failed lookup returns an empty result so
    memory recall can only degrade analysis quality, never stop the task.

    Two rules shape every result here:

    * Only trusted memory is recalled by default. ``candidate`` entries are
      single-run guesses and stay out unless explicitly enabled, which is what
      stops one wrong analysis from becoming the prior for the next one.
      Episodes are held to the same rule through a trust level derived from
      their own findings (see :func:`~trace_agent.memory.rerank.episode_trust`):
      gating only the curated channel left the raw episode as an unguarded back
      door into the prompt.
    * A hit must first prove *relevance*. Trust, confidence and recency only
      reorder hits that already match the query; they can never pull an
      unrelated memory into the context.
    """

    def __init__(
        self,
        backend: MemoryStore,
        *,
        weights: RecallWeights = DEFAULT_WEIGHTS,
        include_candidate: bool | None = None,
        min_score: float | None = None,
        semantic: SemanticIndex | None = None,
        min_semantic: float | None = None,
        cache: RecallCache | None = None,
    ) -> None:
        self._backend = backend
        self._weights = weights
        # A weight table handed in by a caller is a decision, so it survives the
        # presence of a semantic channel. Only the default profile is switched,
        # which is what keeps a caller that pinned lexical-only weights getting
        # lexical-only scores.
        self._weights_pinned = weights is not DEFAULT_WEIGHTS
        # ``None`` means "read the environment on every call", so tests and
        # operators can flip candidate recall without rebuilding the service.
        self._include_candidate = include_candidate
        self._min_score = min_score
        # ``None`` is the normal state: no provider configured, or a store that
        # cannot hold vectors. Every semantic path below degrades to the
        # lexical-only behaviour that existed before P3 rather than to an error.
        self._semantic = semantic
        self._min_semantic = min_semantic
        # ``None`` means no caching at all, not "build one from the environment":
        # a service constructed by a test or by a caller that wants deterministic
        # recall must not inherit whatever ``TRACE_AGENT_MEMORY_CACHE`` happens to
        # say. :class:`~trace_agent.memory.runtime.MemoryRuntime` is the one place
        # that reads the flag, so there is exactly one construction site to audit.
        self._cache = cache

    def _candidate_allowed(self) -> bool:
        if self._include_candidate is None:
            return memory_include_candidate()
        return self._include_candidate

    def _relevance_floor(self) -> float:
        return memory_min_score() if self._min_score is None else self._min_score

    def _semantic_floor(self) -> float:
        """Cosine at which a hit is relevant on semantics alone."""
        return (
            memory_min_semantic() if self._min_semantic is None else self._min_semantic
        )

    def _allowed_statuses(self) -> frozenset[MemoryStatus]:
        allowed = set(TRUSTED_STATUSES)
        if self._candidate_allowed():
            allowed.add(MemoryStatus.CANDIDATE)
        return frozenset(allowed)

    def recall(
        self,
        query: str,
        *,
        scenario_type: str | None = None,
        top_k: int = 5,
        include_episodic: bool = True,
    ) -> RecallResult:
        """Ranked memories for one query, served from cache when nothing changed.

        The cached answer and the computed one are the same object shape, so
        callers cannot tell which path they took - which is the requirement, not
        a nicety: a cache that changed recall ordering under load would be
        indistinguishable from nondeterminism in the retriever.

        ``latency_ms`` is stamped here rather than left to the caller so that
        every consumer of the same work reports the same number: a run, the
        dashboard's recall probe, ``memory stats`` and an eval trial all measure
        their own overhead differently, and only this one measures retrieval. It
        covers the whole call including the cache lookup, because that is how long
        the caller waited. It is deliberately *not* stored in the cache - a hit
        would otherwise replay the latency of the miss that filled it and report a
        cost this call never paid.
        """
        started = perf_counter()
        key = self._cache_key(
            query,
            scenario_type=scenario_type,
            top_k=top_k,
            include_episodic=include_episodic,
        )
        cached = self._cache_get(key)
        if cached is not None:
            # Access still happened, so access time is still stamped. ``touch``
            # deliberately does not advance the corpus epoch in any adapter, which
            # is what keeps this from invalidating the entry it just read.
            self._touch_hits(cached.hits)
            return self._stamp_latency(cached, started)
        result = self._recall(
            query,
            scenario_type=scenario_type,
            top_k=top_k,
            include_episodic=include_episodic,
        )
        self._touch_hits(result.hits)
        self._cache_put(key, result)
        return self._stamp_latency(result, started)

    @staticmethod
    def _stamp_latency(result: RecallResult, started: float) -> RecallResult:
        """``result`` with this call's wall-clock cost on it.

        A copy rather than an assignment: the same object is what went into the
        cache, and mutating it would persist this call's latency as the entry's.
        """
        return result.model_copy(
            update={"latency_ms": round((perf_counter() - started) * 1000.0, 3)}
        )

    def _recall(
        self,
        query: str,
        *,
        scenario_type: str | None = None,
        top_k: int = 5,
        include_episodic: bool = True,
    ) -> RecallResult:
        """The retrieval itself. Never raises; a failure is an empty result."""
        hits: list[RecallHit] = []
        try:
            floor = self._relevance_floor()
            sem_floor = self._semantic_floor()
            pool, similarities = self._hybrid_candidates(
                query,
                scenario_type=scenario_type,
                include_episodic=include_episodic,
            )
            weights = self._weights_for(similarities)
            for candidate in pool:
                semantic = similarities.get(_hit_key(candidate), 0.0)
                if isinstance(candidate, CuratedMemory):
                    hit = self._score_curated(
                        query,
                        candidate,
                        floor=floor,
                        semantic=semantic,
                        sem_floor=sem_floor,
                        weights=weights,
                    )
                else:
                    hit = self._score_episode(
                        query,
                        candidate,
                        floor=floor,
                        semantic=semantic,
                        sem_floor=sem_floor,
                        weights=weights,
                    )
                if hit is not None:
                    hits.append(hit)
        except Exception:
            return RecallResult(query=query, hits=[])

        hits.sort(key=lambda item: item.score, reverse=True)
        top = _apply_type_quota(_dedupe_hits(hits), top_k=top_k)
        composed = compose_memory(top)
        return RecallResult(
            query=query,
            hits=top,
            context_prompt=composed.text,
            injected_ids=composed.injected_ids,
            truncated=composed.truncated,
        )

    # ---------------------------------------------------------------- cache (P4)
    def _cache_key(
        self,
        query: str,
        *,
        scenario_type: str | None,
        top_k: int,
        include_episodic: bool,
    ) -> str | None:
        """Key for this recall, or ``None`` when caching is not in effect.

        Carries the policy fingerprint alongside the arguments, because most of
        what decides a recall result is read from the environment rather than
        passed in: two calls with identical arguments and different
        ``TRACE_AGENT_MEMORY_INCLUDE_CANDIDATE`` give genuinely different answers
        and must not share an entry.
        """
        if self._cache is None:
            return None
        return cache_key(
            self._backend,
            query=query,
            scenario_type=scenario_type or "",
            top_k=int(top_k),
            include_episodic=bool(include_episodic),
            policy=self._policy_fingerprint(),
        )

    def _policy_fingerprint(self) -> dict[str, Any]:
        """Every knob that can change a result without changing the arguments."""
        semantic = self._semantic
        return {
            "include_candidate": self._candidate_allowed(),
            "min_score": self._relevance_floor(),
            "min_semantic": self._semantic_floor(),
            "candidate_limit": memory_candidate_limit(),
            "type_quota": memory_type_quota(),
            "weights_pinned": self._weights_pinned,
            # ``asdict`` rather than ``vars``: the table is a slotted dataclass,
            # so it has no ``__dict__`` to read.
            "weights": asdict(self._weights) if self._weights_pinned else "auto",
            "semantic_available": bool(semantic is not None and semantic.available),
            "semantic_model": semantic.model if semantic is not None else "",
        }

    def _cache_get(self, key: str | None) -> RecallResult | None:
        if key is None or self._cache is None:
            return None
        try:
            return self._cache.get(key)
        except Exception:
            # A broken cache is a slower recall, never a failed one.
            return None

    def _cache_put(self, key: str | None, result: RecallResult) -> None:
        if key is None or self._cache is None:
            return None
        # An empty result is not cached. "Nothing matched" is the answer most
        # likely to be wrong while a corpus is being consolidated, and it is also
        # the cheapest one to recompute.
        if not result.hits:
            return None
        try:
            self._cache.put(key, result)
        except Exception:
            return None
        return None

    def cache_snapshot(self) -> dict[str, Any]:
        """Cache diagnostics for ``memory stats`` and the dashboard."""
        if self._cache is None:
            return {"kind": "none", "entries": 0, "hits": 0, "misses": 0}
        try:
            return dict(self._cache.snapshot())
        except Exception:
            return {"kind": getattr(self._cache, "kind", "unknown"), "error": True}

    def _touch_hits(self, hits: Sequence[RecallHit]) -> None:
        """Stamp ``last_accessed_at`` on the memories that were actually recalled.

        Access time is the only signal separating a memory nobody has looked at
        in a year from one every diagnosis reaches for, and retention needs that
        difference to avoid archiving the corpus's most useful entries. Episodes
        are skipped: the record layout has no such column for them and their
        value is already captured by the memories projected from them.

        Grouped in one transaction when the store offers :meth:`batch`, because
        five separate write transactions per recall is five fsyncs for metadata.
        """
        ids = [
            hit.memory.memory_id
            for hit in hits
            if isinstance(hit.memory, CuratedMemory)
        ]
        if not ids:
            return
        touch = getattr(self._backend, "touch", None)
        if not callable(touch):
            return
        batch = getattr(self._backend, "batch", None)
        try:
            if callable(batch):
                with batch():
                    for memory_id in ids:
                        touch(memory_id)
            else:
                for memory_id in ids:
                    touch(memory_id)
        except Exception:
            # Bookkeeping. Losing it costs a retention decision, not a recall.
            return

    # ------------------------------------------------------------ hybrid (P3)
    def _hybrid_candidates(
        self,
        query: str,
        *,
        scenario_type: str | None,
        include_episodic: bool,
    ) -> tuple[list[CuratedMemory | EpisodicMemory], dict[tuple[str, str], float]]:
        """The lexical pool widened by the vector channel, plus one cosine each.

        Two invariants. A record both channels found appears **once** and carries
        one similarity, so the merged pool cannot score the same memory two
        different ways. And when the semantic channel is unavailable the return is
        exactly what the lexical path produced before P3, in the same order -
        which is what makes "embeddings off" a bit-identical recall rather than a
        slightly different one.
        """
        pool = self._candidates(
            query,
            scenario_type=scenario_type,
            include_episodic=include_episodic,
        )
        vector = self._query_vector(query)
        if vector is None:
            return pool, {}

        similarities: dict[tuple[str, str], float] = {}
        seen = {_hit_key(record) for record in pool}
        allowed = self._allowed_statuses()
        for scored in self._vector_candidates(
            vector,
            query,
            scenario_type=scenario_type,
            include_episodic=include_episodic,
        ):
            key = _hit_key(scored.memory)
            if key in seen:
                continue
            # A channel that widens the pool must not widen the permissions, so
            # the derived-trust rule is re-applied here rather than trusted to
            # the store, which has no column to filter episodes on.
            if isinstance(scored.memory, EpisodicMemory) and not _episode_admitted(
                scored.memory, allowed
            ):
                continue
            seen.add(key)
            pool.append(scored.memory)

        # Cosines are read back from the store for the whole pool rather than
        # taken from ``ScoredMemory.similarity``. One source for the number is
        # what guarantees a lexical hit and a vector hit of the same record agree,
        # and records with no stored vector simply come back absent, i.e. 0.0.
        curated_ids = [
            record.memory_id
            for record in pool
            if isinstance(record, CuratedMemory)
        ]
        episode_ids = [
            record.episode_id
            for record in pool
            if isinstance(record, EpisodicMemory)
        ]
        for record_id, value in self._semantic.similarity_for(vector, curated_ids).items():
            similarities[("memory", record_id)] = value
        for record_id, value in self._semantic.similarity_for(
            vector, episode_ids, episode=True
        ).items():
            similarities[("episode", record_id)] = value
        return pool, similarities

    def _query_vector(self, query: str) -> list[float] | None:
        """Query embedding, or ``None`` when the semantic channel is not live."""
        if self._semantic is None:
            return None
        try:
            return self._semantic.query_vector(query)
        except Exception:
            # ``SemanticIndex`` is documented never to raise; this is here for the
            # same reason the rest of ``recall`` is wrapped - a memory failure must
            # cost recall quality and never an analysis run.
            return None

    def _vector_candidates(
        self,
        vector: list[float],
        query: str,
        *,
        scenario_type: str | None,
        include_episodic: bool,
    ) -> list[ScoredMemory]:
        """Nearest records under the *same* filters the lexical channel uses.

        Trust, scenario and namespace travel in the query object, so the vector
        channel cannot hand the reranker a ``candidate`` record that
        ``include_candidate`` is off, or a cold-start pattern during a frame-drop
        analysis. A channel that widens the pool must not widen the permissions.
        """
        if self._semantic is None:
            return []
        try:
            return list(
                self._semantic.candidates(
                    vector,
                    MemoryQuery(
                        text=query,
                        scenario_type=scenario_type,
                        statuses=self._allowed_statuses(),
                        include_episodic=include_episodic,
                        limit=memory_candidate_limit(),
                    ),
                )
            )
        except Exception:
            return []

    def _weights_for(
        self, similarities: dict[tuple[str, str], float]
    ) -> RecallWeights:
        """Which profile this recall is scored under.

        "A semantic channel exists" is not the right trigger. A corpus whose
        vectors have not been backfilled yet would move 35% of every score onto a
        term that is always zero, compressing all scores by a third and
        reshuffling recall for nothing. The profile is adopted only once at least
        one candidate actually has a cosine, so an unembedded corpus scores
        exactly as it did before P3 and the switch is visible in ``run.json``.
        """
        if self._weights_pinned:
            return self._weights
        if any(value > 0.0 for value in similarities.values()):
            return SEMANTIC_WEIGHTS
        return DEFAULT_WEIGHTS

    def _candidates(
        self,
        query: str,
        *,
        scenario_type: str | None,
        include_episodic: bool,
    ) -> list[CuratedMemory | EpisodicMemory]:
        """Records worth scoring, from the index when it is safe to use one.

        This only decides *which* records reach the scorer. Scoring itself is
        identical on both paths, which is what keeps the JSON and SQLite
        adapters interchangeable rather than approximately equivalent.

        The indexed path needs two conditions. The store must expose ``search``,
        and the relevance floor must sit above ``PHRASE_WEIGHT``: a phrase-only
        match scores exactly that much, so above it every surviving hit has to
        share a real token with the query. The FTS document is built from the
        same fields the scorer reads (see ``memory.search_text``), so under
        those conditions the index cannot miss a hit the scan would have kept.
        Below the floor that guarantee is gone and the scan is used instead.
        """
        allowed = self._allowed_statuses()
        floor = self._relevance_floor()
        if store_supports_pushdown(self._backend) and floor > PHRASE_WEIGHT:
            try:
                scored: list[ScoredMemory] | None = self._backend.search(
                    MemoryQuery(
                        text=query,
                        scenario_type=scenario_type,
                        statuses=allowed,
                        include_episodic=include_episodic,
                        limit=memory_candidate_limit(),
                    )
                )
            except Exception:
                # A broken index must degrade to a scan, never to no memory.
                scored = None
            if scored is not None:
                return [
                    item.memory
                    for item in scored
                    if not isinstance(item.memory, EpisodicMemory)
                    or _episode_admitted(item.memory, allowed)
                ]
        return self._scan_candidates(
            scenario_type=scenario_type,
            include_episodic=include_episodic,
            allowed=allowed,
        )

    def _scan_candidates(
        self,
        *,
        scenario_type: str | None,
        include_episodic: bool,
        allowed: frozenset[MemoryStatus],
    ) -> list[CuratedMemory | EpisodicMemory]:
        """Full-corpus fallback: the JSON adapter, or a floor too low to index.

        A curated memory with no ``scenario_type`` is scenario-agnostic and may
        match any query, so the filter skips the check rather than dropping it.
        """
        out: list[CuratedMemory | EpisodicMemory] = []
        for memory in self._backend.list_curated(active_only=True):
            if memory.status not in allowed:
                continue
            if (
                scenario_type
                and memory.scenario_type
                and memory.scenario_type != scenario_type
            ):
                continue
            out.append(memory)
        if include_episodic:
            for episode in self._backend.list_episodes():
                if scenario_type and episode.scenario_type != scenario_type:
                    continue
                if not _episode_admitted(episode, allowed):
                    continue
                out.append(episode)
        return out

    def _score_curated(
        self,
        query: str,
        memory: CuratedMemory,
        *,
        floor: float,
        semantic: float = 0.0,
        sem_floor: float = 0.0,
        weights: RecallWeights | None = None,
    ) -> RecallHit | None:
        text = curated_search_text(memory)
        lexical = lexical_score(query, text, memory.keywords)
        if lexical < floor and semantic < sem_floor:
            # Admission is an OR, and only relevance is allowed to open it. A
            # paraphrase the tokenizer cannot overlap still clears on cosine; a
            # keyword match on a record with no vector still clears on lexical.
            # What neither may do is let trust or recency pull an unrelated memory
            # into the prompt, which is the rule the whole service is built on.
            return None
        signals = ScoreSignals(
            lexical=lexical,
            semantic=semantic,
            confidence=memory.confidence,
            importance=importance_for(memory),
            freshness=freshness(memory.updated_at),
            trust_prior=trust_prior(memory.status),
        )
        why = (
            f"curated {memory.status.value} "
            f"lex={lexical:.2f} conf={memory.confidence:.2f} "
            f"runs={memory.distinct_runs}"
        )
        if semantic > 0.0:
            why += f" sem={semantic:.2f}"
        return RecallHit(
            memory=memory,
            score=weighted_score(signals, weights or self._weights),
            lexical=lexical,
            semantic=semantic,
            why=why,
        )

    def _score_episode(
        self,
        query: str,
        episode: EpisodicMemory,
        *,
        floor: float,
        semantic: float = 0.0,
        sem_floor: float = 0.0,
        weights: RecallWeights | None = None,
    ) -> RecallHit | None:
        text = episode_search_text(episode)
        lexical = lexical_score(query, text, episode.tags)
        if lexical < floor and semantic < sem_floor:
            return None
        trust = episode_trust(episode)
        signals = ScoreSignals(
            lexical=lexical,
            semantic=semantic,
            confidence=episode_confidence(episode),
            importance=DEFAULT_IMPORTANCE,
            freshness=freshness(episode.created_at),
            trust_prior=episode_trust_prior(episode),
        )
        why = f"episode {trust.value} lex={lexical:.2f}"
        if semantic > 0.0:
            why += f" sem={semantic:.2f}"
        return RecallHit(
            memory=episode,
            score=weighted_score(signals, weights or self._weights),
            lexical=lexical,
            semantic=semantic,
            why=why,
        )

    @staticmethod
    def render_context(hits: list[RecallHit]) -> str:
        """Render ranked hits as the trust-partitioned prompt section.

        Still a static method on the service because callers reach for it
        directly, but the rules live in
        :mod:`trace_agent.memory.context.composer`. One definition of how a
        prior is worded and budgeted, shared by every path that builds a prompt:
        before the composer, "Prior curated project knowledge" was the heading
        for a five-run pattern *and* for a single-run guess, and the model had
        no way to tell them apart.
        """
        return compose_memory_context(hits)


def _dedupe_hits(hits: Sequence[RecallHit]) -> list[RecallHit]:
    """Drop redundant hits so top-k is not spent saying one thing twice.

    A curated memory is the distilled form of the episodes that produced it, so
    when both are recalled the episode adds no information. Near-identical
    topics are collapsed the same way, keeping the highest-scoring one.

    Episodes are collapsed against each other on what they would render to, not
    on identity. Two runs over the same case write two episodes with the same
    scenario and the same conclusion, and the candidate partition only has room
    for two lines - so without this both slots went to one sentence repeated and
    a genuinely different case was dropped. Corroboration is not lost by the
    collapse: "this happened N times" is the gate's output and reaches the
    prompt as a curated memory's ``runs=``, which is a stronger form of the same
    signal than printing the episode twice.
    """
    covered_episodes: set[str] = set()
    for hit in hits:
        if isinstance(hit.memory, CuratedMemory):
            covered_episodes |= hit.memory.source_episode_ids

    kept: list[RecallHit] = []
    seen_topics: set[str] = set()
    for hit in hits:
        memory = hit.memory
        if isinstance(memory, EpisodicMemory):
            if memory.episode_id in covered_episodes:
                continue
            fingerprint = episode_body(memory).strip().lower()
            if fingerprint in seen_topics:
                continue
            seen_topics.add(fingerprint)
        else:
            fingerprint = memory.topic.strip().lower()
            if fingerprint in seen_topics:
                continue
            seen_topics.add(fingerprint)
        kept.append(hit)
    return kept


def _hit_type(memory: CuratedMemory | EpisodicMemory) -> MemoryType:
    """Functional type of a pooled hit, the axis the top-k quota is counted on.

    Episodes are their own type rather than inheriting one from a ``kind`` they
    do not have, so "two cases and three procedures" is expressible.
    """
    if isinstance(memory, EpisodicMemory):
        return MemoryType.EPISODIC
    return memory_type_for(memory.kind)


def _apply_type_quota(
    hits: Sequence[RecallHit],
    *,
    top_k: int,
    quota: int | None = None,
) -> list[RecallHit]:
    """Cut to ``top_k`` giving each functional type a turn first (section 6.3).

    Two passes, and the second one is the reason this is safe to leave on by
    default. The first admits at most ``quota`` hits per type, which is what
    stops a corpus of similar patterns from filling every slot and leaving no
    room for the procedure that says how to investigate. The second fills
    whatever is left with the best deferred hits regardless of type, so a query
    whose only relevant memories are all one type returns exactly what it
    returned before the rule existed. Diversity reorders the cut; it never
    shrinks it.

    ``hits`` must already be score-ordered: both passes keep first-seen order,
    which is score order, and the final sort only undoes the interleaving the
    first pass introduced.
    """
    limit = max(0, top_k)
    if limit == 0:
        return []
    per_type = memory_type_quota() if quota is None else max(0, quota)
    if per_type == 0:
        return list(hits)[:limit]

    chosen: list[RecallHit] = []
    deferred: list[RecallHit] = []
    counts: dict[MemoryType, int] = {}
    for hit in hits:
        kind = _hit_type(hit.memory)
        used = counts.get(kind, 0)
        if used < per_type and len(chosen) < limit:
            counts[kind] = used + 1
            chosen.append(hit)
        else:
            deferred.append(hit)
    for hit in deferred:
        if len(chosen) >= limit:
            break
        chosen.append(hit)
    chosen.sort(key=lambda item: item.score, reverse=True)
    return chosen


def memory_id_for(content: str, *, scenario_type: str | None) -> str:
    scenario = scenario_type or "general"
    return f"mem-{scenario}-{_hash_text(content)}"
