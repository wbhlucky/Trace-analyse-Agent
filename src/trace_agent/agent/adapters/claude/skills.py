"""Claude-specific Agent Skill discovery bridge.

``SkillCatalog`` keeps the project skills under ``<root>/.qoder/skills/``. The
Qoder CLI reads that directory natively, but the Claude Code CLI only discovers
project skills under ``<cwd>/.claude/skills/`` or through a plugin. Rather than
duplicating the skill tree into ``.claude/``, ``.qoder`` itself is declared as a
local plugin, which keeps a single source of truth and leaves the ``PreToolUse``
Read whitelist (``skill.directory``) valid.

Verified empirically by ``scripts/probe_production_skills.py``: with the plugin
declared and ``skills=[...]`` set, the CLI injects a layer-0 listing containing
exactly the selected skills, the model calls ``Skill(...)`` for each, and
``Read`` of ``.qoder/skills/<name>/references/*.md`` succeeds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PLUGIN_NAME = "diting-agent-skills"
SKILLS_DIRNAME = ".qoder"
MANIFEST_RELPARTS = (".qoder", ".claude-plugin", "plugin.json")


MANIFEST_DESCRIPTION = (
    "Exposes the project-local Agent Skills under .qoder/skills to the "
    "Claude Code CLI plugin loader."
)


def ensure_skill_plugin(
    project_root: Path,
) -> tuple[list[dict[str, Any]] | None, str]:
    """Return the ``plugins=`` value for the Claude CLI, plus a status reason.

    The manifest is written on demand so the installed ``runtime_project``
    layout (which only ships ``.qoder/skills``) works without a packaging
    change. A read-only install degrades to ``None`` instead of failing the
    run; the caller records that so the regression stays visible.
    """
    root = Path(project_root)
    plugin_root = root / SKILLS_DIRNAME
    if not (plugin_root / "skills").is_dir():
        return None, "missing .qoder/skills"

    manifest = root.joinpath(*MANIFEST_RELPARTS)
    if not manifest.is_file():
        try:
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "name": PLUGIN_NAME,
                        "version": "0.1.0",
                        "description": MANIFEST_DESCRIPTION,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return None, f"cannot write plugin manifest: {exc}"

    return [{"type": "local", "path": str(plugin_root.resolve())}], "ok"
