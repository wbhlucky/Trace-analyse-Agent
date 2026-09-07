from __future__ import annotations

import json
import re
from pathlib import Path

from trace_agent.models import utc_now
from trace_agent.memory.models import (
    CuratedMemory,
    EpisodicMemory,
    ExecutionCheckpointSnapshot,
    MemoryStatus,
    SessionMemory,
)

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    return _SAFE_ID_RE.sub("-", value).strip("-") or "item"


class JsonMemoryBackend:
    """File-backed memory backend.

    Layout is deliberately simple and diff-friendly::

        <root>/episodes/<episode_id>.json
        <root>/curated/<memory_id>.json
        <root>/index.json

    Curated memories accumulate in a single ``fast`` file whose contents are
    the active, importable corpus used by recall. Full records remain
    individually addressable for provenance and governance.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.episodes_dir = self.root / "episodes"
        self.curated_dir = self.root / "curated"
        self.index_path = self.root / "index.json"

    @property
    def episodes_path(self) -> Path:
        return self.episodes_dir

    # ---------------------------------------------------------------- episode
    def episode_uri(self, episode_id: str) -> str:
        episode_id_ = _safe_name(episode_id)
        return str(self.episodes_dir / f"{episode_id_}.json")

    def write_episode(self, episode: EpisodicMemory) -> Path:
        path = self.episodes_dir / f"{_safe_name(episode.episode_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                episode.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def load_episode(self, episode_id: str) -> EpisodicMemory | None:
        path = self.episodes_dir / f"{_safe_name(episode_id)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return EpisodicMemory.model_validate(payload)
        except ValueError:
            return None

    def list_episodes(self) -> list[EpisodicMemory]:
        if not self.episodes_dir.is_dir():
            return []
        episodes: list[EpisodicMemory] = []
        for path in self.episodes_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                episodes.append(EpisodicMemory.model_validate(payload))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(episodes, key=lambda item: item.created_at, reverse=True)

    # ---------------------------------------------------------------- curated
    def write_curated(self, memory: CuratedMemory) -> Path:
        path = self.curated_dir / f"{_safe_name(memory.memory_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        memory = memory.model_copy(update={"updated_at": utc_now()})
        path.write_text(
            json.dumps(
                memory.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def load_curated(self, memory_id: str) -> CuratedMemory | None:
        path = self.curated_dir / f"{_safe_name(memory_id)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return CuratedMemory.model_validate(payload)
        except ValueError:
            return None

    def list_curated(
        self,
        *,
        active_only: bool = False,
    ) -> list[CuratedMemory]:
        if not self.curated_dir.is_dir():
            return []
        memories: list[CuratedMemory] = []
        for path in self.curated_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                memory = CuratedMemory.model_validate(payload)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if active_only and memory.status in {
                MemoryStatus.SUPERSEDED,
                MemoryStatus.ARCHIVED,
            }:
                continue
            memories.append(memory)
        return sorted(memories, key=lambda item: item.updated_at, reverse=True)

    def remove_curated(self, memory_id: str) -> bool:
        path = self.curated_dir / f"{_safe_name(memory_id)}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True
    @property
    def sessions_dir(self) -> Path:
        return self.root / "sessions"

    @property
    def dreams_path(self) -> Path:
        return self.root / "dreaming" / "queue.jsonl"

    def write_session(self, session: SessionMemory) -> Path:
        path = self.sessions_dir / f"{_safe_name(session.session_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        session = session.model_copy(update={"updated_at": utc_now()})
        path.write_text(
            json.dumps(
                session.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    def load_session(self, session_id: str) -> SessionMemory | None:
        path = self.sessions_dir / f"{_safe_name(session_id)}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        try:
            return SessionMemory.model_validate(payload)
        except ValueError:
            return None

    def list_sessions(self) -> list[SessionMemory]:
        if not self.sessions_dir.is_dir():
            return []
        sessions: list[SessionMemory] = []
        for path in self.sessions_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                sessions.append(SessionMemory.model_validate(payload))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def append_dream_job(self, payload: dict[str, object]) -> None:
        path = self.dreams_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str))
            handle.write("\n")

    def read_dream_jobs(self, *, limit: int = 100) -> list[dict[str, object]]:
        path = self.dreams_path
        if not path.is_file():
            return []
        items: list[dict[str, object]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(payload, dict):
                    items.append(payload)
                if len(items) >= limit:
                    break
        except OSError:
            return []
        return items

    def clear_dream_jobs(self) -> None:
        try:
            self.dreams_path.unlink(missing_ok=True)
        except OSError:
            return

    @staticmethod
    def execution_snapshot(output_dir: str | Path) -> ExecutionCheckpointSnapshot:
        import json as _json

        output = Path(output_dir)
        payload: dict[str, object] = {}
        run_path = output / "run.json"
        if run_path.is_file():
            try:
                raw = _json.loads(run_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    payload = raw
            except (OSError, ValueError, json.JSONDecodeError):
                payload = {}
        step_states = payload.get("step_states") or []
        completed = [
            str(state.get("step"))
            for state in step_states
            if isinstance(state, dict) and state.get("status") == "done"
        ]
        failed = [
            str(state.get("step"))
            for state in step_states
            if isinstance(state, dict) and state.get("status") == "failed"
        ]

        # run.json may not include the latest step files (especially during
        # interrupted runs), so merge durable steps/ files as a second source.
        steps_dir = output / "steps"
        if steps_dir.is_dir():
            for state_path in sorted(steps_dir.glob("*.json")):
                try:
                    raw_state = _json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(raw_state, dict):
                    continue
                step = str(raw_state.get("step") or "")
                status = raw_state.get("status")
                if step and status == "done" and step not in completed:
                    completed.append(step)
                elif step and status == "failed" and step not in failed:
                    failed.append(step)
        return ExecutionCheckpointSnapshot(
            output_dir=str(output),
            run_id=payload.get("run_id") if isinstance(payload, dict) else None,
            status=payload.get("status") if isinstance(payload, dict) else None,
            resume_from=(
                payload.get("resume_from")
                if isinstance(payload, dict)
                else None
            ),
            completed_steps=completed,
            failed_steps=failed,
            pending_steps=[],
            resumable=(
                payload.get("status")
                in {"running", "interrupted", "resumable", "running-recovery"}
                if isinstance(payload, dict)
                else False
            ),
        )
