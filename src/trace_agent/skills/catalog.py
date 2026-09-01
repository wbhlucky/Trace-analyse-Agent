from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from trace_agent.models import SkillSnapshot


@dataclass(frozen=True, slots=True)
class SkillDefinition:
    name: str
    directory: Path
    project_root: Path
    fingerprint: str

    def snapshot(self) -> SkillSnapshot:
        return SkillSnapshot(
            name=self.name,
            fingerprint=self.fingerprint,
        )


class SkillCatalog:
    """Discover and fingerprint project-local Agent Skills."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()

    @classmethod
    def project_default(cls) -> SkillCatalog:
        source_root = Path(__file__).resolve().parents[3]
        source_skills = source_root / ".qoder" / "skills"
        if source_skills.is_dir():
            return cls(source_root)

        installed_root = Path(__file__).resolve().parents[1] / "runtime_project"
        return cls(installed_root)

    def require(self, name: str) -> SkillDefinition:
        skill_dir = self._project_root / ".qoder" / "skills" / name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(
                f"缺少必需的 Agent Skill：{skill_file}"
            )

        return SkillDefinition(
            name=name,
            directory=skill_dir,
            project_root=self._project_root,
            fingerprint=self._fingerprint(skill_dir),
        )

    def require_many(self, names: list[str]) -> list[SkillDefinition]:
        return [self.require(name) for name in names]

    def list_available(self) -> list[str]:
        skills_root = self._project_root / ".qoder" / "skills"
        if not skills_root.is_dir():
            return []
        return sorted(
            path.parent.name
            for path in skills_root.glob("*/SKILL.md")
            if path.is_file()
        )

    @staticmethod
    def _fingerprint(skill_dir: Path) -> str:
        digest = sha256()
        for path in sorted(
            item for item in skill_dir.rglob("*") if item.is_file()
        ):
            digest.update(path.relative_to(skill_dir).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"
