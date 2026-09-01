from __future__ import annotations

from trace_agent.evidence import EvidenceIndex
from trace_agent.models import EvidenceRecord


def _record(
    evidence_id: str,
    tool: str,
    data: dict[str, object],
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        trace_id="trace",
        tool=tool,
        summary=evidence_id,
        data=data,
    )


def test_evidence_index_selects_narrowest_matching_timeline() -> None:
    index = EvidenceIndex(
        [
            _record(
                "wide",
                "inspect_cold_start_timeline",
                {
                    "target_process": {"ipid": 10},
                    "window": {"start_ns": 0, "end_ns": 1000},
                },
            ),
            _record(
                "narrow",
                "inspect_cold_start_timeline",
                {
                    "target_process": {"ipid": 10},
                    "window": {"start_ns": 100, "end_ns": 500},
                },
            ),
        ]
    )

    selected = index.cold_start_timeline(
        ipid=10,
        start_ns=150,
        end_ns=450,
    )

    assert selected is not None
    assert selected.evidence_id == "narrow"


def test_evidence_index_matches_completion_process_and_boundaries() -> None:
    index = EvidenceIndex(
        [
            _record(
                "candidate-other",
                "inspect_completion_latency_candidates",
                {"target_ipid": 20},
            ),
            _record(
                "candidate-target",
                "inspect_completion_latency_candidates",
                {"target_ipid": 10},
            ),
            _record(
                "phase-old",
                "inspect_completion_latency_phases",
                {
                    "target_process": {"ipid": 10},
                    "input_ns": 100,
                    "response_ns": 150,
                    "completion_ns": 300,
                },
            ),
            _record(
                "phase-exact",
                "inspect_completion_latency_phases",
                {
                    "target_process": {"ipid": 10},
                    "input_ns": 100,
                    "response_ns": 200,
                    "completion_ns": 400,
                },
            ),
        ]
    )

    candidate = index.completion_candidate(ipid=10)
    phase = index.completion_phases(
        ipid=10,
        input_ns=100,
        response_ns=200,
        completion_ns=400,
    )

    assert candidate is not None
    assert candidate.evidence_id == "candidate-target"
    assert phase is not None
    assert phase.evidence_id == "phase-exact"
