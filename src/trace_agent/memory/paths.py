from __future__ import annotations

from pathlib import Path


DEFAULT_MEMORY_DIR_NAME = ".trace-agent/memory"
DEFAULT_PROJECT_MEMORY_ROOT = Path(DEFAULT_MEMORY_DIR_NAME)


def resolve_memory_root(project_root: Path) -> Path:
    """Resolve the project-scoped memory root.

    The default lives under ``<project>/.trace-agent/memory``. Resolution is
    intentionally side-effect free, so callers decide when to create
    directories. Any I/O failure must be handled by the caller, never by the
    resolver.
    """
    return Path(project_root).resolve() / DEFAULT_MEMORY_DIR_NAME
