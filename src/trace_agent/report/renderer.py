from __future__ import annotations

import json
from pathlib import Path

from jinja2 import (
    Environment,
    PackageLoader,
    StrictUndefined,
    select_autoescape,
)

from trace_agent.models import AnalysisResult, AnalyzeRequest, EvidenceRecord
from trace_agent.report.projection import ReportProjectionBuilder


class ReportRenderer:
    """Render a self-contained HTML report from a deterministic projection."""

    def __init__(
        self,
        projection_builder: ReportProjectionBuilder | None = None,
    ) -> None:
        self._projection_builder = (
            projection_builder or ReportProjectionBuilder()
        )
        self._environment = Environment(
            loader=PackageLoader("trace_agent", "report/templates"),
            autoescape=select_autoescape(("html", "xml")),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(
        self,
        *,
        request: AnalyzeRequest,
        analysis: AnalysisResult,
        evidence: list[EvidenceRecord],
        output_path: Path,
        database_path: Path | None = None,
    ) -> None:
        template = self._environment.get_template("report.html.j2")
        view = self._projection_builder.build(
            request=request,
            analysis=analysis,
            evidence=evidence,
            database_path=database_path,
        )
        report_payload = {
            "request": request.model_dump(mode="json"),
            "analysis": analysis.model_dump(mode="json"),
            "evidence": [
                record.model_dump(mode="json") for record in evidence
            ],
            "timeline": view["timeline"],
            "startup_modules": view["startup_modules"],
            "perf": view["perf"],
        }
        output_path.write_text(
            template.render(
                request=request,
                analysis=analysis,
                evidence=evidence,
                view=view,
                report_json=self._script_safe_json(report_payload),
            ),
            encoding="utf-8",
        )

    @staticmethod
    def _script_safe_json(payload: dict[str, object]) -> str:
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            .replace("&", "\\u0026")
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
        )
