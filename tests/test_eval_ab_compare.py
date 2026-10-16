"""Section 14 acceptance: an A/B that reports its own trustworthiness.

Section 14 names three axes - grader pass rate, root cause hit rate, average
tool calls - and appendix F makes the contamination rate the precondition for
reading them. These tests pin both halves, and in particular the case that a
score-only delta gets wrong: a comparison where the "memory on" side recalled
its own unverified guesses produces a *positive* delta that means nothing, so
the verdict has to say so out loud rather than let the arrow do the talking.

Also pinned: "not measured" is never rendered as zero. A run with no graded
trial has no root cause hit rate, and reporting 0.0 there would look like a
regression instead of a broken experiment.
"""

from __future__ import annotations

import typing
from pathlib import Path

from trace_agent.eval.models import (
    ComponentScore,
    ContaminatedRecord,
    EvalRun,
    GraderResult,
    MemoryContamination,
    MultiTrialResult,
    OperationalMetrics,
    Trial,
    TrialMemory,
    TrialOutcome,
    TrialResult,
    TrialStatus,
)
from trace_agent.eval.report import ab_verdict, compare, load, side_metrics


def _run(
    run_id: str,
    *,
    passed: list[bool],
    diagnosis: list[float] | None,
    tool_calls: list[int],
    memory: list[TrialMemory | None] | None = None,
) -> EvalRun:
    """One case with ``len(passed)`` trials, wired through every layer.

    ``run.trials`` feeds the memory audit and ``multi_trial_results`` feeds the
    pass/tool aggregates, so both have to be populated or the two halves of the
    report would silently disagree.
    """
    case_id = "white_screen_001"
    count = len(passed)
    memory = memory or [None] * count

    trials: list[Trial] = []
    results: list[TrialResult] = []
    graders: list[GraderResult] = []
    for index in range(count):
        trial_id = f"trial-{index}"
        trials.append(
            Trial(
                trial_id=trial_id,
                case_id=case_id,
                index=index,
                status=TrialStatus.COMPLETED,
                memory=memory[index],
            )
        )
        results.append(
            TrialResult(
                case_id=case_id,
                trial_id=trial_id,
                index=index,
                status=TrialStatus.COMPLETED,
                outcome=TrialOutcome(
                    case_id=case_id,
                    trial_id=trial_id,
                    graded_score=1.0 if passed[index] else 0.4,
                    passed=passed[index],
                ),
                operational=OperationalMetrics(
                    case_id=case_id,
                    trial_id=trial_id,
                    turns=1,
                    tool_calls=tool_calls[index],
                ),
                memory=memory[index],
            )
        )
        if diagnosis is not None:
            graders.append(
                GraderResult(
                    case_id=case_id,
                    trial_id=trial_id,
                    overall=diagnosis[index],
                    components=[
                        ComponentScore(
                            component="diagnosis",
                            score=diagnosis[index],
                            weight=0.35,
                        ),
                        ComponentScore(component="evidence", score=0.5, weight=0.2),
                    ],
                )
            )

    return EvalRun(
        run_id=run_id,
        trials=trials,
        grader_results=graders,
        case_scores={case_id: sum(passed) / count if count else 0.0},
        multi_trial_results={
            case_id: MultiTrialResult(
                case_id=case_id,
                k=count,
                trial_results=results,
                pass_count=sum(passed),
            )
        },
    )


def _memory(
    *,
    injected: int,
    contaminated: int = 0,
    trusted_same_run: int = 0,
) -> TrialMemory:
    rate = (contaminated + trusted_same_run) / injected if injected else 0.0
    return TrialMemory(
        enabled=True,
        namespace="eval:eval-abc",
        injected_ids=[f"mem-{i}" for i in range(injected)],
        graded_pass=True,
        contamination=MemoryContamination(
            eval_run_id="eval-abc",
            injected=injected,
            contaminated=contaminated,
            trusted_from_same_run=trusted_same_run,
            rate=rate,
            clean=not (contaminated or trusted_same_run),
            items=[
                ContaminatedRecord(record_id="mem-0", reason="same-run guess")
            ]
            if contaminated
            else [],
        ),
    )


def _side(*, memory_enabled: bool, injected: int) -> dict[str, object]:
    """A minimal side payload, shaped exactly like :func:`side_metrics` output."""
    return {
        "trials": 1,
        "root_cause_hit_rate": 0.5,
        "memory": {
            "enabled": memory_enabled,
            "injected_total": injected,
            "self_contamination_rate": 0.0,
            "contaminated": 0,
            "trusted_from_same_run": 0,
            "clean": True,
        },
    }


