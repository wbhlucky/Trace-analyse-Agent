from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from trace_agent.cli import _print_failure
from trace_agent.errors import (
    AgentFailure,
    AgentRetryExhausted,
    ErrorCategory,
    async_retry,
    classify_exception,
)


class _Retryable(OSError):
    pass


class _NonRetryable(ValueError):
    pass


def test_classify_retryable_status_and_markers() -> None:
    error = RuntimeError("provider transient: timeout waiting for response")
    assert classify_exception(error) is ErrorCategory.RETRYABLE


def test_classify_nonretryable_auth_error() -> None:
    error = RuntimeError("authentication failed: invalid api key")
    assert classify_exception(error) is ErrorCategory.USER_RECOVERABLE


def test_classify_fatal_local_error() -> None:
    error = RuntimeError("本地状态机不可恢复地损坏")
    assert classify_exception(error) is ErrorCategory.FATAL


def test_async_retry_succeeds_after_transient_failure() -> None:
    calls: list[int] = []

    async def operation() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise _Retryable("temporary network blip")
        return "ok"

    result = asyncio.run(async_retry(operation, max_attempts=3))
    assert result == "ok"
    assert len(calls) == 2


def test_async_retry_does_not_retry_nonretryable() -> None:
    calls: list[int] = []

    async def operation() -> str:
        calls.append(1)
        raise _NonRetryable("bad request payload")

    with pytest.raises(_NonRetryable):
        asyncio.run(async_retry(operation, max_attempts=3))
    assert len(calls) == 1


def test_async_retry_exhausts_and_wraps_failure() -> None:
    calls: list[int] = []

    async def operation() -> str:
        calls.append(1)
        raise _Retryable("service unavailable")

    with pytest.raises(AgentRetryExhausted) as caught:
        asyncio.run(async_retry(operation, max_attempts=2))
    assert len(calls) == 2
    assert caught.value.category is ErrorCategory.RETRYABLE
    assert caught.value.last_exception is not None


def test_print_failure_reports_diagnostic_paths(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_path = tmp_path / "run.json"
    run_path.write_text(
        json.dumps({"run_id": "run-test-123"}, ensure_ascii=False),
        encoding="utf-8",
    )
    agent_result_path = tmp_path / "agent-result.json"
    agent_result_path.write_text("{}", encoding="utf-8")

    failure = AgentFailure(
        "model service error",
        category=ErrorCategory.RETRYABLE,
        session_id="sess-abc",
        diagnostic_paths=[],
    )

    _print_failure(failure, output_dir=tmp_path, show_traceback=False)

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "分析失败" in text
    assert "错误类型: RetryableError" in text
    assert "Exception: AgentFailure: model service error" in text
    assert "Run ID : run-test-123" in text
    assert "Session: sess-abc" in text
    assert str(run_path) in text
    assert str(agent_result_path) in text
