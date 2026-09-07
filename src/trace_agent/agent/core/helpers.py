"""SDK-independent helpers shared by all agent adapters."""

from __future__ import annotations

import json
import os
from typing import Any

def positive_int_env(
    name: str,
    *,
    default: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} 必须在 1 到 {maximum} 之间")
    return value


def extract_json(text_blocks: list[str]) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for text in reversed(text_blocks):
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.removeprefix("```json")
            candidate = candidate.removeprefix("```")
            candidate = candidate.removesuffix("```").strip()
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value

    raise ValueError("Qoder Agent 没有返回可解析的结构化 JSON")


def bounded_error(error: ValueError) -> str:
    return str(error)[:4000]


def bounded_preview(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(
                value,
                ensure_ascii=False,
                default=str,
            )
        except (TypeError, ValueError):
            text = repr(value)
    return text[:limit]

