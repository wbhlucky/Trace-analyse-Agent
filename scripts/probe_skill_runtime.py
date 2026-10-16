# Phase 2a/2b probe: does the Claude Agent SDK really wire Agent Skills?
#
# Builds an isolated fixture project (tmp-agent/) containing one probe skill
# with two markers:
#   SKILL_MARKER_XYZ in SKILL.md            (layer 1)
#   DEEP_MARKER_QRS  in references/deep.md  (layer 2)
#
# Then runs a permission matrix against the real model to answer empirically:
#   1. Discovery - does the CLI find skills under .claude/skills/ when
#      setting_sources=["project"]?
#   2. Gating - does an explicit tools=[...] list (as production sets)
#      suppress the Skill tool even when skills=[...] is configured?
#   3. Progressive disclosure - does the marker reach the model only after a
#      tool call (Read/Skill/Bash), or is SKILL.md pre-injected into context?
#
# Nothing here touches production skill directories or the repo's .qoder/.

from __future__ import annotations

import asyncio
import io
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

FIXTURE = ROOT / "tmp-agent"
PLUGIN_FIXTURE = ROOT / "tmp-agent-plugin"
SKILL_L1_MARKER = "SKILL_MARKER_XYZ"
SKILL_L2_MARKER = "DEEP_MARKER_QRS"

_SKILL_MD_LINES = [
    "---",
    "name: skill-probe",
    'description: "Probe skill that verifies Agent Skills discovery and '
    'progressive disclosure. Use whenever the user asks for probe markers."',
    "---",
    "",
    "# Skill Probe",
    "",
    "The primary marker is " + SKILL_L1_MARKER + ".",
    "",
    "To obtain the secondary marker you must read the reference file",
    "`references/deep.md` relative to this skill directory.",
    "",
]

_DEEP_MD_LINES = [
    "# Deep Reference",
    "",
    "The secondary marker is " + SKILL_L2_MARKER + ".",
    "",
]

PLUGIN_JSON = (
    '{"name":"diting-skills","version":"0.1.0",'
    '"description":"Local plugin exposing the project Agent Skills to the CLI"}'
)

BASE_DISALLOWED = [
    "Edit",
    "Glob",
    "Grep",
    "NotebookEdit",
    "Task",
    "WebFetch",
    "WebSearch",
    "Write",
]

PROMPT = (
    "Use the skill-probe skill now. Then reply with exactly two lines and "
    "nothing else: the primary marker from its SKILL.md, and the secondary "
    "marker from its deep reference file."
)


@dataclass
class Variant:
    vid: str
    label: str
    skills: Any
    tools: list[str] | None
    allow_bash: bool = False
    allowed_tools: list[str] | None = None
    layout: str = "claude"
    plugins: list[dict[str, Any]] | None = None

    @property
    def root(self) -> Path:
        return PLUGIN_FIXTURE if self.layout == "plugin" else FIXTURE


@dataclass
class Observation:
    vid: str
    label: str
    ok: bool = False
    error: str | None = None
    elapsed_s: float = 0.0
    tool_calls: list[str] = field(default_factory=list)
    tool_inputs: list[dict[str, Any]] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    l1_in_text: bool = False
    l2_in_text: bool = False
    turns: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    system_messages: list[dict[str, Any]] = field(default_factory=list)
    session_id: str | None = None
    skill_listing: dict[str, Any] | None = None
    listing_bytes: int = 0


