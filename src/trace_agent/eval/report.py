from __future__ import annotations

from typing import Any

from trace_agent.eval.harness import load_run
from trace_agent.eval.models import EvalRun


def summarize(run: EvalRun) -> dict[str, Any]:
    """Compact, JSON-friendly summary for CLI output."""
    total_cases = len(run.case_scores)
    suite_count = len(run.suite_scores)
    mean_case = (
        round(sum(run.case_scores.values()) / total_cases, 4)
        if total_cases
        else 0.0
    )
    return {
        "run_id": run.run_id,
        "total_cases": total_cases,
        "suites": suite_count,
        "overall_score": mean_case,
        "case_scores": dict(sorted(run.case_scores.items())),
        "suite_scores": dict(sorted(run.suite_scores.items())),
        "trials": len(run.trials),
    }


def compare(run_a: EvalRun, run_b: EvalRun) -> dict[str, Any]:
    """Deterministic score delta between two completed runs."""
    result: dict[str, Any] = {
        "run_a": run_a.run_id,
        "run_b": run_b.run_id,
        "cases": {},
    }
    for case_id in sorted(set(run_a.case_scores) | set(run_b.case_scores)):
        a = run_a.case_scores.get(case_id, 0.0)
        b = run_b.case_scores.get(case_id, 0.0)
        result["cases"][case_id] = {
            "a": a,
            "b": b,
            "delta": round(b - a, 4),
        }
    result["overall_delta"] = round(
        sum(item["delta"] for item in result["cases"].values()), 4
    )
    return result


def load(run_id: str, root: Path) -> EvalRun:
    """Locate a persisted run by id under an eval artifacts directory."""
    candidates = [root / run_id / "eval-run.json", root / f"{run_id}.json"]
    for candidate in candidates:
        if candidate.is_file():
            return load_run(candidate)
    raise FileNotFoundError(
        f"eval run {run_id!r} not found under {root}"
    )
