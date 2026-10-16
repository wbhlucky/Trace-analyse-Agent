"""Skill/Read access audit shared by all agent adapters.

Progressive disclosure means the methodology text reaches the model through
built-in ``Skill`` and ``Read`` calls, which never pass through
:class:`~trace_agent.tools.registry.ToolRegistry` and therefore leave no trace
in ``evidence.json`` or ``agent-log.jsonl``. Without this audit there is no way
to tell "the skill was never loaded" apart from "the skill was loaded and did
not help", which is exactly the distinction the evaluation phase needs.

The audit is best-effort by design: a filesystem failure must never abort an
analysis run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from trace_agent.agent.core.prompts import memory_section


class SkillAccessAudit:
    """Append-only JSONL log of skill loading and skill-file reads."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sequence = 0

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        *,
        tool: str,
        decision: str,
        requested_path: str | None = None,
        resolved_path: str | None = None,
        skill: str | None = None,
        reason: str | None = None,
        tool_use_id: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "seq": self._sequence + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "decision": decision,
        }
        if skill is not None:
            entry["skill"] = skill
        if requested_path is not None:
            entry["requested_path"] = requested_path
        if resolved_path is not None:
            entry["resolved_path"] = resolved_path
        if reason is not None:
            entry["reason"] = reason
        if tool_use_id is not None:
            entry["tool_use_id"] = tool_use_id
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(entry, ensure_ascii=False, default=str) + "\n"
                )
        except OSError:
            # Auditing is observational; never let it break a run.
            return
        self._sequence += 1

    def read_entries(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        entries: list[dict[str, Any]] = []
        for line in self._path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries


def open_skill_access_audit(output_dir: Path | None) -> SkillAccessAudit | None:
    """Create the run-scoped audit sink, or ``None`` when there is no run dir."""
    if output_dir is None:
        return None
    return SkillAccessAudit(Path(output_dir) / "skill-access.jsonl")


def record_memory_injection(
    audit: SkillAccessAudit | None,
    *,
    memory_context: str | None,
    system_prompt_text: str,
    hits: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Record whether recalled memory actually reached the system prompt.

    The CLI transcript does not carry the system prompt, so "did the recalled
    memory survive into the model context" cannot be answered from run artifacts
    after the fact. This line makes it observable per run, and ``dropped`` turns
    a silent regression into a loud one.

    ``hits`` adds the section-9 half: *which* memories were in front of the
    model, at what trust. A character count can prove memory was injected and
    still leave a contaminated run undiagnosable, because the interesting
    question is whether the injected items were ``verified`` regularities or one
    run's unverified guess. They are folded into ``reason`` rather than given
    their own columns so the audit schema stays stable for existing readers.
    """
    if audit is None:
        return
    rendered = memory_section(memory_context)
    if not rendered:
        decision = "no_recall"
        prompt_chars = 0
    elif rendered in system_prompt_text:
        decision = "injected"
        prompt_chars = len(rendered)
    else:
        decision = "dropped"
        prompt_chars = 0
    reason = "recall_chars=%d,prompt_chars=%d" % (
        len((memory_context or "").strip()),
        prompt_chars,
    )
    if hits:
        reason += ",hits=%d,injected=%d,ids=%s" % (
            len(hits),
            sum(1 for hit in hits if _truthy(hit.get("injected"))),
            "|".join(str(hit.get("memory_id") or "") for hit in hits[:20]),
        )
        trusts = sorted(
            {str(hit.get("trust") or "") for hit in hits} - {""}
        )
        if trusts:
            reason += ",trust=" + "+".join(trusts)
    audit.record(
        tool="memory_context",
        decision=decision,
        reason=reason,
    )


def _truthy(value: Any) -> bool:
    """Accept both the model's ``bool`` and a JSON round-tripped ``1``."""
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)
