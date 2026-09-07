"""Deterministic trajectory grader: scores how an agent worked, not what it found.

V1 checks five behavioral signals without prescribing a fixed tool-call path:

1. required evidence obtained
2. tool errors
3. repeated / wasteful tool calls
4. turn / tool budget
5. forbidden or invalid actions

Operational metrics (turns, tool_calls, errors, repeated calls) are reported
separately and never folded into outcome correctness.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from trace_agent.eval.models import TrajectoryCheck, TrajectoryResult


@dataclass(slots=True)
class TrajectoryConfig:
    """Deterministic thresholds for the trajectory layer."""

    max_tool_errors: int = 1
    max_turns: int = 20
    max_tool_calls: int = 20
    max_repeated_calls: int = 0
    min_evidence_retrievals: int = 1
    forbidden_tools: list[str] = field(default_factory=list)
    required_evidence_tools: list[str] = field(default_factory=list)
    known_tools: list[str] = field(default_factory=list)


class TrajectoryGrader:
    """Scores an agent run's tool/transcript trace deterministically.

    ``tool_calls`` is expected to be a list of audit records as emitted to
    ``agent-log.jsonl``::

        {"tool": ..., "arguments": {...}, "status": "success"|"error",
         "evidence_id": ..., "error": null|...}

    The tool record contract is intentionally lenient: unknown/missing fields
    degrade to safe defaults rather than raising, because eval data is data.
    """

    def __init__(self, config: TrajectoryConfig | None = None) -> None:
        self._config = config or TrajectoryConfig()

    def grade(
        self,
        case_id: str,
        trial_id: str,
        tool_calls: list[dict[str, Any]],
        *,
        turns: int | None = None,
    ) -> TrajectoryResult:
        records = self._normalize(tool_calls)
        tool_names = sorted({r.tool for r in records if r.tool})

        tool_error_count = sum(1 for r in records if r.is_error)
        turn_count = self._resolve_turns(turns, records)
        repeated = self._count_repeated(records)
        forbidden = sum(
            1 for r in records if r.tool in set(self._config.forbidden_tools)
        )
        invalid = sum(
            1
            for r in records
            if r.tool
            and self._config.known_tools
            and r.tool not in set(self._config.known_tools)
        )
        evidence_retrievals = sum(
            1 for r in records if r.evidence_id and not r.is_error
        )

        checks: list[TrajectoryCheck] = []
        checks.append(
            self._check_evidence(records)
        )
        checks.append(
            TrajectoryCheck(
                code="tool.errors",
                passed=tool_error_count <= self._config.max_tool_errors,
                message=(
                    ""
                    if tool_error_count <= self._config.max_tool_errors
                    else (
                        f"{tool_error_count} tool errors exceed "
                        f"limit {self._config.max_tool_errors}"
                    )
                ),
            )
        )
        checks.append(
            TrajectoryCheck(
                code="tool.repeated_calls",
                passed=repeated <= self._config.max_repeated_calls,
                message=(
                    ""
                    if repeated <= self._config.max_repeated_calls
                    else f"{repeated} repeated/wasteful tool calls"
                ),
            )
        )
        checks.append(
            self._check_budget(turn_count, len(records))
        )
        checks.append(
            self._check_forbidden_invalid(forbidden, invalid)
        )

        passed_count = sum(1 for check in checks if check.passed)
        score = round(passed_count / len(checks), 4) if checks else 1.0

        return TrajectoryResult(
            case_id=case_id,
            trial_id=trial_id,
            score=score,
            tool_calls=len(records),
            tool_errors=tool_error_count,
            turns=turn_count,
            invalid_actions=invalid,
            evidence_retrievals=evidence_retrievals,
            repeated_calls=repeated,
            forbidden_calls=forbidden,
            tool_names=tool_names,
            checks=checks,
        )

    def _check_evidence(
        self,
        records: list[_Record],
    ) -> TrajectoryCheck:
        required = set(self._config.required_evidence_tools)
        obtained = {
            r.tool for r in records if r.tool and r.evidence_id and not r.is_error
        }
        if required:
            missing = sorted(required - obtained)
            passed = not missing
            message = (
                "" if passed else f"missing required evidence tools: {missing}"
            )
            return TrajectoryCheck(
                code="trajectory.required_evidence",
                passed=passed,
                message=message,
            )

        retrievals = sum(
            1 for r in records if r.evidence_id and not r.is_error
        )
        passed = retrievals >= self._config.min_evidence_retrievals
        return TrajectoryCheck(
            code="trajectory.required_evidence",
            passed=passed,
            message=(
                ""
                if passed
                else (
                    f"only {retrievals} evidence retrieval(s), "
                    f"expected at least {self._config.min_evidence_retrievals}"
                )
            ),
        )

    def _check_budget(
        self,
        turns: int,
        tool_calls: int,
    ) -> TrajectoryCheck:
        problems: list[str] = []
        if turns > self._config.max_turns:
            problems.append(f"{turns} turns exceed {self._config.max_turns}")
        if tool_calls > self._config.max_tool_calls:
            problems.append(
                f"{tool_calls} tool calls exceed {self._config.max_tool_calls}"
            )
        return TrajectoryCheck(
            code="trajectory.budget",
            passed=not problems,
            message="; ".join(problems),
        )

    def _check_forbidden_invalid(
        self,
        forbidden: int,
        invalid: int,
    ) -> TrajectoryCheck:
        problems: list[str] = []
        if forbidden:
            problems.append(f"{forbidden} forbidden tool call(s)")
        if invalid:
            problems.append(f"{invalid} invalid/unknown action(s)")
        return TrajectoryCheck(
            code="trajectory.actions",
            passed=not problems,
            message="; ".join(problems),
        )

    @staticmethod
    def _resolve_turns(
        turns: int | None,
        records: list["_Record"],
    ) -> int:
        if turns is not None:
            return max(0, turns)
        return len(records)

    @staticmethod
    def _count_repeated(records: list["_Record"]) -> int:
        """Count duplicate invocations of the same tool+arguments pair."""
        seen: set[tuple[str, str]] = set()
        repeated = 0
        for record in records:
            signature = (record.tool, record.args_key)
            if signature in seen:
                repeated += 1
            else:
                seen.add(signature)
        return repeated

    @staticmethod
    def _normalize(
        tool_calls: list[dict[str, Any]],
    ) -> list["_Record"]:
        records: list[_Record] = []
        for raw in tool_calls:
            if not isinstance(raw, dict):
                continue
            tool = raw.get("tool") or raw.get("name") or ""
            args = raw.get("arguments", raw.get("args", {}))
            status = str(raw.get("status", "success"))
            error = raw.get("error")
            evidence_id = raw.get("evidence_id")
            records.append(
                _Record(
                    tool=str(tool),
                    args_key=_args_key(args),
                    is_error=status == "error" or bool(error),
                    evidence_id=evidence_id or None,
                )
            )
        return records


@dataclass(frozen=True, slots=True)
class _Record:
    tool: str
    args_key: str
    is_error: bool
    evidence_id: str | None


def _args_key(args: Any) -> str:
    if args is None:
        return "null"
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(args)