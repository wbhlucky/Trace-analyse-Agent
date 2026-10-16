"""Selenium harness for the local DitingAgent web panel.

The panel under test is the production one: ``trace_agent.web.serve`` runs in a
subprocess exactly as ``diting-agent serve`` launches it, pointed at a scratch
results directory seeded from ``results/``. Tests observe the real HTTP layer
and the real frontend through a real browser, so nothing is reimplemented here
and a regression in either half is caught.

Environment switches:

``DITING_WEBUI=0``        skip the whole suite (e.g. on a headless CI box)
``DITING_WEBUI_HEADED=1`` run Chrome visibly instead of headless
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_RESULTS = REPO_ROOT / "results"
ARTIFACT_ROOT = REPO_ROOT / "reports" / "webui"

CASES_TO_SEED = 3
STARTUP_TIMEOUT_S = 120.0
HEALTH_TIMEOUT_S = 60.0

# serve() prints the address it actually bound, and _get_free_port() may have
# moved it, so the printed URL is the only reliable source of truth.
_BOUND_URL_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost):(\d+)")

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _disabled() -> bool:
    return os.environ.get("DITING_WEBUI", "1").strip().lower() in _FALSY


def _headed() -> bool:
    return os.environ.get("DITING_WEBUI_HEADED", "").strip().lower() in _TRUTHY


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _seed_cases(destination: Path) -> list[str]:
    """Copy real result dirs into the scratch root so the panel has content."""
    if not SOURCE_RESULTS.is_dir():
        return []
    seeded: list[str] = []
    for child in sorted(SOURCE_RESULTS.iterdir()):
        if not child.is_dir() or not (child / "run.json").is_file():
            continue
        shutil.copytree(child, destination / child.name, dirs_exist_ok=True)
        seeded.append(child.name)
        if len(seeded) >= CASES_TO_SEED:
            break
    return seeded


@dataclass
class WebPanel:
    """A live panel plus the scratch directories it was pointed at."""

    url: str
    process: "subprocess.Popen[str]"
    results_dir: Path
    work_dir: Path
    cases: list[str]
    stdout: list[str] = field(default_factory=list)

    def get(self, path: str) -> str:
        with urlopen(f"{self.url}{path}", timeout=20) as response:  # noqa: S310
            return response.read().decode("utf-8")

    def get_json(self, path: str) -> Any:
        return json.loads(self.get(path))


@dataclass
class Browser:
    """Thin wrapper adding artifact capture to a Selenium driver."""

    driver: Any
    base_url: str
    label: str

    def open(self, path: str = "/") -> Any:
        self.driver.get(f"{self.base_url}{path}")
        return self.driver

    def console_messages(self, *levels: str) -> list[dict[str, Any]]:
        wanted = {level.upper() for level in levels} or {"SEVERE"}
        try:
            entries = self.driver.get_log("browser")
        except Exception:  # pragma: no cover - driver dependent
            return []
        return [e for e in entries if str(e.get("level", "")).upper() in wanted]

    def screenshot(self, suffix: str) -> Path:
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_ROOT / f"{self.label}-{suffix}.png"
        self.driver.save_screenshot(str(path))
        return path


def _drain(stream: Any, sink: list[str]) -> None:
    for line in stream:
        sink.append(line)


def _stop(process: "subprocess.Popen[str]") -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _tail(panel: WebPanel) -> str:
    return "".join(panel.stdout[-40:]) or "<no output>"


def _await_url(panel: WebPanel) -> str:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if panel.process.poll() is not None:
            raise RuntimeError(
                f"web panel exited early (code {panel.process.returncode}):\n{_tail(panel)}"
            )
        for line in reversed(panel.stdout):
            match = _BOUND_URL_RE.search(line)
            if match:
                return f"http://127.0.0.1:{match.group(1)}"
        time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for the panel address:\n{_tail(panel)}")


def _await_health(panel: WebPanel) -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    last = "no attempt"
    while time.monotonic() < deadline:
        if panel.process.poll() is not None:
            raise RuntimeError(
                f"web panel exited early (code {panel.process.returncode}):\n{_tail(panel)}"
            )
        try:
            payload = panel.get_json("/api/health")
            if isinstance(payload, dict) and payload.get("status") == "ok":
                return
            last = str(payload)
        except (URLError, OSError, ValueError) as exc:
            last = repr(exc)
        time.sleep(0.25)
    raise RuntimeError(f"panel never became healthy at {panel.url}: {last}")


@pytest.fixture(scope="session")
def web_panel(tmp_path_factory: pytest.TempPathFactory):
    if _disabled():
        pytest.skip("DITING_WEBUI=0: web panel evaluation disabled")

    work_dir = tmp_path_factory.mktemp("webui")
    results_dir = work_dir / "results"
    results_dir.mkdir()
    cases = _seed_cases(results_dir)
    if not cases:
        pytest.skip(f"no usable result directories under {SOURCE_RESULTS}")

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "NO_COLOR": "1",
        "COLUMNS": "200",
        # Keep the panel's memory store out of the repo and quiet the
        # background governance loop for the duration of the session.
        "TRACE_AGENT_MEMORY_ROOT": str(work_dir / "memory"),
        "TRACE_AGENT_MEMORY_DREAM_INTERVAL": "3600",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "trace_agent",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(_free_port()),
            "--no-open",
            "--results-dir",
            str(results_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    lines: list[str] = []
    threading.Thread(target=_drain, args=(process.stdout, lines), daemon=True).start()

    panel = WebPanel(
        url="",
        process=process,
        results_dir=results_dir,
        work_dir=work_dir,
        cases=cases,
        stdout=lines,
    )
    try:
        panel.url = _await_url(panel)
        _await_health(panel)
    except BaseException:
        _stop(process)
        raise

    yield panel
    _stop(process)


@pytest.fixture
def browser(request: pytest.FixtureRequest, web_panel: WebPanel) -> Browser:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError as exc:
        pytest.skip(f"selenium is not installed, run `uv sync --group dev`: {exc}")

    options = Options()
    if not _headed():
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--force-device-scale-factor=1")
    options.add_argument("--window-size=1440,900")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chrome/chromedriver unavailable: {exc}")

    driver.set_page_load_timeout(60)
    label = re.sub(r"[^\w.-]+", "-", request.node.name)
    try:
        yield Browser(driver=driver, base_url=web_panel.url, label=label)
    finally:
        driver.quit()
