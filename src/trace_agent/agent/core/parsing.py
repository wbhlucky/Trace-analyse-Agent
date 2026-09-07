"""SDK-independent module shared by all agent adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trace_agent.agent.core.helpers import bounded_error, bounded_preview, extract_json
from trace_agent.models import (
    AgentAnalysisDraft,
    AnalysisResult,
    AnalyzeRequest,
)
from trace_agent.tools import ToolRegistry


def parse_analysis_result(
    result_message: Any | None,
    text_blocks: list[str],
) -> tuple[AnalysisResult | None, str | None, list[str]]:
    errors: list[str] = []
    structured = (
        getattr(result_message, "structured_output", None)
        if result_message is not None
        else None
    )
    if isinstance(structured, AnalysisResult):
        return structured, "result_message.structured_output", errors
    if isinstance(structured, AgentAnalysisDraft):
        return (
            structured.to_analysis_result(),
            "result_message.structured_output",
            errors,
        )
    if isinstance(structured, dict):
        try:
            return (
                AgentAnalysisDraft.model_validate(
                    structured
                ).to_analysis_result(),
                "result_message.structured_output",
                errors,
            )
        except ValueError as exc:
            errors.append(
                "structured_output(dict): "
                + bounded_error(exc)
            )
    elif isinstance(structured, str):
        try:
            return (
                AgentAnalysisDraft.model_validate_json(
                    structured
                ).to_analysis_result(),
                "result_message.structured_output_json",
                errors,
            )
        except ValueError as exc:
            errors.append(
                "structured_output(str): "
                + bounded_error(exc)
            )

    for index, text in enumerate(reversed(text_blocks)):
        try:
            candidate = extract_json([text])
        except ValueError:
            continue
        try:
            return (
                AgentAnalysisDraft.model_validate(
                    candidate
                ).to_analysis_result(),
                f"text_blocks[-{index + 1}]",
                errors,
            )
        except ValueError as exc:
            errors.append(
                f"text_blocks[-{index + 1}]: "
                + bounded_error(exc)
            )
    if not errors:
        errors.append("未发现可解析且符合 Schema 的 AgentAnalysisDraft JSON")
    return None, None, errors



def parse_with_submission(
    *,
    submission_state: dict[str, Any],
    result_message: Any | None,
    text_blocks: list[str],
) -> tuple[AnalysisResult | None, str | None, list[str]]:
    submitted = submission_state.get("result")
    if isinstance(submitted, AnalysisResult):
        return submitted, "submit_analysis_result", []

    parsed, source, errors = parse_analysis_result(
        result_message,
        text_blocks,
    )
    submission_errors = submission_state.get("errors")
    if isinstance(submission_errors, list):
        errors.extend(str(item) for item in submission_errors[-5:])
    return parsed, source, errors


