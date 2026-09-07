from __future__ import annotations

from pathlib import Path

from trace_agent.contract import TaskContract
from trace_agent.models import AnalyzeRequest, ScenarioType


def _request(tmp_path: Path) -> AnalyzeRequest:
    return AnalyzeRequest(
        trace_id="contract",
        trace_path=tmp_path / "trace.htrace",
        scenario_type=ScenarioType.COLD_START,
        scenario="scenario",
        symptom="symptom",
        output_dir=tmp_path,
        target_process="demo",
    )


def test_task_contract_adapts_request(tmp_path: Path) -> None:
    contract = TaskContract.from_request(_request(tmp_path))
    assert contract.trace_id == "contract"
    assert contract.scenario_type is ScenarioType.COLD_START
    assert contract.target_process == "demo"
    assert contract.constraints == {}


def test_task_contract_custom_task_id(tmp_path: Path) -> None:
    contract = TaskContract.from_request(
        _request(tmp_path),
        task_id="custom-id",
        constraints={"budget": 10},
    )
    assert contract.task_id == "custom-id"
    assert contract.constraints == {"budget": 10}
