from __future__ import annotations

import asyncio
import json
import mimetypes
import socket
import threading
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
from trace_agent.application import AnalyzeApplication
from trace_agent.config import LlmRuntimeConfig
from trace_agent.models import (
    AgentKind,
    AnalyzeRequest,
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


@dataclass
class AnalysisJob:
    id: str
    status: str = "queued"  # queued | running | completed | failed
    case_name: str | None = None
    output_dir: Path | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


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
    return {
        "id": job.id,
        "status": job.status,
        "case_name": job.case_name,
        "output_dir": str(job.output_dir) if job.output_dir else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "duration_s": duration_s,
        "duration_display": _format_duration(duration_s, running=job.status in ("queued", "running")),
        "error": job.error,
        "params": job.params,
    }


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
            job.error = "trace_path is required"
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
        if agent is AgentKind.QODER:
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

        result = asyncio.run(application.run(request))

        job.completed_at = datetime.now(timezone.utc)
        job.status = "completed"
        job.case_name = output_dir.name
        _ = result
    except Exception as exc:  # noqa: BLE001 - surface user-facing job error
        job.completed_at = datetime.now(timezone.utc)
        job.status = "failed"
        job.error = str(exc)


def _submit_analysis(params: dict[str, Any], project_root: Path, results_root: Path) -> dict[str, Any]:
    job = AnalysisJob(id=f"job-{uuid.uuid4().hex[:12]}", params=params)
    with _JOB_LOCK:
        _JOBS[job.id] = job
    _JOB_EXECUTOR.submit(_run_analysis_job, job, project_root, results_root)
    return _job_view(job)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "DitingAgentDashboard/1.0"

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

        if path == "/api/analyze/jobs" or path == "/api/analyze/jobs/":
            with _JOB_LOCK:
                jobs = [_job_view(job) for job in _JOBS.values()]
            jobs.reverse()
            self._send_json({"jobs": jobs})
            return

        if path.startswith("/api/analyze/jobs/"):
            job_id = path.removeprefix("/api/analyze/jobs/").strip("/")
            with _JOB_LOCK:
                job = _JOBS.get(job_id)
            if job is None:
                self._send_json({"error": "job not found"}, HTTPStatus.NOT_FOUND)
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

    handler = type(
        "ConfiguredDashboardHandler",
        (DashboardHandler,),
        {"results_dir": results_root, "project_root": Path.cwd().resolve()},
    )
    bound_port = _get_free_port(host, port)

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
