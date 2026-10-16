# Phase 2c probe: does the REAL repo skill set reach the model through the
# Claude CLI plugin loader?
#
# Unlike probe_skill_runtime.py (isolated fixture), this points the CLI at the
# production discovery bridge: <repo>/.qoder declared as a local plugin, with
# the skill names ScenarioSkillRouter actually selects for a scenario.
#
# It answers three production questions:
#   1. Do plain skill names in skills=[...] match plugin-namespaced skills?
#   2. How large is the L0 skill_listing for the real skill descriptions?
#   3. Does the model call Skill(...) first and then Read a real reference
#      inside .qoder/skills/<name>/ (the path the Read hook whitelists)?

from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PLUGIN_PATH = ROOT / ".qoder"
SCHEMA_REF = (
    ".qoder/skills/trace-analysis/references/trace-streamer-schema.md"
)

PROMPT = (
    "Use the Skill tool to load every skill listed as enabled in your context, "
    "one call per skill. Then Read the file "
    f"{SCHEMA_REF} and report, in three short lines: (1) its first heading, "
    "(2) the name of the table that stores frame slices, (3) the exact column "
    "that identifies a process instance. Do not guess; only report what the "
    "file says."
)


async def run(
    label: str,
    skill_names: list[str],
    cfg: Any,
    env: dict[str, str],
) -> dict[str, Any]:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
    )

    options = ClaudeAgentOptions(
        model=cfg.model,
        system_prompt="You are a terse assistant. Follow instructions exactly.",
        cwd=ROOT,
        env=env,
        setting_sources=["project"],
        plugins=[{"type": "local", "path": str(PLUGIN_PATH)}],
        skills=skill_names,
        mcp_servers={},
        tools=["Read", "Skill"],
        allowed_tools=["Read", "Skill"],
        disallowed_tools=[
            "Bash",
            "Edit",
            "Glob",
            "Grep",
            "NotebookEdit",
            "Task",
            "WebFetch",
            "WebSearch",
            "Write",
        ],
        max_turns=12,
        permission_mode="dontAsk",
        strict_mcp_config=True,
    )

    tool_calls: list[str] = []
    tool_inputs: list[dict[str, Any]] = []
    texts: list[str] = []
    init_record: dict[str, Any] = {}
    session_id: str | None = None
    error: str | None = None
    turns: int | None = None
    usage: dict[str, Any] = {}
    started = time.perf_counter()
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(PROMPT)
            async for message in client.receive_response():
                if isinstance(message, SystemMessage):
                    subtype = getattr(message, "subtype", None)
                    if subtype == "thinking_tokens":
                        continue
                    payload = getattr(message, "data", None)
                    if subtype == "init" and isinstance(payload, dict):
                        init_record = {
                            "tools": payload.get("tools"),
                            "skills": payload.get("skills"),
                            "plugins": payload.get("plugins"),
                        }
                        sid = payload.get("session_id")
                        session_id = sid if isinstance(sid, str) else None
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(block.name)
                            raw = block.input if isinstance(block.input, dict) else {}
                            tool_inputs.append(
                                {k: str(v)[:220] for k, v in raw.items()}
                            )
                        elif isinstance(block, TextBlock):
                            texts.append(block.text)
                elif isinstance(message, ResultMessage):
                    turns = getattr(message, "num_turns", None)
                    got = getattr(message, "usage", None)
                    if isinstance(got, dict):
                        usage = got
                    if getattr(message, "result", None):
                        texts.append(str(message.result))
    except Exception as exc:
        error = (type(exc).__name__ + ": " + str(exc))[:900]
    elapsed = round(time.perf_counter() - started, 1)

    listing, listing_bytes = _read_skill_listing(ROOT, session_id, started)
    joined = "\n".join(texts)
    return {
        "label": label,
        "skills_requested": skill_names,
        "ok": error is None,
        "error": error,
        "elapsed_s": elapsed,
        "turns": turns,
        "usage": usage,
        "init_tools": init_record.get("tools"),
        "init_skills_count": (
            len(init_record["skills"])
            if isinstance(init_record.get("skills"), list)
            else init_record.get("skills")
        ),
        "init_plugins": init_record.get("plugins"),
        "tool_calls": tool_calls,
        "tool_inputs": tool_inputs,
        "skill_listing": listing,
        "skill_listing_bytes": listing_bytes,
        "schema_ref_read": any(
            "trace-streamer-schema.md" in json.dumps(inp, ensure_ascii=False)
            for inp in tool_inputs
        ),
        "mentions_frame_slice": "frame_slice" in joined,
        "mentions_ipid": "ipid" in joined.lower(),
        "text": joined[-1200:],
    }


