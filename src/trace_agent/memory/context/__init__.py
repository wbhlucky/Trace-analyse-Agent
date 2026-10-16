"""Context assembly: the last step before recalled memory reaches the model.

Retrieval answers "what is relevant"; this package answers "what does the model
get to see, and how is it framed". Keeping the two apart is what lets the trust
partitioning in section 6.4 change without touching ranking, and lets ranking
change without re-litigating how a prior is worded.
"""

from __future__ import annotations

from trace_agent.memory.context.composer import (
    ACTIVE_SHARE,
    CANDIDATE_ITEM_LIMIT,
    CANDIDATE_SHARE,
    VERIFIED_SHARE,
    ComposedMemory,
    compose_memory,
    compose_memory_context,
)

__all__ = [
    "ACTIVE_SHARE",
    "CANDIDATE_ITEM_LIMIT",
    "CANDIDATE_SHARE",
    "VERIFIED_SHARE",
    "ComposedMemory",
    "compose_memory",
    "compose_memory_context",
]
