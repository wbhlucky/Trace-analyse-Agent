from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import queue
import socket
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from rich.console import Console

from trace_agent.agent import DefaultAnalysisAgentFactory
from trace_agent.agent.registry import metadata_for_kind
from trace_agent.application import AnalyzeApplication
from trace_agent.config import LlmRuntimeConfig
from trace_agent.memory import (
    SessionMemory,
    SessionRole,
    default_memory_runtime,
)
from trace_agent.errors import (
    AgentFailure,
    ErrorCategory,
    RunInterrupted,
    classify_exception,
)
from trace_agent.runtime import (
    SqliteEventStore,
    get_event_bus,
)
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
    JobError,
    LlmProvider,
    ScenarioType,
)
from trace_agent.trace import HTraceAdapter

_STATIC_DIR = Path(__file__).parent / "static"

_RESULT_FILES = (
    "run.json",
    "findings.json",
    "evidence.json",
    "validation.json",
    "analysis-checkpoint.json",
    "agent-result.json",
    "rejected-findings.json",
)

_SCENARIO_LABELS = {
    "cold-start": "\u51b7\u542f\u52a8",
    "response-latency": "\u54cd\u5e94\u65f6\u5ef6",
    "completion-latency": "\u5b8c\u6210\u65f6\u5ef6",
    "frame-jank": "\u5361\u987f/\u4e22\u5e27",
}

_SCENARIO_TEMPLATES = {
    "cold-start": {
        "scenario": "\u70b9\u51fb\u542f\u52a8\u5e94\u7528\u8fdb\u5165\u9996\u5c4f",
        "symptom": "\u70b9\u51fb\u540e\u9875\u9762\u957f\u65f6\u95f4\u767d\u5c4f\uff0c\u51b7\u542f\u52a8\u603b\u65f6\u957f\u660e\u663e\u504f\u9ad8",
        "problem_duration_ms": 2000,
    },
    "response-latency": {
        "scenario": "\u70b9\u51fb\u6309\u94ae\u540e\u7b49\u5f85\u64cd\u4f5c\u54cd\u5e94",
        "symptom": "\u70b9\u51fb\u540e\u63a7\u4ef6\u65e0\u54cd\u5e94\uff0c\u54cd\u5e94\u65f6\u5ef6\u8fc7\u957f",
        "problem_duration_ms": 1000,
    },
    "completion-latency": {
        "scenario": "\u70b9\u51fb\u8fdb\u5165\u8be6\u60c5\u9875",
        "symptom": "\u70b9\u51fb\u540e\u754c\u9762\u5df2\u51fa\u73b0\u4f46\u5185\u5bb9\u4e3a\u957f\u65f6\u95f4\u52a0\u8f7d\uff0c\u5b8c\u6210\u65f6\u5ef6\u8fc7\u957f",
        "problem_duration_ms": 2000,
    },
    "frame-jank": {
        "scenario": "\u6ed1\u52a8\u5217\u8868\u6216\u8fde\u7eed\u64cd\u4f5c",
        "symptom": "\u6eda\u52a8/\u70b9\u51fb\u8fc7\u7a0b\u4e2d\u660e\u663e\u5361\u987f\u6216\u4e22\u5e27",
        "problem_duration_ms": 500,
    },
}