def test_ab_reports_the_three_section_14_axes() -> None:
    baseline = _run(
        "eval-off",
        passed=[True, False],
        diagnosis=[0.5, 0.25],
        tool_calls=[12, 14],
    )
    experiment = _run(
        "eval-on",
        passed=[True, True],
        diagnosis=[0.75, 0.5],
        tool_calls=[9, 8],
        memory=[_memory(injected=2), _memory(injected=3)],
    )

    result = compare(baseline, experiment)
    a, b = result["sides"]["a"], result["sides"]["b"]

    assert (a["pass_rate"], b["pass_rate"]) == (0.5, 1.0)
    assert (a["root_cause_hit_rate"], b["root_cause_hit_rate"]) == (0.375, 0.625)
    assert (a["avg_tool_calls"], b["avg_tool_calls"]) == (13.0, 8.5)
    assert result["deltas"]["pass_rate"] == 0.5
    assert result["deltas"]["root_cause_hit_rate"] == 0.25
    # Cost went down while quality went up; the sign convention is always b - a.
    assert result["deltas"]["avg_tool_calls"] == -4.5

    assert result["verdict"]["trustworthy"] is True
    assert result["verdict"]["reasons"] == []
    assert a["memory"]["enabled"] is False
    assert b["memory"]["enabled"] is True


def test_a_positive_delta_from_self_contamination_is_refused() -> None:
    """The case a score-only compare gets wrong: better numbers, worse science."""
    baseline = _run(
        "eval-off",
        passed=[False, False],
        diagnosis=[0.25, 0.25],
        tool_calls=[10, 10],
    )
    experiment = _run(
        "eval-on",
        passed=[True, True],
        diagnosis=[0.9, 0.9],
        tool_calls=[6, 6],
        memory=[_memory(injected=2, contaminated=1), _memory(injected=2)],
    )

    result = compare(baseline, experiment)
    assert result["deltas"]["pass_rate"] == 1.0
    verdict = result["verdict"]
    assert verdict["trustworthy"] is False
    assert any("自污染" in reason for reason in verdict["reasons"])
    assert result["sides"]["b"]["memory"]["clean"] is False


def test_promotion_contamination_also_refuses_the_verdict() -> None:
    """``clean`` folds in trusted-same-run: a zero rate earned by promoting a
    same-run memory to ``active`` is a worse result, not a clean one."""
    baseline = _run("eval-off", passed=[True], diagnosis=[0.8], tool_calls=[5])
    experiment = _run(
        "eval-on",
        passed=[True],
        diagnosis=[0.9],
        tool_calls=[5],
        memory=[_memory(injected=1, trusted_same_run=1)],
    )

    verdict = compare(baseline, experiment)["verdict"]
    assert verdict["trustworthy"] is False
    assert any("自污染" in reason for reason in verdict["reasons"])
    # The rate for this run is 0.00 - it only counted unverified injections. The
    # reason must therefore say *which* axis broke instead of quoting a zero
    # rate as if it were the evidence.
    assert any("不独立" in reason for reason in verdict["reasons"])
    assert not any(
        "自污染率 0.00，测到的是自我污染" in reason for reason in verdict["reasons"]
    )


def test_two_memory_off_runs_are_not_an_ab() -> None:
    a = _run("eval-1", passed=[True], diagnosis=[0.5], tool_calls=[5])
    b = _run("eval-2", passed=[True], diagnosis=[0.5], tool_calls=[5])

    verdict = compare(a, b)["verdict"]
    assert verdict["trustworthy"] is False
    assert any("都关闭了记忆" in reason for reason in verdict["reasons"])


def test_memory_on_with_nothing_injected_is_an_equivalence_not_a_win() -> None:
    baseline = _run("eval-off", passed=[False], diagnosis=[0.2], tool_calls=[9])
    experiment = _run(
        "eval-on",
        passed=[True],
        diagnosis=[0.9],
        tool_calls=[9],
        memory=[_memory(injected=0)],
    )

    verdict = compare(baseline, experiment)["verdict"]
    assert verdict["trustworthy"] is False
    assert any("一条都没注入" in reason for reason in verdict["reasons"])


def test_unmeasured_axes_stay_none_rather_than_zero() -> None:
    """No graded trial means no hit rate - reporting 0.0 would read as a
    regression instead of a broken experiment."""
    baseline = _run("eval-off", passed=[True], diagnosis=None, tool_calls=[5])
    experiment = _run(
        "eval-on",
        passed=[True],
        diagnosis=[0.9],
        tool_calls=[5],
        memory=[_memory(injected=1)],
    )

    result = compare(baseline, experiment)
    assert result["sides"]["a"]["root_cause_hit_rate"] is None
    assert result["deltas"]["root_cause_hit_rate"] is None
    assert result["sides"]["b"]["root_cause_hit_rate"] == 0.9
    assert result["verdict"]["trustworthy"] is False
    assert any("root cause" in reason for reason in result["verdict"]["reasons"])


def test_mismatched_trial_counts_are_called_out() -> None:
    baseline = _run("eval-off", passed=[True], diagnosis=[0.5], tool_calls=[5])
    experiment = _run(
        "eval-on",
        passed=[True, True],
        diagnosis=[0.5, 0.5],
        tool_calls=[5, 5],
        memory=[_memory(injected=1), _memory(injected=1)],
    )

    verdict = compare(baseline, experiment)["verdict"]
    assert verdict["trustworthy"] is False
    assert any("trial 数不同" in reason for reason in verdict["reasons"])


