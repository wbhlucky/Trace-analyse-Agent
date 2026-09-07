"""OpenAI SDK guard and lazy import boundary."""

from trace_agent.agent.sdk_guard import require_sdk


def ensure_sdk() -> None:
    require_sdk("openai", extra_label="openai", provider="openai")
