"""Provider registry mapping ``AgentKind`` values to agent factories.

The registry is the only place where a concrete provider name is bound to a
concrete adapter. Business layers iterate provider metadata and instantiate
agents through :func:`create_agent` instead of importing SDK-specific classes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trace_agent.agent.protocols import AnalysisAgent, ProviderMetadata
from trace_agent.errors import ProviderUnavailable
from trace_agent.models import AgentKind

AgentFactory = Callable[..., AnalysisAgent]

_factories: dict[str, AgentFactory] = {}
_metadata: dict[str, ProviderMetadata] = {}


def register_provider(
    name: str,
    *,
    factory: AgentFactory,
    requires_model_auth: bool = False,
    requires_preflight: bool = False,
    supports_checkpoint: bool = False,
    requires_perf_evidence: bool = False,
    strict_result_validation: bool = False,
    extra_label: str = "",
    description: str = "",
) -> None:
    """Register a binding between an ``AgentKind`` name and an agent factory.

    ``factory`` accepts the same keyword arguments as an agent constructor and
    returns an :class:`AnalysisAgent`.
    """
    _factories[name] = factory
    _metadata[name] = ProviderMetadata(
        name=name,
        requires_model_auth=requires_model_auth,
        requires_preflight=requires_preflight,
        supports_checkpoint=supports_checkpoint,
        requires_perf_evidence=requires_perf_evidence,
        strict_result_validation=strict_result_validation,
        extra_label=extra_label,
        description=description,
    )


def unregister_provider(name: str) -> None:
    """Remove a provider binding (primarily for tests)."""
    _factories.pop(name, None)
    _metadata.pop(name, None)


def get_metadata(name: str) -> ProviderMetadata:
    """Return the declarative metadata for a provider."""
    try:
        return _metadata[name]
    except KeyError as exc:
        raise ProviderUnavailable(f"unregistered agent provider: {name}") from exc


def metadata_for_kind(kind: AgentKind) -> ProviderMetadata:
    """Return metadata for an :class:`AgentKind` enum member."""
    return get_metadata(kind.value)


def available_providers() -> list[ProviderMetadata]:
    """Return provider metadata in stable registration order."""
    return list(_metadata.values())


def is_registered(name: str) -> bool:
    return name in _factories


def create_agent(name: str, **kwargs: Any) -> AnalysisAgent:
    """Instantiate the agent bound to ``name``.

    Raises :class:`ProviderUnavailable` when the provider (or its optional SDK)
    is unavailable, keeping SDK-guard errors uniform across providers.
    """
    if not is_registered(name):
        raise ProviderUnavailable(f"unregistered agent provider: {name}")
    return _factories[name](**kwargs)


def agent_for_kind(kind: AgentKind, **kwargs: Any) -> AnalysisAgent:
    return create_agent(kind.value, **kwargs)
