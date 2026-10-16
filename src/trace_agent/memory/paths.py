from __future__ import annotations

import os
from pathlib import Path

from trace_agent.memory.flags import MEMORY_ROOT_ENV

DEFAULT_MEMORY_DIR_NAME = ".trace-agent/memory"
DEFAULT_PROJECT_MEMORY_ROOT = Path(DEFAULT_MEMORY_DIR_NAME)


def resolve_memory_root(project_root: Path) -> Path:
    """Resolve the project-scoped memory root.

    The default lives under ``<project>/.trace-agent/memory``. Resolution is
    intentionally side-effect free, so callers decide when to create
    directories. Any I/O failure must be handled by the caller, never by the
    resolver.

    ``TRACE_AGENT_MEMORY_ROOT`` wins over the project-relative default. Callers
    currently pass ``Path.cwd()``, which meant launching the CLI or dashboard
    from a different directory silently swapped the whole memory corpus, and
    eval runs shared the production store. An explicit root removes both
    surprises and gives eval somewhere isolated to write.
    """
    override = os.environ.get(MEMORY_ROOT_ENV)
    if override and override.strip():
        return Path(override.strip()).expanduser().resolve()
    return Path(project_root).resolve() / DEFAULT_MEMORY_DIR_NAME
