from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Sequence

from trace_agent.agent.local import LocalAnalysisAgent
from trace_agent.application import AnalyzeApplication
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
    ScenarioType,
    TraceCapability,
)
from trace_agent.progress import ProgressStatus
from trace_agent.skills import (
    SCENARIO_SKILLS,
    ScenarioSkillRouter,
    SkillCatalog,
)
from trace_agent.trace import HTraceAdapter
from trace_agent.trace.trace_streamer import ProcessResult


class FakeTraceStreamerRunner:
    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> ProcessResult:
        del cwd, timeout_seconds
        if command[-1] == "-v":
            return ProcessResult(0, b"version test", b"")

        database_path = Path(command[-1])
        with sqlite3.connect(database_path) as connection:
            connection.executescript(
                """
                CREATE TABLE process (id INTEGER, ipid INTEGER);
                CREATE TABLE thread (id INTEGER, ipid INTEGER, itid INTEGER);
                CREATE TABLE callstack (id INTEGER, callid INTEGER, ts INTEGER);
                CREATE TABLE sched_slice (id INTEGER, itid INTEGER, ts INTEGER);
                CREATE TABLE frame_slice (id INTEGER, ipid INTEGER);
                CREATE TABLE frame_maps (src_row INTEGER, dst_row INTEGER);
                CREATE TABLE diskio (id INTEGER);
                CREATE TABLE instant (id INTEGER, ref INTEGER, ts INTEGER);
                """
            )
        return ProcessResult(0, b"converted", b"")


def make_trace_adapter(tmp_path: Path) -> HTraceAdapter:
    executable = tmp_path / "trace_streamer-test"
    executable.write_bytes(b"fake")
    return HTraceAdapter(
        trace_streamer_path=executable,
        runner=FakeTraceStreamerRunner(),
    )


def test_local_analysis_generates_result_bundle(tmp_path):
    trace_path = tmp_path / "current.htrace"
    trace_path.write_bytes(b"trace-current")
    output_dir = tmp_path / "results"

    request = AnalyzeRequest(
        trace_id="case-001",
        trace_path=trace_path,
        scenario_type=ScenarioType.FRAME_JANK,
        scenario="页面帧率",
        symptom="页面存在卡顿",
        output_dir=output_dir,
        agent=AgentKind.LOCAL,
    )
    application = AnalyzeApplication(
        trace_adapter=make_trace_adapter(tmp_path),
        agent=LocalAnalysisAgent(),
    )

    result = asyncio.run(application.run(request))

    assert result.report_path.is_file()
    assert {
        "report.html",
        "analysis-checkpoint.json",
        "findings.json",
        "evidence.json",
        "run.json",
        "agent-log.jsonl",
        "validation.json",
    } <= {path.name for path in output_dir.iterdir()}

    run = json.loads((output_dir / "run.json").read_text(encoding="utf-8"))
    findings = json.loads(
        (output_dir / "findings.json").read_text(encoding="utf-8")
    )
    evidence = json.loads(
        (output_dir / "evidence.json").read_text(encoding="utf-8")
    )

    assert run["status"] == "completed"
    validation = json.loads(
        (output_dir / "validation.json").read_text(encoding="utf-8")
    )
    assert validation["valid"] is True
    assert Path(run["database_path"]).is_file()
    assert "trace-database" in run["trace_capabilities"]
    assert run["trace_conversions"][0]["version"] == "version test"
    assert findings["findings"] == []
    assert evidence[0]["evidence_id"] == "ev-0001"
    assert evidence[0]["tool"] == "get_trace_overview"


def test_application_reports_all_lifecycle_stages(tmp_path):
    trace_path = tmp_path / "current.htrace"
    trace_path.write_bytes(b"trace-current")
    events = []
    request = AnalyzeRequest(
        trace_id="progress-case",
        trace_path=trace_path,
        scenario_type=ScenarioType.FRAME_JANK,
        scenario="页面帧率",
        symptom="页面存在卡顿",
        output_dir=tmp_path / "results",
        agent=AgentKind.LOCAL,
    )
    application = AnalyzeApplication(
        trace_adapter=make_trace_adapter(tmp_path),
        agent=LocalAnalysisAgent(),
        progress_callback=events.append,
    )

    asyncio.run(application.run(request))

    completed = [
        event
        for event in events
        if event.status is ProgressStatus.COMPLETED
        and event.stage != "agent.tool"
    ]
    assert [event.step for event in completed] == list(range(1, 9))
    assert any(event.stage == "agent.tool" for event in events)


def test_baseline_adds_comparison_evidence(tmp_path):
    trace_path = tmp_path / "current.htrace"
    baseline_path = tmp_path / "baseline.htrace"
    trace_path.write_bytes(b"current-trace-is-larger")
    baseline_path.write_bytes(b"baseline")
    output_dir = tmp_path / "results"

    request = AnalyzeRequest(
        trace_id="case-compare",
        trace_path=trace_path,
        baseline_trace_path=baseline_path,
        scenario_type=ScenarioType.RESPONSE_LATENCY,
        scenario="应用冷启动",
        symptom="版本性能回退",
        output_dir=output_dir,
    )
    application = AnalyzeApplication(
        trace_adapter=make_trace_adapter(tmp_path),
        agent=LocalAnalysisAgent(),
    )

    asyncio.run(application.run(request))

    evidence = json.loads(
        (output_dir / "evidence.json").read_text(encoding="utf-8")
    )
    assert [item["tool"] for item in evidence] == [
        "get_trace_overview",
        "compare_traces",
    ]
    assert evidence[1]["data"]["size_delta_bytes"] > 0


def test_missing_trace_persists_failed_run(tmp_path):
    output_dir = tmp_path / "results"
    request = AnalyzeRequest(
        trace_id="missing",
        trace_path=tmp_path / "missing.htrace",
        scenario_type=ScenarioType.FRAME_JANK,
        scenario="应用冷启动",
        symptom="无法分析",
        output_dir=output_dir,
    )
    application = AnalyzeApplication(
        trace_adapter=HTraceAdapter(),
        agent=LocalAnalysisAgent(),
    )

    try:
        asyncio.run(application.run(request))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing trace should fail")

    run = json.loads((output_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "failed"
    assert "Trace 文件不存在" in run["error"]


def test_all_scenario_skills_are_discoverable_and_versioned():
    catalog = SkillCatalog.project_default()
    available = catalog.list_available()

    assert "trace-analysis" in available
    for scenario_type, scenario_skill in SCENARIO_SKILLS.items():
        skills = ScenarioSkillRouter(catalog).select(scenario_type)
        assert [skill.name for skill in skills] == [
            "trace-analysis",
            scenario_skill,
        ]
        for skill in skills:
            assert skill.fingerprint.startswith("sha256:")
            assert len(skill.fingerprint) == len("sha256:") + 64


def test_perf_skill_is_selected_only_for_valid_perf_capability():
    catalog = SkillCatalog.project_default()
    router = ScenarioSkillRouter(catalog)

    without_perf = router.select(ScenarioType.COLD_START)
    with_perf = router.select(
        ScenarioType.COLD_START,
        [TraceCapability.PERF_SAMPLES],
    )

    assert "perf-sample-analysis" not in {
        skill.name for skill in without_perf
    }
    assert [skill.name for skill in with_perf] == [
        "trace-analysis",
        "cold-start-analysis",
        "perf-sample-analysis",
    ]