def _load_json(path: Path) -> Any | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _iso_to_local(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _first_number(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _duration_seconds(started: str | None, completed: str | None) -> float | None:
    start = _parse_timestamp(started)
    end = _parse_timestamp(completed)
    if start is None or end is None:
        return None
    duration = (end - start).total_seconds()
    return duration if duration >= 0 else None


def _format_duration(seconds: float | None, *, running: bool = False) -> str | None:
    if seconds is None:
        return None
    value = max(0.0, seconds)
    if value >= 3600:
        hours = int(value // 3600)
        minutes = int((value % 3600) // 60)
        return f"{hours}h {minutes}m"
    if value >= 60:
        minutes = int(value // 60)
        secs = value % 60
        return f"{minutes}m {secs:.0f}s"
    return f"{value:.2f}s"


def _case_summary(run: dict[str, Any], findings: dict[str, Any] | None) -> dict[str, Any]:
    scenario_type = run.get("scenario_type", "")
    metric_name: str | None = None
    metric_value: float | None = None
    metric_unit = "ms"

    if findings:
        for key, label in (
            ("completion_latency", "\u5b8c\u6210\u65f6\u5ef6"),
            ("cold_start", "\u542f\u52a8\u65f6\u957f"),
        ):
            block = findings.get(key)
            if isinstance(block, dict):
                for field, candidate in (
                    ("completion_latency_ms", block.get("completion_latency_ms")),
                    ("response_latency_ms", block.get("response_latency_ms")),
                    ("total_duration_ms", block.get("total_duration_ms")),
                    ("presentation_duration_ms", block.get("presentation_duration_ms")),
                ):
                    candidate = _first_number(candidate)
                    if candidate is not None:
                        metric_name = label
                        metric_value = candidate
                        break
                if metric_value is not None:
                    break

    raw_started_at = run.get("started_at")
    raw_completed_at = run.get("completed_at")
    status = run.get("status")
    duration_s = _duration_seconds(raw_started_at, raw_completed_at)
    if status == "running" and raw_started_at:
        start = _parse_timestamp(raw_started_at)
        if start is not None:
            duration_s = max(0.0, (datetime.now(timezone.utc) - start).total_seconds())

    findings_count = len(findings.get("findings") or []) if findings else 0
    severity = None
    if findings:
        for item in findings.get("findings") or []:
            sev = item.get("severity")
            rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(sev, 0)
            if severity is None or rank > severity[1]:
                severity = (sev, rank)

    return {
        "name": run.get("trace_id") or "",
        "scenario_type": scenario_type,
        "scenario_type_label": _SCENARIO_LABELS.get(scenario_type, scenario_type),
        "scenario": run.get("scenario"),
        "symptom": run.get("symptom"),
        "agent": run.get("agent"),
        "model": run.get("model"),
        "status": run.get("status"),
        "started_at": _iso_to_local(run.get("started_at")),
        "completed_at": _iso_to_local(run.get("completed_at")),
        "metric_name": metric_name,
        "metric_value": metric_value,
        "metric_unit": metric_unit,
        "findings_count": findings_count,
        "severity": severity[0] if severity else None,
        "duration_s": duration_s,
        "duration_display": _format_duration(duration_s, running=(status == "running")),
        "running": status == "running",
        "summary": findings.get("summary") if findings else None,
    }


def _build_bundle(case_dir: Path, case_name: str) -> dict[str, Any]:
    files: dict[str, Any] = {}
    for name in _RESULT_FILES:
        files[name.split(".")[0]] = _load_json(case_dir / name)

    report_path = case_dir / "report.html"
    return {
        "name": case_name,
        "report_available": report_path.is_file(),
        "files": files,
    }


def _preflight(project_root: Path) -> dict[str, Any]:
    """Report local runtime readiness without leaking secrets.

    Mirrors the Claude Code / Codex "preflight" gate: the frontend should be
    able to tell the operator *what* is missing and *how* to fix it before a
    long-running agent task is submitted, rather than failing mid-run.
    """
    checks: list[dict[str, Any]] = []
    dotenv_path = project_root / ".env"
    has_dotenv = dotenv_path.is_file()

    def env_value(name: str) -> str | None:
        return os.environ.get(name)

    agent_session_token = env_value("QODER_PERSONAL_ACCESS_TOKEN")
    deepseek_key = (
        env_value("DEEPSEEK_API_KEY")
        or env_value("ANTHROPIC_AUTH_TOKEN")
        or env_value("ANTHROPIC_API_KEY")
    )
    if has_dotenv:
        try:
            for raw_line in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, raw_value = line.split("=", 1)
                value = raw_value.strip().strip("'\"").strip("'\"")
                if name == "QODER_PERSONAL_ACCESS_TOKEN" and value and not agent_session_token:
                    agent_session_token = value
                if name in ("DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY") and value and not deepseek_key:
                    deepseek_key = value
        except OSError:
            pass

    checks.append({
        "id": "dotenv",
        "label": ".env \u914d\u7f6e\u6587\u4ef6",
        "status": "ok" if has_dotenv else "warn",
        "detail": "\u5df2\u5b58\u5728" if has_dotenv else "\u5efa\u8bae\u521b\u5efa .env \u5e76\u586b\u5199 DeepSeek Key",
        "action": None if has_dotenv else "\u521b\u5efa\u9879\u76ee\u6839\u76ee\u5f55\u4e0b\u7684 .env",
    })
    checks.append({
        "id": "deepseek",
        "label": "DeepSeek API Key",
        "status": "ok" if deepseek_key else "error",
        "detail": "\u5df2\u914d\u7f6e" if deepseek_key else "\u672a\u914d\u7f6e DEEPSEEK_API_KEY",
        "action": None if deepseek_key else "\u5728 .env \u4e2d\u586b\u5199 DEEPSEEK_API_KEY",
    })
    checks.append({
        "id": "agent-session",
        "label": "Agent \u4f1a\u8bdd\u767b\u5f55",
        "status": "ok" if agent_session_token else "warn",
        "detail": "\u5df2\u914d\u7f6e PAT" if agent_session_token else "\u672a\u914d\u7f6e PAT",
        "action": None if agent_session_token else "\u914d\u7f6e PAT \u540e\u9009\u62e9 SDK Agent",
    })
    ok_count = sum(1 for check in checks if check["status"] == "ok")
    ready = all(check["status"] in ("ok", "warn") for check in checks)
    return {
        "ready": ready,
        "checks": checks,
        "summary": f"{ok_count}/{len(checks)} \u9879\u5c31\u7eea",
        "templates": _SCENARIO_TEMPLATES,
    }



# Keep a small yet complete public contract for the frontend: state (status),
# reason (user_message + suggested_action), next action (resumable) and details
# (a separate diagnostics endpoint). Internal stack traces are never sent to
# the browser.
@dataclass
class AnalysisJob:
    id: str
    status: str = "queued"  # queued | running | completed | failed
    case_name: str | None = None
    output_dir: Path | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: JobError | None = None
    params: dict[str, Any] = field(default_factory=dict)
    cancel_event: threading.Event = field(default_factory=threading.Event)


_CATEGORY_ACTIONS = {
    ErrorCategory.RETRYABLE.value: "可重试，请稍后重新运行分析",
    ErrorCategory.USER_RECOVERABLE.value: "请检查 Trace 参数并重新运行 Agent 分析",
    ErrorCategory.FATAL.value: "发生内部错误，请查看诊断信息",
}


def _job_error_from_exception(
    exc: BaseException,
    *,
    trace_id: str | None,
    output_dir: Path | None,
) -> JobError:
    category = classify_exception(exc)
    stage = _last_failed_step(output_dir)
    return JobError(
        code=type(exc).__name__,
        category=category.value,
        stage=stage,
        retryable=category is ErrorCategory.RETRYABLE,
        resumable=_job_is_resumable(output_dir),
        user_message=_user_message(exc, category),
        suggested_action=_CATEGORY_ACTIONS.get(
            category.value,
            _CATEGORY_ACTIONS[ErrorCategory.FATAL.value],
        ),
        trace_id=trace_id,
        internal_detail=str(exc) or type(exc).__name__,
    )


def _user_message(exc: BaseException, category: ErrorCategory) -> str:
    if isinstance(exc, AgentFailure):
        return str(exc) or "正在执行 Agent 分析"
    if category is ErrorCategory.RETRYABLE:
        return "遇到临时错误，请稍后重试"
    if category is ErrorCategory.USER_RECOVERABLE:
        return "输入或配置有误，请修正后重试"
    message = str(exc).strip()
    return message or "分析运行失败"


def _last_failed_step(output_dir: Path | None) -> str | None:
    if output_dir is None:
        return None
    run = _load_json(output_dir / "run.json")
    if not isinstance(run, dict):
        return None
    for state in reversed(run.get("step_states") or []):
        if isinstance(state, dict) and state.get("status") == "failed":
            return state.get("step")
    return None


def _job_is_resumable(output_dir: Path | None) -> bool:
    if output_dir is None:
        return False
    run = _load_json(output_dir / "run.json")
    if not isinstance(run, dict):
        return False
    return run.get("status") in {
        "interrupted",
        "resumable",
        "running-recovery",
        "running",
    }


_JOB_LOCK = threading.Lock()
_JOBS: dict[str, AnalysisJob] = {}
_JOB_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="analysis")


def _set_job_status(job: AnalysisJob, status: str) -> None:
    with _JOB_LOCK:
        job.status = status


def _job_view(job: AnalysisJob) -> dict[str, Any]:
    duration_s = None
    if job.started_at is not None:
        end = job.completed_at or datetime.now(timezone.utc)
        duration_s = (end - job.started_at).total_seconds()
    error = None
    if job.error is not None:
        error = {
            "user_message": job.error.user_message,
            "suggested_action": job.error.suggested_action,
            "trace_id": job.error.trace_id,
        }
    return {
        "id": job.id,
        "status": job.status,
        "case_name": job.case_name,
        "output_dir": str(job.output_dir) if job.output_dir else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "duration_s": duration_s,
        "duration_display": _format_duration(duration_s, running=job.status in ("queued", "running")),
        "error": error,
        "params": job.params,
    }


def _job_diagnostics(job: AnalysisJob) -> dict[str, Any]:
    if job.output_dir is None:
        return {"files": {}}
    files: dict[str, Any] = {}
    for name in ("run.json", "agent-result.json"):
        files[name] = _load_json(job.output_dir / name)
    steps_dir = job.output_dir / "steps"
    step_states = []
    if steps_dir.is_dir():
        for path in sorted(steps_dir.glob("*.state.json")):
            step_states.append(
                {
                    "name": path.name,
                    "content": _load_json(path),
                }
            )
    return {"files": files, "steps": step_states}


def _default_output_name(trace_path: Path, scenario_type: str) -> str:
    stem = trace_path.stem or "trace"
    return f"analyze-{ScenarioType(scenario_type).value}-{stem}-{uuid.uuid4().hex[:6]}"


def _resolve_user_path(value: Any, base: Path) -> Path | None:
    if value is None:
        return None
    text_value = str(value).strip()
    if not text_value:
        return None
    path = Path(text_value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _run_analysis_job(job: AnalysisJob, project_root: Path, results_root: Path) -> None:
    try:
        params = job.params
        trace_path = _resolve_user_path(params["trace_path"], project_root)
        if trace_path is None:
            job.status = "failed"
            job.error = JobError(
                code="MissingTracePath",
                category=ErrorCategory.USER_RECOVERABLE.value,
                stage="run.prepare",
                retryable=False,
                resumable=False,
                user_message="缺少 Trace 路径参数",
                suggested_action=(
                    "请提供有效的 Trace 文件路径"
                ),
                trace_id=params.get("trace_id"),
                internal_detail="trace_path is required",
            )
            job.completed_at = datetime.now(timezone.utc)
            _set_job_status(job, "failed")
            return
        output_dir = _resolve_user_path(params.get("output_dir"), project_root)
        if output_dir is None:
            output_dir = results_root / _default_output_name(trace_path, params["scenario_type"])

        agent = AgentKind(params.get("agent") or "local")
        provider_raw = params.get("provider")
        provider = LlmProvider(provider_raw) if provider_raw else None

        llm_config: LlmRuntimeConfig | None = None
        if metadata_for_kind(agent).requires_model_auth:
            llm_config = LlmRuntimeConfig.resolve(
                project_root=project_root,
                provider=provider,
                model=params.get("model"),
            )

        request = AnalyzeRequest(
            trace_id=params.get("trace_id") or trace_path.stem,
            trace_path=trace_path,
            scenario_type=ScenarioType(params["scenario_type"]),
            scenario=params["scenario"],
            symptom=params["symptom"],
            output_dir=output_dir,
            device=params.get("device"),
            build=params.get("build"),
            time_range=params.get("time_range"),
            target_process=params.get("target_process"),
            operation_marker=params.get("operation_marker"),
            start_marker=params.get("start_marker"),
            end_marker=params.get("end_marker"),
            response_marker=params.get("response_marker"),
            completion_marker=params.get("completion_marker"),
            problem_duration_ms=params.get("problem_duration_ms"),
            refresh_rate_hz=params.get("refresh_rate_hz"),
            baseline_trace_path=_resolve_user_path(
                params.get("baseline_trace_path"),
                project_root,
            ),
            agent=agent,
            provider=provider,
            model=params.get("model") or (llm_config.model if llm_config else None),
        )

        trace_streamer = _resolve_user_path(params.get("trace_streamer"), project_root)
        trace_cache_dir = _resolve_user_path(params.get("trace_cache_dir"), project_root)
        trace_adapter = HTraceAdapter(
            trace_streamer_path=trace_streamer,
            timeout_seconds=float(params.get("trace_streamer_timeout") or 600),
            cache_dir=(
                trace_cache_dir
                or (project_root / ".trace-agent" / "cache" / "trace-db")
                if params.get("trace_cache", True)
                else None
            ),
            refresh_cache=bool(params.get("refresh_trace_cache", False)),
        )

        application = AnalyzeApplication(
            trace_adapter=trace_adapter,
            agent_factory=DefaultAnalysisAgentFactory(llm_config=llm_config),
        )

        job.case_name = output_dir.name
        job.output_dir = output_dir
        job.started_at = datetime.now(timezone.utc)
        _set_job_status(job, "running")

        result = asyncio.run(
            application.run(
                request,
                run_id=job.id,
                cancel_event=job.cancel_event,
                resume_from=params.get("resume"),
            )
        )

        job.completed_at = datetime.now(timezone.utc)
        job.status = "completed"
        job.case_name = output_dir.name
        _ = result
    except RunInterrupted as exc:
        job.completed_at = datetime.now(timezone.utc)
        job.status = "interrupted"
        job.error = _job_error_from_exception(
            exc,
            trace_id=params.get("trace_id") or (
                job.case_name
            ),
            output_dir=job.output_dir,
        )
    except Exception as exc:  # noqa: BLE001 - surface structured job error
        job.completed_at = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = _job_error_from_exception(
            exc,
            trace_id=params.get("trace_id") or (
                job.case_name
            ),
            output_dir=job.output_dir,
        )



_MEMORY_RUNTIME = None
_MEMORY_RUNTIME_LOCK = threading.Lock()

def _memory_runtime() -> "default_memory_runtime":
    global _MEMORY_RUNTIME
    with _MEMORY_RUNTIME_LOCK:
        if _MEMORY_RUNTIME is None:
            _MEMORY_RUNTIME = default_memory_runtime(Path.cwd())
        return _MEMORY_RUNTIME

def _session_view(session: SessionMemory) -> dict[str, Any]:
    return session.model_dump(mode="json")

def _run_dream_cycle(project_root: Path) -> None:
    runtime = _memory_runtime()
    try:
        runtime.dreaming.run_once(limit=20)
    except Exception:
        return

def _submit_analysis(params: dict[str, Any], project_root: Path, results_root: Path) -> dict[str, Any]:
    job = AnalysisJob(id=f"job-{uuid.uuid4().hex[:12]}", params=params)
    with _JOB_LOCK:
        _JOBS[job.id] = job
    _JOB_EXECUTOR.submit(_run_analysis_job, job, project_root, results_root)
    return _job_view(job)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DitingAgentDashboard/1.0"
    protocol_version = "HTTP/1.1"

    # Referenced from the class attribute set by the server factory.
    results_dir: Path = Path("results")
    project_root: Path = Path.cwd()

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type, _ = mimetypes.guess_type(path.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_event_stream(self, job_id: str) -> None:
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            self._send_json(
                {"error": "job not found"},
                HTTPStatus.NOT_FOUND,
            )
            return

        from_seq = 1
        last_event_id = self.headers.get("Last-Event-ID")
        if last_event_id:
            try:
                from_seq = int(last_event_id) + 1
            except ValueError:
                from_seq = 1

        subscription = get_event_bus().subscribe(
            job_id,
            from_seq=from_seq,
        )
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.flush()

            while True:
                try:
                    event = subscription.queue.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                serialized = json.dumps(
                    event.to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                frame = (
                    f"id: {event.seq}\n"
                    "event: message\n"
                    f"data: {serialized}\n\n"
                ).encode("utf-8")
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            subscription.unsubscribe()
            self.close_connection = True


    @staticmethod
    def _cancel_route_job_id(path: str) -> str | None:
        for prefix in ("/runs/", "/api/analyze/jobs/"):
            if not path.startswith(prefix):
                continue
            segments = path.removeprefix(prefix).strip("/").split("/")
            if len(segments) == 2 and segments[1] == "cancel":
                return segments[0]
        return None

    def _cancel_run(self, job_id: str) -> None:
        with _JOB_LOCK:
            job = _JOBS.get(job_id)
        if job is None:
            self._send_json(
                {"error": "job not found"},
                HTTPStatus.NOT_FOUND,
            )
            return
        if job.status in ("queued", "running"):
            job.cancel_event.set()
        self._send_json(_job_view(job))


    def _resolve_case(self, case_name: str) -> Path:
        # Prevent traversal; case names come from a URL path segment.
        return (self.results_dir / case_name).resolve()

    def _ensure_within_results(self, target: Path) -> bool:
        try:
            target.relative_to(self.results_dir.resolve())
            return target.is_dir()
        except ValueError:
            return False

    def do_GET(self) -> None:  # noqa: N802
        from urllib.parse import unquote, urlparse

        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/cases" or path == "/api/cases/":
            cases = []
            if self.results_dir.is_dir():
                for child in sorted(self.results_dir.iterdir()):
                    if not child.is_dir():
                        continue
                    run = _load_json(child / "run.json")
                    if not isinstance(run, dict):
                        continue
                    findings = _load_json(child / "findings.json")
                    case = _case_summary(run, findings if isinstance(findings, dict) else None)
                    case["id"] = child.name
                    cases.append(case)
            self._send_json({"cases": cases})
            return

        if path.startswith("/api/cases/"):
            case_name = path.removeprefix("/api/cases/").strip("/")
            target = self._resolve_case(case_name)
            if not self._ensure_within_results(target):
                self._send_json({"error": "\u65e0\u6548\u7684\u7528\u4f8b\u540d\u79f0"}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json(_build_bundle(target, case_name))
            return

        if path == "/api/health":
            self._send_json({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})
            return

        if path == "/api/memory/sessions" or path == "/api/memory/sessions/":
            sessions = _memory_runtime().sessions.list_sessions()
            self._send_json(
                {"sessions": [s.model_dump(mode="json") for s in sessions]}
            )
            return

        if path.startswith("/api/memory/sessions/"):
            session_id = path.removeprefix("/api/memory/sessions/").strip("/")
            session = _memory_runtime().sessions.get_session(session_id)
            if session is None:
                self._send_json({"error": "session not found"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"session": session.model_dump(mode="json")})
            return

        if path == "/api/memory/recall":
            from urllib.parse import parse_qs
            query = parse_qs(parsed.query).get("query", [""])[0]
            scenario_type = parse_qs(parsed.query).get("scenario_type", [None])[0]
            result = _memory_runtime().recall(query or "", scenario_type=scenario_type)
            self._send_json(result.model_dump(mode="json"))
            return

        if path == "/api/memory/dream":
            _run_dream_cycle(self.project_root)
            self._send_json({"status": "ok"})
            return

        if path == "/api/preflight" or path == "/api/preflight/":
            self._send_json(_preflight(self.project_root))
            return

        if path == "/api/analyze/jobs" or path == "/api/analyze/jobs/":
            with _JOB_LOCK:
                jobs = [_job_view(job) for job in _JOBS.values()]
            jobs.reverse()
            self._send_json({"jobs": jobs})
            return

        if path.startswith("/api/analyze/jobs/"):
            remainder = path.removeprefix("/api/analyze/jobs/").strip("/")
            segments = remainder.split("/")
            job_id = segments[0]
            with _JOB_LOCK:
                job = _JOBS.get(job_id)
            if job is None:
                self._send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
                return
            if len(segments) > 1 and segments[1] == "events":
                self._handle_event_stream(job_id)
                return
            if len(segments) > 1 and segments[1] == "diagnostics":
                self._send_json(_job_diagnostics(job))
                return
            self._send_json(_job_view(job))
            return

        if path == "/reports/" or path == "/reports":
            self._send_json({"error": "missing case name"}, HTTPStatus.BAD_REQUEST)
            return

        if path.startswith("/reports/"):
            rel = path.removeprefix("/reports/").lstrip("/")
            parts = rel.split("/", 1)
            case_name = parts[0]
            file_name = parts[1] if len(parts) > 1 else "report.html"
            case_dir = self._resolve_case(case_name)
            if not self._ensure_within_results(case_dir):
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            if file_name in ("", "report.html"):
                report_path = case_dir / "report.html"
                if report_path.is_file():
                    self._send_file(report_path)
                    return
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if path in ("/", "/index.html"):
            self._send_file(_STATIC_DIR / "index.html")
            return

        requested = path.lstrip("/")
        static_path = (_STATIC_DIR / requested).resolve()
        try:
            static_path.relative_to(_STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if static_path.is_file():
            self._send_file(static_path)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)


    def do_POST(self) -> None:  # noqa: N802
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

        cancel_job_id = self._cancel_route_job_id(path)
        if cancel_job_id is not None:
            self._cancel_run(cancel_job_id)
            return

        if path == "/api/memory/sessions":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            session = _memory_runtime().sessions.create_session(
                title=str(body.get("title") or ""),
                topic=str(body.get("topic") or ""),
                session_id=body.get("session_id"),
            )
            self._send_json({"session": session.model_dump(mode="json")}, HTTPStatus.CREATED)
            return

        if path.startswith("/api/memory/sessions/") and path.endswith("/messages"):
            session_id = path.removeprefix("/api/memory/sessions/").removesuffix("/messages").strip("/")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                body = {}
            content = str(body.get("content") or "")
            if not content:
                self._send_json({"error": "content is required"}, HTTPStatus.BAD_REQUEST)
                return
            role = str(body.get("role") or "user")
            session = _memory_runtime().append_session_message(
                session_id,
                content,
                role=role,
                references=list(body.get("references") or []),
            )
            self._send_json({"session": session.model_dump(mode="json") if session else None})
            return

        if path != "/api/analyze":
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            params = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._send_json({"error": "invalid JSON body"}, HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(params, dict):
            self._send_json({"error": "JSON body must be an object"}, HTTPStatus.BAD_REQUEST)
            return

        missing = [
            name
            for name in ("trace_path", "scenario_type", "scenario", "symptom")
            if not params.get(name)
        ]
        if missing:
            self._send_json(
                {"error": f"missing required fields: {', '.join(missing)}"},
                HTTPStatus.BAD_REQUEST,
            )
            return

        try:
            ScenarioType(params["scenario_type"])
        except ValueError:
            self._send_json({"error": "invalid scenario_type"}, HTTPStatus.BAD_REQUEST)
            return

        job = _submit_analysis(params, self.project_root, self.results_dir)
        self._send_json(job, HTTPStatus.ACCEPTED)


def _get_free_port(host: str, port: int) -> int:
    try:
        server = ThreadingHTTPServer((host, port), DashboardHandler)
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
            return probe.getsockname()[1]
    server.server_close()
    return port


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    results_dir: Path = Path("results"),
    open_browser: bool = False,
) -> None:
    console = Console()
    results_root = Path(results_dir).expanduser().resolve()

    if not results_root.is_dir():
        console.print(
            f"[red]\u7ed3\u679c\u76ee\u5f55\u4e0d\u5b58\u5728: {results_root}[/red]",
        )
        raise SystemExit(1)

    get_event_bus().register_store(
        SqliteEventStore(
            results_root / ".trace-agent" / "events.sqlite3"
        )
    )

    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {"results_dir": results_root, "project_root": Path.cwd().resolve()},
    )
    bound_port = _get_free_port(host, port)
    def _dreaming_loop() -> None:
        while True:
            time.sleep(float(os.environ.get("TRACE_AGENT_MEMORY_DREAM_INTERVAL", "30")))
            try:
                _memory_runtime().dreaming.run_once(limit=20)
            except Exception:
                continue

    dream_thread = threading.Thread(
        target=_dreaming_loop,
        name="memory-dreaming",
        daemon=True,
    )
    dream_thread.start()


    try:
        server = ThreadingHTTPServer((host, bound_port), handler)
    except OSError as exc:
        console.print(f"[red]\u65e0\u6cd5\u542f\u52a8\u670d\u52a1\u5668: {exc}[/red]")
        raise SystemExit(1) from exc

    url = f"http://{host}:{bound_port}"
    console.print(f"[green]DitingAgent \u524d\u7aef\u5df2\u542f\u52a8[/green]")
    console.print(f"\u5730\u5740: [bold]{url}[/bold]")
    console.print(f"\u7ed3\u679c\u76ee\u5f55: {results_root}")
    console.print("\u6309 Ctrl+C \u505c\u6b62\u670d\u52a1\u3002\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n\u670d\u52a1\u5df2\u505c\u6b62\u3002")
    finally:
        server.server_close()
