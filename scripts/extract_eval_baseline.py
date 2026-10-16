"""Extract baseline metrics from historical eval runs under .trace-agent/eval.

Reads eval-run.json, agent-result.json, agent-log.jsonl and run.json for every
trial of every run, and reports which agent implementation was used plus the
operational metrics needed for the Phase 0 baseline freeze.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVAL_ROOT = ROOT / ".trace-agent" / "eval"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def _dig(obj: Any, *keys: str) -> Any:
    cur = obj
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def summarize_run(run_dir: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {"run": run_dir.name}
    eval_run = _load(run_dir / "eval-run.json")
    summary["eval_run_keys"] = sorted(eval_run.keys()) if isinstance(eval_run, dict) else []
    summary["created_at"] = _dig(eval_run, "created_at") or _dig(eval_run, "started_at")
    summary["config"] = {
        k: v
        for k, v in (eval_run.items() if isinstance(eval_run, dict) else [])
        if k in {"agent", "agent_kind", "model", "provider", "trials", "case_ids", "cases"}
    }

    trials: list[dict[str, Any]] = []
    for case_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
        for trial_dir in sorted(p for p in case_dir.iterdir() if p.is_dir()):
            t: dict[str, Any] = {"case": case_dir.name, "trial": trial_dir.name}
            result = _load(trial_dir / "agent-result.json")
            if isinstance(result, dict):
                t["status"] = result.get("status")
                t["adapter"] = result.get("adapter") or result.get("agent") or result.get("sdk")
                t["turns"] = result.get("turns") or _dig(result, "diagnostics", "turns")
                t["result_keys"] = sorted(result.keys())
                diag = result.get("diagnostics") or {}
                if isinstance(diag, dict):
                    t["diag_keys"] = sorted(diag.keys())
                    for k in ("tool_calls", "tool_errors", "usage", "tokens", "duration_ms"):
                        if k in diag:
                            t[k] = diag[k]
            run_json = _load(trial_dir / "run.json")
            if isinstance(run_json, dict):
                t["manifest_agent"] = _dig(run_json, "agent") or _dig(run_json, "config", "agent")
                steps = run_json.get("steps")
                if isinstance(steps, dict):
                    aa = steps.get("agent.analyze") or {}
                    t["agent_step"] = {k: aa.get(k) for k in ("status", "duration_ms") if k in aa}
                elif isinstance(steps, list):
                    for s in steps:
                        if isinstance(s, dict) and s.get("name") == "agent.analyze":
                            t["agent_step"] = {k: s.get(k) for k in ("status", "duration_ms") if k in s}
            log_path = trial_dir / "agent-log.jsonl"
            if log_path.exists():
                names: list[str] = []
                errors = 0
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    tool = rec.get("tool") or rec.get("name") or rec.get("tool_name")
                    if tool:
                        names.append(str(tool))
                    if rec.get("error") or rec.get("is_error"):
                        errors += 1
                t["logged_tools"] = names
                t["logged_errors"] = errors
            perf = _load(trial_dir / "performance.json")
            if isinstance(perf, dict):
                t["perf_keys"] = sorted(perf.keys())
            trials.append(t)
    summary["trials"] = trials
    return summary


def main() -> None:
    out: list[dict[str, Any]] = []
    for run_dir in sorted(EVAL_ROOT.iterdir()):
        if not run_dir.is_dir():
            continue
        out.append(summarize_run(run_dir))
    dest = ROOT / "reports" / "eval-baseline-raw.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {dest}")
    for s in out:
        print("=" * 70)
        print(s["run"], s.get("created_at"))
        print("  config:", json.dumps(s.get("config"), ensure_ascii=False)[:300])
        for t in s["trials"]:
            print(
                f"  {t['case']}/{t['trial']} status={t.get('status')} "
                f"adapter={t.get('adapter')} turns={t.get('turns')} "
                f"tools={len(t.get('logged_tools') or [])} errors={t.get('logged_errors')}"
            )


if __name__ == "__main__":
    main()
