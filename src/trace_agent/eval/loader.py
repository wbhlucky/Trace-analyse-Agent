from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from trace_agent.eval.models import Case
from trace_agent.models import ScenarioType
from pydantic import ValidationError


class CaseLoadError(ValueError):
    """Raised when a case file is missing, malformed, or unreadable."""


def load_case(path: Path) -> Case:
    """Load and validate one YAML case document.

    ``path`` may point directly to a ``case.yaml`` file or to a case directory
    containing ``case.yaml`` (the on-disk convention used by eval suites).
    """
    if path.is_dir():
        path = path / "case.yaml"
    if not path.is_file():
        raise CaseLoadError(f"case file not found: {path}")

    payload = _load_yaml(path)
    if not isinstance(payload, dict):
        raise CaseLoadError(f"case must be a mapping: {path}")

    case_id = payload.get("id") or path.parent.name
    payload = {**payload, "id": case_id}

    input_part = payload.get("input") or {}
    trace = input_part.get("trace")
    if trace is not None:
        input_part = {
            **input_part,
            "trace": _resolve_trace_path(path, trace),
        }

    if "scenario_type" in input_part:
        input_part = {**input_part, "type": input_part["scenario_type"]}
        input_part.pop("scenario_type", None)

    payload["input"] = input_part

    try:
        return Case.model_validate(payload)
    except ValidationError as exc:
        raise CaseLoadError(f"invalid case {path}: {exc}") from exc


def load_gold(path: Path) -> dict[str, Any]:
    """Load a raw gold mapping without binding it to a case context."""
    payload = _load_yaml(path)
    if not isinstance(payload, dict):
        raise CaseLoadError(f"gold must be a mapping: {path}")
    return payload


def iter_cases(root: Path, *, suite: str | None = None) -> list[Case]:
    """Discover case directories under ``root`` and load them deterministically."""
    if not root.is_dir():
        return []

    candidates: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / "case.yaml").is_file():
            candidates.append(entry)
        elif entry.is_file() and entry.name == "case.yaml":
            candidates.append(entry)

    cases: list[Case] = []
    for candidate in candidates:
        case = load_case(candidate)
        if suite is None or case.suite == suite:
            cases.append(case)
    return cases


def _load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseLoadError(f"invalid YAML in {path}: {exc}") from exc
    except OSError as exc:
        raise CaseLoadError(f"cannot read {path}: {exc}") from exc


def _resolve_trace_path(case_path: Path, raw: Any) -> Path:
    if not isinstance(raw, (str, Path)):
        raise CaseLoadError(f"case input.trace must be a path: {case_path}")
    path = Path(raw)
    if not path.is_absolute():
        path = (case_path.parent / path).resolve()
    return path
