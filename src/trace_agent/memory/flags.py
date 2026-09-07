from __future__ import annotations

import os

MEMORY_ENABLED_ENV = "TRACE_AGENT_MEMORY"
MEMORY_ENABLED_DEFAULT = True


def memory_enabled_explicit() -> bool | None:
    """Optional explicit override; None means use the default."""
    raw = os.environ.get(MEMORY_ENABLED_ENV)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}
