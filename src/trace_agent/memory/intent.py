from __future__ import annotations

import re

from trace_agent.memory.models import RecallIntent

_HISTORY_HINTS = (
    "previously", "before", "past", "prior", "history", "historical",
    "earlier", "similar", "same device", "again", "last time",
)


class RecallIntentDetector:
    """Rule-based recall intent detection.

    The goal is the same as a mature Active Memory router: ordinary, narrow
    recall stays on the cheap fast path, while context-heavy history or
    comparison questions can upgrade to a deep recall pass. This detector is
    deterministic and never calls a model.
    """

    def __init__(
        self,
        *,
        weak_hit_threshold: float = 0.25,
    ) -> None:
        self._weak_hit_threshold = weak_hit_threshold

    def detect(
        self,
        query: str,
        *,
        best_score: float = 0.0,
    ) -> RecallIntent:
        lowered = query.lower().strip()
        if self._looks_like_history_question(lowered):
            return RecallIntent(
                deep=True,
                reason="history/comparison intent detected",
            )
        if best_score < self._weak_hit_threshold and len(lowered) > 18:
            return RecallIntent(
                deep=True,
                reason="weak fast recall with substantive query",
            )
        return RecallIntent(deep=False, reason="fast recall sufficient")

    @staticmethod
    def _looks_like_history_question(text: str) -> bool:
        if text.endswith("?"):
            words = set(text.lower().split())
            if any(hint in words for hint in _HISTORY_HINTS):
                return True
        return any(hint in text for hint in _HISTORY_HINTS)
