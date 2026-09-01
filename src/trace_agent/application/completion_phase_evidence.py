from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from time import perf_counter

from trace_agent.database import (
    CompletionLatencyPhaseRepository,
    summarize_completion_latency_phases,
)
from trace_agent.evidence import EvidenceIndex, EvidenceStore
from trace_agent.models import (
    AnalysisResult,
    EvidenceRecord,
    ToolAuditRecord,
    TraceCapability,
    TraceHandle,
    utc_now,
)


RepositoryFactory = Callable[[Path], CompletionLatencyPhaseRepository]


class CompletionPhaseEvidenceEnsurer:
    """Ensure final completion boundaries have an exact phase projection.

    The agent owns boundary selection. This service only reruns the
    deterministic projection when the agent inspected an earlier boundary set
    and later submitted a different, valid final boundary set. The repair is
    result normalization, so it does not consume another model tool call.
    """

    _TOOL = "inspect_completion_latency_phases"

    def __init__(
        self,
        repository_factory: RepositoryFactory | None = None,
    ) -> None:
        self._repository_factory = (
            repository_factory or CompletionLatencyPhaseRepository
        )

    def ensure(
        self,
        analysis: AnalysisResult,
        *,
        trace: TraceHandle,
        evidence: EvidenceStore,
    ) -> EvidenceRecord | None:
        completion = analysis.completion_latency
        if (
            completion is None
            or not completion.phases
            or trace.database_path is None
            or TraceCapability.CPU_SCHEDULING not in trace.capabilities
        ):
            return None

        input_ns = completion.input_boundary.timestamp_ns
        response_ns = (
            completion.response_boundary.timestamp_ns
            if completion.response_boundary is not None
            else None
        )
        completion_ns = (
            completion.completion_boundary.timestamp_ns
            if completion.completion_boundary is not None
            and completion.completion_proven
            else None
        )
        if not self._valid_boundaries(
            input_ns=input_ns,
            response_ns=response_ns,
            completion_ns=completion_ns,
        ):
            return None

        existing = EvidenceIndex(evidence.evidence).completion_phases(
            ipid=completion.resolved_process.ipid,
            input_ns=input_ns,
            response_ns=response_ns,
            completion_ns=completion_ns,
        )
        if existing is not None:
            return None

        arguments = {
            "target_ipid": completion.resolved_process.ipid,
            "input_ns": input_ns,
            "response_ns": response_ns or 0,
            "completion_ns": completion_ns or 0,
            "max_threads_per_phase": 4,
            "max_slices_per_phase": 10,
            "max_frames_per_phase": 10,
        }
        started_at = utc_now()
        started = perf_counter()
        try:
            data = self._repository_factory(trace.database_path).inspect(
                target_ipid=arguments["target_ipid"],
                input_ns=input_ns,
                response_ns=response_ns,
                completion_ns=completion_ns,
                max_threads_per_phase=4,
                max_slices_per_phase=10,
                max_frames_per_phase=10,
            )
            record = evidence.add_evidence(
                tool=self._TOOL,
                summary=summarize_completion_latency_phases(data),
                data=data,
            )
            evidence.add_audit(
                ToolAuditRecord(
                    tool=self._TOOL,
                    arguments=arguments,
                    status="success",
                    started_at=started_at,
                    duration_ms=(perf_counter() - started) * 1000,
                    evidence_id=record.evidence_id,
                )
            )
            return record
        except Exception as exc:
            evidence.add_audit(
                ToolAuditRecord(
                    tool=self._TOOL,
                    arguments=arguments,
                    status="error",
                    started_at=started_at,
                    duration_ms=(perf_counter() - started) * 1000,
                    error=str(exc),
                )
            )
            raise

    @staticmethod
    def _valid_boundaries(
        *,
        input_ns: int,
        response_ns: int | None,
        completion_ns: int | None,
    ) -> bool:
        if response_ns is None and completion_ns is None:
            return False
        if response_ns is not None and response_ns <= input_ns:
            return False
        if completion_ns is not None and completion_ns <= input_ns:
            return False
        if (
            response_ns is not None
            and completion_ns is not None
            and completion_ns <= response_ns
        ):
            return False
        return True
