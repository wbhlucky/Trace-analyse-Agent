from __future__ import annotations

from pathlib import Path
from typing import Any

from trace_agent.eval.harness import load_run
from trace_agent.eval.memory_audit import summarize_memory
from trace_agent.eval.models import EvalRun

#: The grader component that scores ``len(found) / len(gold.root_causes)``.
#: Section 14 asks for a "root cause hit rate"; the grader already computes
#: exactly that under this key, so the A/B reads it rather than reinventing it.
_ROOT_CAUSE_COMPONENT = "diagnosis"


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
        # Present in every report, memory-off included: "no memory metrics" and
        # "memory metrics are all zero" must stay distinguishable, and an absent
        # key would make every consumer guess which one it got.
        "memory": summarize_memory(run),
    }


def _mean(values: list[float]) -> float | None:
    """Mean of a sample, or ``None`` when there is nothing to average.

    ``None`` and ``0.0`` must stay distinguishable: "no trial was graded" is a
    broken A/B, "every graded trial scored zero" is a result.
    """
    return round(sum(values) / len(values), 4) if values else None


def _root_cause_hit_rate(run: EvalRun) -> float | None:
    """Mean of the grader's ``diagnosis`` component across graded trials."""
    scores: list[float] = []
    for graded in run.grader_results:
        for component in graded.components:
            if component.component == _ROOT_CAUSE_COMPONENT:
                scores.append(component.score)
                break
    return _mean(scores)


def _tool_calls(run: EvalRun) -> list[int]:
    """Per-trial tool call counts, preferring the operational layer.

    ``operational`` is the accounting of work actually done; ``trajectory``
    scores that work. The fallback exists because a trial that failed before
    trajectory scoring still has an operational record.
    """
    counts: list[int] = []
    for multi in run.multi_trial_results.values():
        for trial in multi.trial_results:
            operational = trial.operational
            if operational is not None:
                counts.append(operational.tool_calls)
                continue
            trajectory = trial.trajectory
            if trajectory is not None:
                counts.append(trajectory.tool_calls)
    return counts


def side_metrics(run: EvalRun) -> dict[str, Any]:
    """One side of the section 14 A/B: quality, root causes, and cost.

    Every axis is derived from the grader and the trial records, never from the
    memory audit, so the memory-off side reports exactly the same shape as the
    memory-on side. The memory block is attached separately and answers a
    different question: whether this side is allowed to be believed.
    """
    passed = 0
    total = 0
    for multi in run.multi_trial_results.values():
        for trial in multi.trial_results:
            total += 1
            if trial.outcome is not None and trial.outcome.passed:
                passed += 1

    tool_calls = _tool_calls(run)
    memory = summarize_memory(run)
    return {
        "run_id": run.run_id,
        "cases": len(run.case_scores),
        "trials": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else None,
        "root_cause_hit_rate": _root_cause_hit_rate(run),
        "avg_tool_calls": (
            round(sum(tool_calls) / len(tool_calls), 2) if tool_calls else None
        ),
        "mean_score": _mean(list(run.case_scores.values())),
        "memory": {
            "enabled": memory.get("enabled", False),
            "injected_total": memory.get("injected_total", 0),
            "self_contamination_rate": memory.get("self_contamination_rate", 0.0),
            # Both halves of ``clean`` travel with the side. The rate alone
            # counts unverified injections only, so a run that leaked a
            # *trusted* same-run memory would otherwise report 0.00 and still be
            # refused - a contradiction the reader has to be able to resolve.
            "contaminated": memory.get("contaminated", 0),
            "trusted_from_same_run": memory.get("trusted_from_same_run", 0),
            "clean": memory.get("clean", True),
        },
    }


def _delta(a: float | None, b: float | None) -> float | None:
    """``b - a`` when both sides measured the axis, else ``None``."""
    if a is None or b is None:
        return None
    return round(b - a, 4)


def ab_verdict(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Whether an A/B delta may be read as "memory helped", and why not.

    Section 14 makes the contamination rate a precondition rather than a
    footnote: a positive delta produced by a run that recalled its own
    unverified guesses measures self-contamination, not memory value. The
    reasons are returned even when the verdict is positive, so a reader can see
    which checks the comparison actually passed.

    Direction matters too. Every delta is ``b - a`` and the CLI prints it under
    a heading that says "memory effect", so a pair whose memory-off side is ``b``
    is refused rather than read backwards.
    """
    reasons: list[str] = []
    mem_a = a["memory"]
    mem_b = b["memory"]

    if mem_a["enabled"] and mem_b["enabled"]:
        reasons.append("两组都开启了记忆，缺少 memory-off 基线")
    elif not mem_a["enabled"] and not mem_b["enabled"]:
        reasons.append("两组都关闭了记忆，对比不含记忆变量")
    elif mem_a["enabled"]:
        # Not a data problem, a reading problem: the CLI prints every delta as
        # ``b - a`` under a heading that says "memory effect", so a reversed
        # pair yields arrows that point the wrong way and read as a conclusion.
        reasons.append("基线组开着记忆而对比组关着，Δ 的方向与「记忆带来收益」相反")
    if not mem_b["clean"]:
        # ``.get`` keeps this readable for a compare payload persisted before
        # the two counts were carried alongside the rate.
        unverified = mem_b.get("contaminated", 0)
        same_run = mem_b.get("trusted_from_same_run", 0)
        rate = mem_b["self_contamination_rate"]
        if unverified:
            reasons.append(
                f"对比组注入了 {unverified} 条本次 eval 内产生的未验证记忆"
                f"（自污染率 {rate:.2f}），测到的是自我污染而非记忆收益"
            )
        if same_run:
            reasons.append(
                f"对比组把本次 eval 内其他 trial 的记忆当作可信先验注入了 {same_run} 条"
                f"（自污染率 {rate:.2f} 只统计未验证注入，trial 之间已不独立）"
            )
        if not unverified and not same_run:
            reasons.append(f"对比组自污染率 {rate:.2f}，测到的是自我污染而非记忆收益")
    if mem_b["enabled"] and not mem_b["injected_total"]:
        reasons.append("对比组开启了记忆但一条都没注入，两组实际等价")
    if a["trials"] != b["trials"]:
        reasons.append(f"两组 trial 数不同（{a['trials']} vs {b['trials']}）")
    if not a["trials"] or not b["trials"]:
        reasons.append("至少一组没有任何 trial，无法对比")
    if a["root_cause_hit_rate"] is None or b["root_cause_hit_rate"] is None:
        reasons.append("至少一组没有可判分的 root cause 命中数据")

    return {"trustworthy": not reasons, "reasons": reasons}


def compare(run_a: EvalRun, run_b: EvalRun) -> dict[str, Any]:
    """Deterministic delta between two completed runs.

    ``cases`` / ``overall_delta`` are the original per-case score view.
    ``sides`` / ``deltas`` / ``verdict`` are the section 14 A/B view: the three
    axes it names (pass rate, root cause hit rate, average tool calls) plus the
    memory observability that decides whether the delta means anything.
    """
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

    side_a = side_metrics(run_a)
    side_b = side_metrics(run_b)
    result["sides"] = {"a": side_a, "b": side_b}
    result["deltas"] = {
        axis: _delta(side_a[axis], side_b[axis])
        for axis in (
            "pass_rate",
            "root_cause_hit_rate",
            "avg_tool_calls",
            "mean_score",
        )
    }
    result["verdict"] = ab_verdict(side_a, side_b)
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
