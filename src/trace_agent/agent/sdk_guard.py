"""Uniform optional-SDK availability guard for provider adapters."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from typing import Any, TypeVar

from trace_agent.errors import ProviderUnavailable

_T = TypeVar("_T")


def require_sdk(
    module_name: str,
    *,
    extra_label: str | None = None,
    provider: str | None = None,
) -> None:
    """Raise :class:`ProviderUnavailable` unless ``module_name`` is importable.

    Providers import their SDK lazily and call this guard first so that a
    missing optional dependency surfaces as a uniform, user-recoverable error
    instead of a raw ``ImportError`` / ``RuntimeError`` string.
    """
    if importlib.util.find_spec(module_name) is None:
        hint = f"uv sync --extra {extra_label}" if extra_label else "install the optional SDK"
        raise ProviderUnavailable(
            f"{module_name} ????????{hint}",
            provider=provider,
        )


def load_sdk(
    module_name: str,
    loader: Callable[[], _T],
    *,
    extra_label: str | None = None,
    provider: str | None = None,
) -> _T:
    """Guard SDK availability and return the value produced by ``loader``."""
    require_sdk(
        module_name,
        extra_label=extra_label,
        provider=provider,
    )
    return loader()