VARIANTS = [
    Variant(
        "V0",
        "production shape today: skills unset, tools=[Read]",
        skills=None,
        tools=["Read"],
        allowed_tools=["Read"],
    ),
    Variant(
        "V1",
        "skills set, tools=[Read] - does explicit tools= gate Skill?",
        skills=["skill-probe"],
        tools=["Read"],
        allowed_tools=["Read"],
    ),
    Variant(
        "V2",
        "skills set, tools=[Read,Skill]",
        skills=["skill-probe"],
        tools=["Read", "Skill"],
        allowed_tools=["Read", "Skill"],
    ),
    Variant(
        "V3",
        "skills set, tools=None - SDK/CLI decides",
        skills=["skill-probe"],
        tools=None,
        allowed_tools=None,
    ),
    Variant(
        "V4",
        "skills set, tools=[Read,Skill], Bash NOT disallowed",
        skills=["skill-probe"],
        tools=["Read", "Skill"],
        allow_bash=True,
        allowed_tools=["Read", "Skill"],
    ),
    Variant(
        "V5",
        "plugin discovery: .qoder as local plugin, no copy",
        skills=["skill-probe"],
        tools=["Read", "Skill"],
        allowed_tools=["Read", "Skill"],
        layout="plugin",
        plugins=[{"type": "local", "path": str(PLUGIN_FIXTURE / ".qoder")}],
    ),
    Variant(
        "V6",
        "plugin discovery, skills= unset",
        skills=None,
        tools=["Read", "Skill"],
        allowed_tools=["Read", "Skill"],
        layout="plugin",
        plugins=[{"type": "local", "path": str(PLUGIN_FIXTURE / ".qoder")}],
    ),
]


def build_fixture() -> None:
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    skill_dir = FIXTURE / ".claude" / "skills" / "skill-probe"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("\n".join(_SKILL_MD_LINES), encoding="utf-8")
    (skill_dir / "references" / "deep.md").write_text(
        "\n".join(_DEEP_MD_LINES), encoding="utf-8"
    )
    print("fixture built at " + str(FIXTURE))

    # Mirrors the real repo shape: skills live under .qoder/skills/ and the
    # .qoder directory itself is declared as a local CLI plugin.
    if PLUGIN_FIXTURE.exists():
        shutil.rmtree(PLUGIN_FIXTURE)
    qoder_skill = PLUGIN_FIXTURE / ".qoder" / "skills" / "skill-probe"
    (qoder_skill / "references").mkdir(parents=True)
    (qoder_skill / "SKILL.md").write_text(
        "\n".join(_SKILL_MD_LINES), encoding="utf-8"
    )
    (qoder_skill / "references" / "deep.md").write_text(
        "\n".join(_DEEP_MD_LINES), encoding="utf-8"
    )
    plugin_meta = PLUGIN_FIXTURE / ".qoder" / ".claude-plugin"
    plugin_meta.mkdir(parents=True)
    (plugin_meta / "plugin.json").write_text(PLUGIN_JSON, encoding="utf-8")
    print("plugin fixture built at " + str(PLUGIN_FIXTURE))


async def run_variant(variant: Variant, cfg: Any, env: dict[str, str]) -> Observation:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ToolUseBlock,
    )

    obs = Observation(vid=variant.vid, label=variant.label)
    disallowed = list(BASE_DISALLOWED)
    if not variant.allow_bash:
        disallowed.append("Bash")

    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "system_prompt": "You are a terse assistant. Follow instructions exactly.",
        "cwd": variant.root,
        "env": env,
        "setting_sources": ["project"],
        "disallowed_tools": disallowed,
        "max_turns": 8,
        "permission_mode": "dontAsk",
        "strict_mcp_config": True,
    }
    if variant.skills is not None:
        kwargs["skills"] = variant.skills
    if variant.plugins:
        kwargs["plugins"] = variant.plugins
    if variant.tools is not None:
        kwargs["tools"] = variant.tools
    if variant.allowed_tools is not None:
        kwargs["allowed_tools"] = variant.allowed_tools

    options = ClaudeAgentOptions(**kwargs)
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
                    record: dict[str, Any] = {"subtype": subtype}
                    if isinstance(payload, dict):
                        record["keys"] = sorted(payload.keys())
                        for key in (
                            "tools",
                            "slash_commands",
                            "skills",
                            "agents",
                            "mcp_servers",
                            "permissionMode",
                            "model",
                        ):
                            if key in payload:
                                record[key] = payload[key]
                    obs.system_messages.append(record)
                    if subtype == "init":
                        if isinstance(payload, dict):
                            sid = payload.get("session_id")
                            obs.session_id = sid if isinstance(sid, str) else None
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            obs.tool_calls.append(block.name)
                            raw = block.input if isinstance(block.input, dict) else {}
                            obs.tool_inputs.append(
                                {k: str(v)[:200] for k, v in raw.items()}
                            )
                        elif isinstance(block, TextBlock):
                            obs.text.append(block.text)
                elif isinstance(message, ResultMessage):
                    obs.turns = getattr(message, "num_turns", None)
                    usage = getattr(message, "usage", None)
                    if isinstance(usage, dict):
                        obs.usage = usage
                    if getattr(message, "result", None):
                        obs.text.append(str(message.result))
        obs.ok = True
    except Exception as exc:
        obs.error = (type(exc).__name__ + ": " + str(exc))[:900]
    obs.elapsed_s = round(time.perf_counter() - started, 1)
    obs.skill_listing, obs.listing_bytes = _read_skill_listing(
        variant.root, obs.session_id, started
    )

    joined = "\n".join(obs.text)
    obs.l1_in_text = SKILL_L1_MARKER in joined
    obs.l2_in_text = SKILL_L2_MARKER in joined
    return obs


