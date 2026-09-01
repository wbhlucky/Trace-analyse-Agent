from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trace_agent.models import EvidenceRecord, ToolAuditRecord


class EvidenceStore:
    def __init__(self, trace_id: str) -> None:
        self._trace_id = trace_id
        self._evidence: list[EvidenceRecord] = []
        self._audit: list[ToolAuditRecord] = []

    @property
    def evidence(self) -> list[EvidenceRecord]:
        return list(self._evidence)

    def next_id(self) -> str:
        return f"ev-{len(self._evidence) + 1:04d}"

    def add_evidence(
        self,
        *,
        tool: str,
        summary: str,
        data: dict[str, Any],
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            evidence_id=self.next_id(),
            trace_id=self._trace_id,
            tool=tool,
            summary=summary,
            data=data,
        )
        self._evidence.append(record)
        return record

    def add_audit(self, record: ToolAuditRecord) -> None:
        self._audit.append(record)

    def write(self, output_dir: Path) -> None:
        evidence_payload = [item.model_dump(mode="json") for item in self._evidence]
        (output_dir / "evidence.json").write_text(
            json.dumps(evidence_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        with (output_dir / "agent-log.jsonl").open("w", encoding="utf-8") as stream:
            for record in self._audit:
                stream.write(
                    json.dumps(
                        record.model_dump(mode="json"),
                        ensure_ascii=False,
                    )
                )
                stream.write("\n")
