from __future__ import annotations

import asyncio
import logging
import random
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class ErrorCategory(StrEnum):
    RETRYABLE = "RetryableError"
    FATAL = "FatalError"
    USER_RECOVERABLE = "UserRecoverableError"


_RETRYABLE_STATUS = {408, 409, 425, 429, *range(500, 600)}

_RETRYABLE_MARKERS = (
    "rate limit",
    "rate limited",
    "too many requests",
    "timeout",
    "timed out",
    "connection reset",
    "connection refused",
    "connection error",
    "network",
    "temporarily unavailable",
    "service unavailable",
    "internal server error",
    "provider transient",
    "overloaded",
)

_NONRETRYABLE_MARKERS = (
    "401",
    "403",
    "400",
    "unauthorized",
    "authentication",
    "api key",
    "not installed",
    "未安装",
)


def classify_exception(exc: BaseException) -> ErrorCategory:
    if not isinstance(exc, Exception):
        return ErrorCategory.FATAL
    if isinstance(exc, AgentFailure):
        return exc.category

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in _RETRYABLE_STATUS:
            return ErrorCategory.RETRYABLE
        if 400 <= status_code <= 499:
            return ErrorCategory.USER_RECOVERABLE

    error_type = getattr(exc, "type", None)
    if isinstance(error_type, str):
        lowered = error_type.lower()
        if any(marker in lowered for marker in _NONRETRYABLE_MARKERS):
            return ErrorCategory.USER_RECOVERABLE
        if any(marker in lowered for marker in _RETRYABLE_MARKERS):
            return ErrorCategory.RETRYABLE

    message = str(exc).lower()
    if any(marker in message for marker in _NONRETRYABLE_MARKERS):
        return ErrorCategory.USER_RECOVERABLE
    if any(marker in message for marker in _RETRYABLE_MARKERS):
        return ErrorCategory.RETRYABLE

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError)):
        return ErrorCategory.RETRYABLE

    if isinstance(exc, (ImportError, ModuleNotFoundError, TypeError, KeyError)):
        return ErrorCategory.USER_RECOVERABLE

    return ErrorCategory.FATAL


class RunInterrupted(Exception):
    """Cooperative cancellation of a run at the next checkable boundary."""

    def __init__(self, *, run_id: str | None = None) -> None:
        self.run_id = run_id
        suffix = ("\uFF08run_id=" + str(run_id) + "\uFF09") if run_id else ""
        super().__init__("\u5206\u6790\u4efb\u52a1\u5df2\u53d6\u6d88" + suffix)

class AgentFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory,
        session_id: str | None = None,
        diagnostic_paths: list[Path] | None = None,
        attempts: int = 1,
    ) -> None:
        self.category = category
        self.session_id = session_id
        self.diagnostic_paths = list(diagnostic_paths or [])
        self.attempts = attempts
        super().__init__(message)


class AgentRetryExhausted(AgentFailure):
    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.RETRYABLE,
        last_exception: BaseException | None = None,
        **kwargs: Any,
    ) -> None:
        self.last_exception = last_exception
        super().__init__(message, category=category, **kwargs)


async def async_retry(
    operation: Callable[[], Awaitable[_T]],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 1.0,
    max_delay_seconds: float = 15.0,
    jitter: bool = True,
    deadline_seconds: float | None = None,
    on_retry: Callable[
        [BaseException, int], Awaitable[None] | None
    ] | None = None,
) -> _T:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + deadline_seconds
        if deadline_seconds is not None
        else None
    )
    last_exception: BaseException | None = None
    last_category: ErrorCategory = ErrorCategory.FATAL

    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            last_exception = exc
            last_category = classify_exception(exc)
            if last_category is not ErrorCategory.RETRYABLE:
                raise
            if attempt >= max_attempts:
                break
            if deadline is not None and loop.time() >= deadline:
                break

            delay = min(
                base_delay_seconds * (2 ** (attempt - 1)),
                max_delay_seconds,
            )
            if jitter:
                delay = delay * (0.5 + random.random())
            if delay <= 0:
                delay = 0.05

            logger.warning(
                "重试 %s (%s/%s)...",
                type(last_exception).__name__,
                attempt,
                max_attempts,
            )
            if deadline is not None and loop.time() + delay > deadline:
                break
            if on_retry is not None:
                retry_result = on_retry(last_exception, attempt)
                if asyncio.iscoroutine(retry_result):
                    await retry_result
            await asyncio.sleep(delay)

    assert last_exception is not None
    raise AgentRetryExhausted(
        f"{last_category.value} 在 {max_attempts} 次尝试后仍未恢复："
        f"{type(last_exception).__name__}: {last_exception}",
        category=last_category,
        last_exception=last_exception,
        attempts=max_attempts,
    )