def _read_skill_listing(
    root: Path, session_id: str | None, since: float
) -> tuple[dict[str, Any] | None, int]:
    """Pull the CLI's skill_listing attachment out of its session transcript.

    This is the ground truth for what layer-0 skill metadata actually reaches
    the model, which the SDK message stream alone does not expose.
    """
    if not session_id:
        return None, 0
    # The CLI encodes the project dir by mapping both ":" and path separators
    # to "-", so "D:\foo" becomes "D--foo".
    encoded = str(root).replace(":", "-").replace("\\", "-").replace("/", "-")
    project_dir = Path.home() / ".claude" / "projects" / encoded
    transcript = project_dir / (session_id + ".jsonl")
    if not transcript.is_file():
        candidates = [
            p
            for p in project_dir.glob("*.jsonl")
            if p.stat().st_mtime >= since - 5
        ] if project_dir.is_dir() else []
        if not candidates:
            return None, 0
        transcript = max(candidates, key=lambda p: p.stat().st_mtime)
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
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

    build_fixture()
    cfg = LlmRuntimeConfig.resolve(project_root=ROOT)
    env = cc.build_runtime_env(cfg)
    print("model=" + str(cfg.model) + " base_url=" + str(cfg.base_url))

    only = sys.argv[1:] or [v.vid for v in VARIANTS]
    results: list[Observation] = []
    for variant in VARIANTS:
        if variant.vid not in only:
            continue
        print("\n--- running " + variant.vid + ": " + variant.label)
        obs = await run_variant(variant, cfg, env)
        results.append(obs)
        print("    ok=%s elapsed=%ss turns=%s" % (obs.ok, obs.elapsed_s, obs.turns))
        print("    tools=" + json.dumps(obs.tool_calls, ensure_ascii=False))
        for name, inp in zip(obs.tool_calls, obs.tool_inputs):
            print("      " + name + " " + json.dumps(inp, ensure_ascii=False)[:220])
        print(
            "    L1_marker_in_text=%s L2_marker_in_text=%s"
            % (obs.l1_in_text, obs.l2_in_text)
        )
        print(
            "    skill_listing_injected=%s listing_bytes=%s names=%s"
            % (
                obs.skill_listing is not None,
                obs.listing_bytes,
                (obs.skill_listing or {}).get("names"),
            )
        )
        for rec in obs.system_messages:
            print("    sys " + json.dumps(rec, ensure_ascii=False)[:900])
        if obs.error:
            print("    ERROR: " + obs.error)
        else:
            print("    text=" + " | ".join(t.strip()[:160] for t in obs.text)[:400])

    dest = ROOT / "reports" / "skill-probe-matrix.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps([o.__dict__ for o in results], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("\nwrote " + str(dest))


if __name__ == "__main__":
    asyncio.run(main())
