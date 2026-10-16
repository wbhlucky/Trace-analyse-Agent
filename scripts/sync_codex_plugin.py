# Sync the project-local Agent Skills into the Codex plugin bundle.
#
# Codex discovers plugin skills at <plugin-root>/skills/<name>/SKILL.md, while
# DitingAgent keeps one source of truth in .qoder/skills. The Claude bridge
# solves this by declaring .qoder itself as a local plugin; Codex installs a
# plugin archive instead, so the shared methodology skills are mirrored here.
# Generated files must never be edited by hand.
#
# The bundle also carries one Codex-only skill (`diting-agent`, the CLI driver)
# that is authored in place. It is listed in CODEX_ONLY_SKILLS and never pruned.
#
# Usage:
#   python scripts/sync_codex_plugin.py            # write the bundle
#   python scripts/sync_codex_plugin.py --check     # exit 1 on drift (CI)

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILLS = ROOT / ".qoder" / "skills"
PLUGIN_SKILLS = ROOT / "plugins" / "diting-agent" / "skills"

# Authored directly inside the plugin bundle; not mirrored, not pruned.
CODEX_ONLY_SKILLS = frozenset({"diting-agent"})

# CN translations stay in .qoder/skills for human readers. Codex reads the
# English SKILL.md and references, so the installed bundle stays lean.
EXCLUDE_SUFFIXES = ("CN.md",)
EXCLUDE_NAMES = frozenset({"SKILLCN.md", "__pycache__", ".DS_Store"})


def is_excluded(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES:
        return True
    return any(path.name.endswith(suffix) for suffix in EXCLUDE_SUFFIXES)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_files(skill_dir: Path) -> list[Path]:
    return sorted(
        item
        for item in skill_dir.rglob("*")
        if item.is_file()
        and not is_excluded(item)
        and not any(part in EXCLUDE_NAMES for part in item.relative_to(skill_dir).parts)
    )


def bundle_files(skill_dir: Path) -> list[Path]:
    return sorted(item for item in skill_dir.rglob("*") if item.is_file())


def sync_skill(skill_dir: Path, *, check: bool) -> tuple[list[str], list[str]]:
    """Return (changed, removed) repo-relative paths for one skill."""
    target_root = PLUGIN_SKILLS / skill_dir.name
    changed: list[str] = []
    removed: list[str] = []

    wanted: dict[Path, str] = {}
    for source in source_files(skill_dir):
        relative = source.relative_to(skill_dir)
        target = target_root / relative
        if not target.is_file() or digest(target) != digest(source):
            changed.append(str(target.relative_to(ROOT)))
            if not check:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        wanted[target.resolve()] = digest(source)

    if target_root.is_dir():
        for existing in bundle_files(target_root):
            if existing.resolve() in wanted:
                continue
            removed.append(str(existing.relative_to(ROOT)))
            if not check:
                existing.unlink()
        for stale_dir in sorted(
            (p for p in target_root.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            if not check and not any(stale_dir.iterdir()):
                stale_dir.rmdir()

    return changed, removed


def prune_orphans(*, check: bool) -> list[str]:
    """Drop bundled skills that no longer exist in .qoder/skills."""
    if not PLUGIN_SKILLS.is_dir():
        return []
    known = {path.name for path in SOURCE_SKILLS.iterdir() if path.is_dir()}
    known |= CODEX_ONLY_SKILLS
    orphans: list[str] = []
    for bundled in sorted(PLUGIN_SKILLS.iterdir()):
        if not bundled.is_dir() or bundled.name in known:
            continue
        orphans.append(str(bundled.relative_to(ROOT)))
        if not check:
            shutil.rmtree(bundled)
    return orphans


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mirror .qoder/skills into the Codex plugin bundle.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing; exits 1 when the bundle is stale",
    )
    args = parser.parse_args()

    if not SOURCE_SKILLS.is_dir():
        print(f"missing skill source: {SOURCE_SKILLS}", file=sys.stderr)
        return 2
    if not args.check:
        PLUGIN_SKILLS.mkdir(parents=True, exist_ok=True)

    changed: list[str] = []
    removed: list[str] = []
    for skill_dir in sorted(p for p in SOURCE_SKILLS.iterdir() if p.is_dir()):
        skill_changed, skill_removed = sync_skill(skill_dir, check=args.check)
        changed.extend(skill_changed)
        removed.extend(skill_removed)
    orphans = prune_orphans(check=args.check)

    verb = "stale" if args.check else "updated"
    if not changed and not removed and not orphans:
        print(f"codex plugin skills in sync: {len(list(PLUGIN_SKILLS.iterdir()))} skills")
        return 0

    for path in changed:
        print(f"{verb}: {path}")
    for path in removed + orphans:
        print(f"removed: {path}")
    if args.check:
        print(
            "rerun `python scripts/sync_codex_plugin.py` to refresh the bundle",
            file=sys.stderr,
        )
        return 1
    print(f"synced {len(changed)} file(s) into {PLUGIN_SKILLS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