def _read_skill_listing(
    root: Path, session_id: str | None, since: float
) -> tuple[dict[str, Any] | None, int]:
    if not session_id:
        return None, 0
    encoded = str(root).replace(":", "-").replace("\\", "-").replace("/", "-")
    project_dir = Path.home() / ".claude" / "projects" / encoded
    transcript = project_dir / (session_id + ".jsonl")
    if not transcript.is_file():
        candidates = (
            [
                p
                for p in project_dir.glob("*.jsonl")
                if p.stat().st_mtime >= since - 5
            ]
            if project_dir.is_dir()
            else []
        )
        if not candidates:
            return None, 0
        transcript = max(candidates, key=lambda p: p.stat().st_mtime)
    for line in transcript.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines():
        if "skill_listing" not in line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        attachment = record.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "skill_listing":
            content = attachment.get("content") or ""
            return attachment, len(content.encode("utf-8"))
    return None, 0


async def main() -> None:
    from trace_agent.agent.adapters.claude import client as cc
    from trace_agent.config import LlmRuntimeConfig
    from trace_agent.models import ScenarioType, TraceCapability
    from trace_agent.skills import ScenarioSkillRouter, SkillCatalog

    selected = ScenarioSkillRouter(SkillCatalog.project_default()).select(
        ScenarioType.COMPLETION_LATENCY,
        [TraceCapability.PERF_SAMPLES],
    )
    plain = [skill.name for skill in selected]
    namespaced = ["diting-agent-skills:" + name for name in plain]
    print("router selected: " + json.dumps(plain, ensure_ascii=False))

    cfg = LlmRuntimeConfig.resolve(project_root=ROOT)
    env = cc.build_runtime_env(cfg)
    print("model=" + str(cfg.model))

    cases: list[tuple[str, list[str]]] = []
    which = [a.upper() for a in sys.argv[1:]]
    if not which or "P1" in which:
        cases.append(("P1 plain names", plain))
    if "P2" in which:
        cases.append(("P2 namespaced names", namespaced))

    results = []
    for label, names in cases:
        print("\n--- running " + label + ": " + json.dumps(names))
        obs = await run(label, names, cfg, env)
        results.append(obs)
        print("    ok=%s elapsed=%ss turns=%s" % (obs["ok"], obs["elapsed_s"], obs["turns"]))
        print("    init_tools=" + json.dumps(obs["init_tools"], ensure_ascii=False))
        print("    init_skills_count=%s plugins=%s" % (obs["init_skills_count"], obs["init_plugins"]))
        print("    tools=" + json.dumps(obs["tool_calls"], ensure_ascii=False))
        for name, inp in zip(obs["tool_calls"], obs["tool_inputs"]):
            print("      " + name + " " + json.dumps(inp, ensure_ascii=False)[:240])
        listing = obs["skill_listing"] or {}
        print(
            "    listing_bytes=%s skillCount=%s names=%s"
            % (obs["skill_listing_bytes"], listing.get("skillCount"), listing.get("names"))
        )
        print("    usage=" + json.dumps(obs["usage"], ensure_ascii=False))
        print("    schema_ref_read=%s frame_slice=%s ipid=%s" % (
            obs["schema_ref_read"], obs["mentions_frame_slice"], obs["mentions_ipid"]))
        if obs["error"]:
            print("    ERROR: " + obs["error"])
        else:
            print("    text tail=" + obs["text"][-400:].replace("\n", " | "))

    dest = ROOT / "reports" / "production-skill-probe.json"
    dest.write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nwrote " + str(dest))


if __name__ == "__main__":
    asyncio.run(main())
