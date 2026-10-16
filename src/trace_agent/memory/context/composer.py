"""Turning ranked hits into the memory section of a system prompt.

Section 6.4 exists because of a wording problem, not a length problem. The old
renderer put every curated hit under one heading - "Prior curated project
knowledge" - and a single-run ``suspected`` guess therefore reached the model
looking exactly like a pattern five independent runs had agreed on. The model
could not tell them apart, and neither could a reviewer reading the transcript
afterwards.

So the composer partitions by trust and gives each partition its own heading,
its own instruction and its own share of the character budget:

``verified``    corroborated or signed off. May influence investigation order.
``active``      earned a second independent run, plus episodic hits. Priors only.
``candidate``   a single run's guess. Capped at two items, explicitly disclaimed.

An episode joins whichever of those partitions its own evidence earns it (see
:func:`~trace_agent.memory.rerank.episode_trust`). Filing every episode under
``active`` handed a single-run ``suspected`` guess the "historical similar case"
heading, which is the mislabelling this module exists to remove - arriving
through the episodic channel instead of the curated one.

Every line carries ``[id | trust | conf | runs]``. That costs characters and
buys attributability: when a run produces a wrong conclusion, the trace shows
which memory was in the prompt and how strongly it was presented, which is the
only way section 14's self-contamination rate can be computed at all.

``conf`` means the same thing on both channels: the record's own confidence.
An episode has no stored confidence, so its line derives one from its findings
(see :func:`~trace_agent.memory.rerank.episode_confidence`) and reports the
rerank result separately as ``score``. Rendering the blended score under the
``conf`` label - which an earlier revision did - made a retrieval artefact read
as a confidence claim, and the two channels disagreed about what the same word
meant.

Budgets are shares of :func:`~trace_agent.memory.flags.memory_prompt_chars`, the
same number ``agent.core.prompts.memory_section`` enforces, so the composer and
the prompt cannot disagree about how much room memory gets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from trace_agent.memory.flags import memory_prompt_chars
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    MemoryStatus,
    RecallHit,
)
from trace_agent.memory.rerank import episode_confidence, episode_trust

# Budget shares per trust partition. They sum to 1.0; ``candidate`` is the
# smallest because it is the only partition whose contents are unverified.
VERIFIED_SHARE = 0.40
ACTIVE_SHARE = 0.45
CANDIDATE_SHARE = 0.15

# Hard cap on candidate lines, independent of budget. Two is enough to seed a
# hypothesis and small enough that a contaminated corpus cannot dominate a
# prompt even when every hit happens to be a guess.
CANDIDATE_ITEM_LIMIT = 2

_ELISION = "- （其余召回项因长度预算被省略）"

# Heading plus the instruction that goes with it. The instruction differs per
# partition on purpose: telling the model "verified" and "guess" in the same
# words is what made them interchangeable.
_HEADINGS: dict[str, str] = {
    "verified": (
        "【已验证规律 verified】经多案例或人工确认，可作为排查顺序参考，"
        "仍须以本次取证为准："
    ),
    "active": (
        "【历史相似案例 active】仅作先验假设，不得直接写入 Draft："
    ),
    "candidate": (
        "【未验证候选 candidate】来自历史单次分析的未验证推测，仅作发散提示；"
        "与本次取证冲突时无条件丢弃："
    ),
}

_KIND_LABELS: dict[str, str] = {
    "pattern": "Pattern",
    "fact": "Fact",
    "procedure": "Procedure",
    "anti-pattern": "Anti-pattern",
    "summary": "Summary",
    # The two derived/observational kinds. Without an entry each would fall
    # through to its raw enum value, which is fine but reads like an unfinished
    # mapping in the prompt the analysis actually sees.
    "profile": "Profile",
    "project": "Project",
}

# Where a derived trust level is rendered. Only the two levels an episode can
# earn appear as keys in practice, but the mapping covers every status so a
# future ``episode_trust`` that returns ``verified`` cannot KeyError inside a
# prompt build - the composer is on the analysis hot path and must not raise.
_PARTITION_FOR: dict[MemoryStatus, str] = {
    MemoryStatus.VERIFIED: "verified",
    MemoryStatus.CONSOLIDATED: "verified",
    MemoryStatus.ACTIVE: "active",
    MemoryStatus.CANDIDATE: "candidate",
    MemoryStatus.SUPERSEDED: "candidate",
    MemoryStatus.ARCHIVED: "candidate",
}


@dataclass(frozen=True, slots=True)
class ComposedMemory:
    """The rendered section plus what actually made it in.

    ``injected_ids`` is the observability half: the prompt is assembled under a
    budget, so "what recall returned" and "what the model saw" are different
    lists. Only the second one can answer whether a memory influenced a run.
    """

    text: str
    injected_ids: list[str]
    chars: int
    budget: int
    truncated: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "chars": self.chars,
            "budget": self.budget,
            "truncated": self.truncated,
            "injected_ids": list(self.injected_ids),
        }


def compose_memory_context(
    hits: Sequence[RecallHit],
    *,
    budget: int | None = None,
) -> str:
    """Render hits as the trust-partitioned memory section (string form)."""
    return compose_memory(hits, budget=budget).text


def compose_memory(
    hits: Sequence[RecallHit],
    *,
    budget: int | None = None,
) -> ComposedMemory:
    """Partition, budget and render. Never raises: memory may degrade, not fail."""
    limit = memory_prompt_chars() if budget is None else int(budget)
    limit = max(0, limit)
    if not hits or limit <= 0:
        return ComposedMemory("", [], 0, limit, False)

    partitions = _partition(hits)
    verified_cap = int(limit * VERIFIED_SHARE)
    active_cap = int(limit * ACTIVE_SHARE)
    candidate_cap = limit - verified_cap - active_cap

    blocks: list[str] = []
    injected: list[str] = []
    truncated = False

    verified_block, verified_ids, verified_left = _render(
        "verified", partitions["verified"], verified_cap
    )
    # Unspent verified budget flows to ``active`` and not to ``candidate``:
    # growing the unverified partition because a well-corroborated one happened
    # to be small is exactly the wrong trade.
    active_block, active_ids, active_left = _render(
        "active", partitions["active"], active_cap + verified_left
    )
    candidate_block, candidate_ids, _ = _render(
        "candidate",
        partitions["candidate"][:CANDIDATE_ITEM_LIMIT],
        candidate_cap,
    )

    for block, ids in (
        (verified_block, verified_ids),
        (active_block, active_ids),
        (candidate_block, candidate_ids),
    ):
        if not block:
            continue
        blocks.append(block)
        injected.extend(ids)

    truncated = len(injected) < len(hits)
    text = "\n\n".join(blocks)
    return ComposedMemory(
        text=text,
        injected_ids=injected,
        chars=len(text),
        budget=limit,
        truncated=truncated or bool(_ELISION in text),
    )


# ------------------------------------------------------------------ internals
def _partition(hits: Sequence[RecallHit]) -> dict[str, list[RecallHit]]:
    """Bucket hits by the trust partition they will be rendered under.

    Episodes have no stored trust column, but they do carry evidence, so their
    level is derived rather than assumed: a corroborated episode is a prior
    hypothesis, an uncorroborated one is a guess and inherits the candidate
    partition's item cap and disclaimer along with it.
    """
    out: dict[str, list[RecallHit]] = {
        "verified": [],
        "active": [],
        "candidate": [],
    }
    for hit in hits:
        memory = hit.memory
        if isinstance(memory, EpisodicMemory):
            out[_PARTITION_FOR[episode_trust(memory)]].append(hit)
            continue
        if memory.status in (MemoryStatus.VERIFIED, MemoryStatus.CONSOLIDATED):
            out["verified"].append(hit)
        elif memory.status is MemoryStatus.CANDIDATE:
            out["candidate"].append(hit)
        else:
            # ``active`` and anything retired that slipped through a caller's
            # filter. Retired records must not be rendered as verified.
            out["active"].append(hit)
    return out


def _render(
    partition: str,
    hits: Sequence[RecallHit],
    cap: int,
) -> tuple[str, list[str], int]:
    """Fill one partition item by item; returns block, ids and unused budget.

    Whole items only. Cutting a prior mid-sentence hands the model a claim it
    cannot evaluate, which is worse than not showing it.
    """
    if not hits or cap <= 0:
        return "", [], max(0, cap)
    heading = _HEADINGS[partition]
    if len(heading) >= cap:
        return "", [], cap

    lines: list[str] = []
    ids: list[str] = []
    used = len(heading)
    elided = False
    for hit in hits:
        line = _render_line(hit)
        # +1 for the joining newline.
        cost = len(line) + 1
        if used + cost > cap:
            elided = True
            continue
        lines.append(line)
        ids.append(_hit_id(hit))
        used += cost
    if not lines:
        return "", [], cap - used
    if elided and used + len(_ELISION) + 1 <= cap:
        lines.append(_ELISION)
    return heading + "\n" + "\n".join(lines), ids, cap - used


def _render_line(hit: RecallHit) -> str:
    memory = hit.memory
    if isinstance(memory, EpisodicMemory):
        return (
            f"- [{memory.episode_id} | episode {episode_trust(memory).value} | "
            f"conf={episode_confidence(memory):.2f} | "
            f"score={hit.score:.2f}] {episode_body(memory)}"
        )
    return f"- [{_marker(memory)}] {memory.topic}: {memory.content}"


def episode_body(episode: EpisodicMemory) -> str:
    """The sentence an episode contributes to the prompt.

    Public because it is also the near-duplicate fingerprint: two episodes that
    render to the same sentence spend budget saying one thing twice, and only
    the renderer knows what that sentence is.
    """
    where = (
        f"{episode.scenario_type} / {episode.target_process}"
        if episode.target_process
        else episode.scenario_type
    )
    cause = (
        episode.root_causes[0]
        if episode.root_causes
        else episode.summary or "no structured cause"
    )
    return f"{where}: {cause}"


def _marker(memory: CuratedMemory) -> str:
    """Inline trust annotation, so a prior's strength is visible in the prompt."""
    label = _KIND_LABELS.get(memory.kind.value, memory.kind.value)
    return (
        f"{memory.memory_id} | {memory.status.value} | {label} | "
        f"conf={memory.confidence:.2f} | runs={memory.distinct_runs}"
    )


def _hit_id(hit: RecallHit) -> str:
    memory = hit.memory
    if isinstance(memory, EpisodicMemory):
        return memory.episode_id
    return memory.memory_id


__all__ = [
    "ACTIVE_SHARE",
    "CANDIDATE_ITEM_LIMIT",
    "CANDIDATE_SHARE",
    "VERIFIED_SHARE",
    "ComposedMemory",
    "compose_memory",
    "compose_memory_context",
    "episode_body",
]