def test_side_metrics_counts_only_graders_that_score_root_causes() -> None:
    """An ``evidence`` component must not leak into the root cause axis."""
    run = _run("eval-x", passed=[True], diagnosis=[0.4], tool_calls=[3])
    metrics = side_metrics(run)
    assert metrics["root_cause_hit_rate"] == 0.4
    assert metrics["cases"] == 1
    assert metrics["trials"] == 1


def test_verdict_is_a_pure_function_of_the_two_sides() -> None:
    """The CLI, the report and any future driver must reach the same verdict.

    Swapping the arguments is not the same experiment: every delta is printed as
    ``b - a`` under a heading that says "memory effect", so a reversed pair has
    to be refused rather than silently read backwards.
    """
    off = _side(memory_enabled=False, injected=0)
    on = _side(memory_enabled=True, injected=2)

    forward = ab_verdict(off, on)
    assert forward["trustworthy"] is True
    assert forward["reasons"] == []

    reversed_ = ab_verdict(on, off)
    assert reversed_["trustworthy"] is False
    assert any("方向" in reason for reason in reversed_["reasons"])


def test_report_load_annotations_resolve() -> None:
    """``report.load`` annotates ``root: Path``; the import used to be missing
    and only survived because PEP 563 makes annotations lazy."""
    hints = typing.get_type_hints(load)
    assert hints["root"] is Path


def test_echo_ab_prints_aligned_axes_then_the_verdict(capsys) -> None:
    """The table a human reads, including why an untrusted one says so.

    Alignment is asserted by display width rather than by eye: the headers are
    Chinese and the cells are numbers, and ``str.format`` padding would put the
    numbers two columns left of the labels they belong to.
    """
    from trace_agent.cli_text import display_width
    from trace_agent.eval.cli import _echo_ab

    baseline = _run(
        "eval-off", passed=[False], diagnosis=[0.25], tool_calls=[12]
    )
    experiment = _run(
        "eval-on",
        passed=[True],
        diagnosis=[0.75],
        tool_calls=[8],
        memory=[_memory(injected=2)],
    )
    _echo_ab(compare(baseline, experiment))
    lines = capsys.readouterr().out.splitlines()

    assert lines[0].startswith("A/B（§14）: eval-off -> eval-on")
    header = lines[1]
    for label in ("grader 通过率", "root cause 命中率", "平均工具调用"):
        assert any(line.startswith(label) for line in lines), label
    widths = {display_width(line) for line in lines[1:5]}
    assert len(widths) == 1, f"columns drift: {sorted(widths)} from {header!r}"
    assert lines[-1].startswith("结论: 可信")


def test_echo_ab_lists_why_a_contaminated_pair_is_refused(capsys) -> None:
    from trace_agent.eval.cli import _echo_ab

    baseline = _run(
        "eval-off", passed=[False], diagnosis=[0.25], tool_calls=[12]
    )
    experiment = _run(
        "eval-on",
        passed=[True],
        diagnosis=[0.9],
        tool_calls=[6],
        memory=[_memory(injected=2, contaminated=1)],
    )
    _echo_ab(compare(baseline, experiment))
    out = capsys.readouterr().out

    assert "结论: 暂不可信" in out
    assert "自污染" in out
    # The contaminated side must be visibly flagged in its own row, not only in
    # the verdict: the table gets quoted on its own.
    assert "CONTAMINATED" in out
    # And the delta is still printed - refusing to interpret a number is not the
    # same as hiding it.
    assert "+1.0000" in out


def test_echo_ab_names_which_contamination_axis_broke(capsys) -> None:
    """The case a real qoder warm-up run produced (appendix I.5).

    Trial 1 recalled trial 0's episode as a *trusted* prior. The curated channel
    stayed ``candidate``, so the unverified rate is genuinely 0.00 while
    ``clean`` is genuinely false. A cell reading ``0.00 CONTAMINATED`` looks like
    a bug in the metric; the cell has to say which axis broke.
    """
    from trace_agent.eval.cli import _echo_ab

    # Two trials on both sides, so the only thing the verdict can object to is
    # the leak itself: trial 1 recalled what trial 0 had just written.
    baseline = _run(
        "eval-off", passed=[False, False], diagnosis=[0.25, 0.25], tool_calls=[12, 12]
    )
    experiment = _run(
        "eval-on",
        passed=[True, True],
        diagnosis=[0.9, 0.9],
        tool_calls=[6, 6],
        memory=[_memory(injected=1), _memory(injected=1, trusted_same_run=1)],
    )
    _echo_ab(compare(baseline, experiment))
    out = capsys.readouterr().out

    assert "CONTAMINATED(同run可信)" in out
    assert any("不独立" in line for line in out.splitlines())
    assert "结论: 暂不可信" in out
    # The rate itself is honestly zero: nothing unverified was injected. The
    # refusal has to come from the axis that actually broke.
    assert "0.00 CONTAMINATED(同run可信)" in out
