# Phase 5: build the Before/After metric table from eval artifacts plus the
# Claude CLI session transcripts (the only place token usage lives).

from __future__ import annotations

import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = (
    Path.home()
    / ".claude"
    / "projects"
    / str(ROOT).replace(":", "-").replace("\\", "-").replace("/", "-")
)


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def transcript(session_id: str) -> Path | None:
    path = PROJECT_DIR / (session_id + ".jsonl")
    return path if path.is_file() else None


def transcript_metrics(path: Path) -> dict[str, Any]:
    in_tokens = 0
    out_tokens = 0
    cache_read = 0
    cache_create = 0
    listing_bytes = 0
    listing_names: list[str] = []
    memory_marker = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "历史分析记忆" in line:
            memory_marker = True
        try:
            record = json.loads(line)
        except ValueError:
            continue
        attachment = record.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "skill_listing":
            content = attachment.get("content") or ""
            listing_bytes += len(content.encode("utf-8"))
            listing_names = attachment.get("names") or listing_names
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            in_tokens += int(usage.get("input_tokens") or 0)
            out_tokens += int(usage.get("output_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            cache_create += int(usage.get("cache_creation_input_tokens") or 0)
    return {
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_create,
        "skill_listing_bytes": listing_bytes,
        "skill_listing_names": listing_names,
        "memory_prompt_present": memory_marker,
    }


def run_metrics(eval_dir: Path) -> list[dict[str, Any]]:
    payload = json.loads((eval_dir / "eval-run.json").read_text(encoding="utf-8"))
    grades = {
        item["trial_id"]: item for item in payload.get("grader_results", [])
    }
    rows: list[dict[str, Any]] = []
    case_dir = next(
        (p for p in eval_dir.iterdir() if p.is_dir() and p.name != "logs"), None
    )
    for trial in payload.get("trials", []):
        case_id = trial["case_id"]
        trial_dirs = sorted((eval_dir / case_id).glob("trial-*"))
        index = trial.get("index")
        trial_dir = (
            trial_dirs[index] if index is not None and index < len(trial_dirs) else None
        )
        row: dict[str, Any] = {
            "case_id": case_id,
            "index": index,
            "status": trial.get("status"),
            "failure": trial.get("failure"),
            "elapsed_s": round((trial.get("elapsed_ms") or 0) / 1000.0, 1),
            "error": str(trial.get("error") or "")[:200] or None,
        }
        calls = trial.get("tool_calls") or []
        row["mcp_tool_calls"] = len(calls)
        row["mcp_tool_errors"] = sum(
            1 for c in calls if c.get("status") != "success"
        )
        row["query_trace_sql_calls"] = sum(
            1 for c in calls if c.get("tool") == "query_trace_sql"
        )
        row["sequence"] = [c.get("tool") for c in calls]
        # Repeated identical calls are explicitly forbidden by the system
        # prompt, so the duplicate count is a direct read on prompt adherence.
        signatures: dict[str, int] = {}
        for call in calls:
            key = json.dumps(
                [call.get("tool"), call.get("arguments") or {}],
                sort_keys=True,
                ensure_ascii=False,
            )
            signatures[key] = signatures.get(key, 0) + 1
        row["repeated_calls"] = sum(n - 1 for n in signatures.values())
        row["repeated_tools"] = sorted(
            {
                json.loads(key)[0]
                for key, count in signatures.items()
                if count > 1
            }
        )
        grade = grades.get(trial.get("trial_id"))
        if grade:
            row["overall_score"] = grade.get("overall")
            row["components"] = {
                c["component"]: c["score"] for c in grade.get("components", [])
            }
        session_id = None
        skill_access: list[dict[str, Any]] = []
        if trial_dir is not None:
            agent_result = trial_dir / "agent-result.json"
            if agent_result.is_file():
                data = json.loads(agent_result.read_text(encoding="utf-8"))
                for attempt in data.get("attempts", []):
                    message = attempt.get("result_message") or {}
                    if message.get("session_id"):
                        session_id = message["session_id"]
                    if message.get("num_turns") is not None:
                        row["num_turns"] = message["num_turns"]
                row["tool_budget"] = data.get("tool_budget", {}).get(
                    "invocation_count"
                )
            access = trial_dir / "skill-access.jsonl"
            if access.is_file():
                skill_access = [
                    json.loads(line)
                    for line in access.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if line.strip()
                ]
        row["skill_calls"] = [
            e.get("skill")
            for e in skill_access
            if e.get("tool") == "Skill" and e.get("decision") == "allow"
        ]
        row["skill_read_calls"] = sum(
            1
            for e in skill_access
            if e.get("tool") == "Read" and e.get("decision") == "allow"
        )
        row["skill_read_denied"] = [
            (e.get("resolved_path"), e.get("reason"))
            for e in skill_access
            if e.get("tool") == "Read" and e.get("decision") == "deny"
        ]
        # "Skill read -> tool call" ordering: a methodology that makes the agent
        # chattier shows up as many evidence calls after the first skill load.
        skill_stamps = [
            stamp
            for stamp in (
                _parse_ts(entry.get("timestamp"))
                for entry in skill_access
                if entry.get("tool") in ("Skill", "Read")
                and entry.get("decision") == "allow"
            )
            if stamp is not None
        ]
        if skill_stamps:
            first_skill = min(skill_stamps)
            row["first_skill_load_at"] = first_skill.isoformat()
            row["tool_calls_after_first_skill"] = sum(
                1
                for call in calls
                if (_parse_ts(call.get("started_at")) or first_skill) >= first_skill
            )
        else:
            row["tool_calls_after_first_skill"] = None
        # The audit, not the transcript, is the authoritative record of what the
        # adapter injected: CLI transcripts do not carry the system prompt.
        discovery = next(
            (e for e in skill_access if e.get("tool") == "skill_discovery"), None
        )
        row["skill_discovery"] = (
            f"{discovery.get('decision')}:{discovery.get('reason')}"
            if discovery
            else None
        )
        memory = next(
            (e for e in skill_access if e.get("tool") == "memory_context"), None
        )
        row["memory_decision"] = memory.get("decision") if memory else None
        for part in str((memory or {}).get("reason") or "").split(","):
            key, _, value = part.partition("=")
            if key in ("recall_chars", "prompt_chars") and value.isdigit():
                row["memory_" + key] = int(value)
        if session_id:
            row["session_id"] = session_id
            path = transcript(session_id)
            if path is not None:
                row.update(transcript_metrics(path))
            else:
                row["transcript"] = "missing"
        rows.append(row)
    del case_dir
    return rows


def resolve_eval_dir(path: Path) -> Path:
    """Accept either an eval run dir or the output root that contains it."""
    if (path / "eval-run.json").is_file() or not path.is_dir():
        return path
    candidates = [
        child
        for child in path.iterdir()
        if child.is_dir() and (child / "eval-run.json").is_file()
    ]
    if not candidates:
        return path
    return max(
        candidates,
        key=lambda child: (child / "eval-run.json").stat().st_mtime,
    )


def main() -> None:
    labels = {
        "baseline": ".trace-agent/eval-phase0-baseline/eval-7e94951cfc71",
        "fixed": ".trace-agent/eval-phase4-fixed/eval-548849fa5ba7",
    }
    for extra in sys.argv[1:]:
        name, _, rel = extra.partition("=")
        labels[name] = rel
    out: dict[str, Any] = {}
    for label, rel in labels.items():
        path = resolve_eval_dir(ROOT / rel)
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        if not (path / "eval-run.json").is_file():
            print("skip " + label + ": " + str(path))
            continue
        out[label] = {"dir": rel, "trials": run_metrics(path)}
        print("\n===== " + label + " (" + rel + ")")
        for row in out[label]["trials"]:
            print(
                " trial %s status=%s score=%s elapsed=%ss turns=%s mcp=%s err=%s sql=%s"
                % (
                    row.get("index"),
                    row.get("status"),
                    row.get("overall_score"),
                    row.get("elapsed_s"),
                    row.get("num_turns"),
                    row.get("mcp_tool_calls"),
                    row.get("mcp_tool_errors"),
                    row.get("query_trace_sql_calls"),
                )
            )
            print(
                "   tokens in=%s out=%s cache_read=%s cache_create=%s listing=%sB memory=%s"
                % (
                    row.get("input_tokens"),
                    row.get("output_tokens"),
                    row.get("cache_read_input_tokens"),
                    row.get("cache_creation_input_tokens"),
                    row.get("skill_listing_bytes"),
                    row.get("memory_decision"),
                )
            )
            print(
                "   memory recall_chars=%s prompt_chars=%s transcript_marker=%s"
                % (
                    row.get("memory_recall_chars"),
                    row.get("memory_prompt_chars"),
                    row.get("memory_prompt_present"),
                )
            )
            print("   skill_discovery=" + str(row.get("skill_discovery")))
            print("   skills=" + json.dumps(row.get("skill_calls"), ensure_ascii=False))
            print(
                "   skill_reads=%s denied=%s"
                % (row.get("skill_read_calls"), row.get("skill_read_denied"))
            )
            print(
                "   repeated_calls=%s repeated_tools=%s mcp_after_first_skill=%s"
                % (
                    row.get("repeated_calls"),
                    row.get("repeated_tools"),
                    row.get("tool_calls_after_first_skill"),
                )
            )
            print("   components=" + json.dumps(row.get("components"), ensure_ascii=False))
            if row.get("error"):
                print("   error=" + row["error"])
    dest = ROOT / "reports" / "phase5-before-after.json"
    dest.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nwrote " + str(dest))


if __name__ == "__main__":
    main()
