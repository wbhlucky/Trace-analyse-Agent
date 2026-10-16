"""The single definition of "what text represents a memory".

:mod:`trace_agent.memory.recall` scores a record by comparing the query against
a concatenation of its fields. The SQLite store indexes that same concatenation
into FTS5 and uses the index to narrow the candidate pool before scoring.

Those two have to agree. If the index covers *less* text than the scorer reads,
pushdown silently drops hits a full scan would have kept: a query for
``pattern`` or ``white_screen`` can match on ``kind`` / ``scenario_type`` alone,
and neither field appears in the section-3.3 column sketch. Building both sides
from this module turns that from a likely bug into an impossible one.

The ``*_index_text`` functions are the index side and must stay a **superset**
of the matching ``*_search_text``. Extra tokens in the index are harmless (the
reranker still rejects them); missing tokens are a recall regression.
"""

from __future__ import annotations

from trace_agent.memory.models import CuratedMemory, EpisodicMemory


def curated_search_text(memory: CuratedMemory) -> str:
    """Curated fields that participate in lexical relevance."""
    return "\n".join(
        [
            memory.topic,
            memory.content,
            " ".join(memory.keywords),
            memory.kind.value,
            memory.scenario_type or "",
        ]
    )


def episode_search_text(episode: EpisodicMemory) -> str:
    """Episode fields that participate in lexical relevance."""
    return "\n".join(
        [
            episode.scenario_type,
            episode.scenario,
            episode.symptom,
            episode.summary,
            " ".join(episode.root_causes),
            episode.target_process or "",
            episode.device or "",
            episode.build or "",
        ]
    )


def curated_index_text(memory: CuratedMemory) -> str:
    """FTS document text for a curated memory.

    Identical to :func:`curated_search_text`: ``keywords`` is already part of
    the scored text, so the keyword-overlap term adds no new tokens.
    """
    return curated_search_text(memory)


def episode_index_text(episode: EpisodicMemory) -> str:
    """FTS document text for an episode.

    Wider than :func:`episode_search_text` on purpose. Tags are not part of the
    scored concatenation, but ``lexical_score`` receives them as ``keywords``,
    so a query matching only a tag can still clear the relevance floor via the
    keyword-overlap term. Indexing tags keeps that hit reachable.
    """
    return "\n".join(
        [
            episode_search_text(episode),
            " ".join(episode.tags),
        ]
    )
