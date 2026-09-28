"""Stage checkpointing for ephemeral runners.

The contract for every stage::

    if not cp.is_done("audio"):
        cp.begin("audio")
        ...produce files...
        cp.commit("audio", artifacts=["narration.mp3", "sentence_timing.json"])

``commit`` records each artifact's size and sha256 in ``state.json`` and then
delta-syncs the job to Drive. ``is_done`` re-verifies those artifacts, so a
stage whose files are missing or altered (partial Drive restore, manual edit,
disk cleanup) is redone instead of trusted.
"""
from __future__ import annotations

import traceback
from pathlib import Path
from typing import Iterable, Sequence

from src.errors import InvalidTransition
from src.state import manager
from src.utils.io import sha256
from src.utils.log import bind, get_logger

log = get_logger("checkpoint")
HASH_LIMIT_BYTES = 64 * 1024 * 1024  # larger files are verified by size only


class Checkpoint:
    def __init__(self, job_dir, job_id: str, channel: str, drive=None, exclude: Sequence[str] = ()):
        self.job_dir = Path(job_dir)
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.job_id = job_id
        self.channel = channel
        self.drive = drive
        self.exclude = tuple(exclude) + ("render/", "videos/", "final.mp4")
        self.path = self.job_dir / "state.json"
        if not self.path.exists():
            manager.init(self.path, job_id)
        bind(channel=channel, job=job_id)

    # ---------------------------------------------------------------- reading
    @property
    def state(self) -> dict:
        return manager.get(self.path)

    @property
    def current(self) -> str:
        return self.state.get("state", manager.QUEUED)

    def is_completed(self) -> bool:
        return self.current == manager.COMPLETED

    def is_done(self, key: str) -> bool:
        """True only if the stage is recorded complete AND its artifacts still verify."""
        data = self.state
        if key not in data.get("completed", []):
            return False
        problem = self._verify(data.get("artifacts", {}).get(key, {}))
        if problem:
            log.warning("stage artifacts failed verification; stage will be redone", stage=key, problem=problem)
            manager.invalidate_from(self.path, key, problem)
            return False
        return True

    def _verify(self, artifacts: dict) -> str:
        for rel, meta in artifacts.items():
            p = self.job_dir / rel
            if not p.is_file():
                return f"missing {rel}"
            size = p.stat().st_size
            if size != meta.get("size"):
                return f"size mismatch {rel}"
            if meta.get("sha256") and sha256(p) != meta["sha256"]:
                return f"hash mismatch {rel}"
        return ""

    # ---------------------------------------------------------------- writing
    def begin(self, key: str) -> None:
        st = manager.stage(key)
        bind(stage=key)
        manager.transition(self.path, st.active)
        log.info("stage started")

    def commit(self, key: str, artifacts: Iterable[str | Path] = (), **extra) -> None:
        st = manager.stage(key)
        recorded = {}
        for item in artifacts:
            p = Path(item)
            rel = p.relative_to(self.job_dir).as_posix() if p.is_absolute() else p.as_posix()
            full = self.job_dir / rel
            if not full.is_file():
                raise InvalidTransition(f"Cannot commit '{key}': artifact {rel} does not exist")
            size = full.stat().st_size
            recorded[rel] = {"size": size, "sha256": sha256(full) if size <= HASH_LIMIT_BYTES else None}
        all_artifacts = dict(self.state.get("artifacts", {}))
        all_artifacts[key] = recorded
        manager.transition(self.path, st.done, artifacts=all_artifacts, **extra)
        self.sync()
        log.info("stage committed", artifacts=len(recorded))

    def skip(self, key: str) -> None:
        manager.mark_skipped(self.path, key)
        self.sync()

    def sync(self, include: Iterable[str] = ()) -> None:
        """Delta-sync the job directory to Drive (no-op without Drive)."""
        if self.drive:
            self.drive.commit_job(self.job_dir, exclude=self.exclude, include=tuple(include))

    def fail(self, exc: BaseException, permanent: bool = False) -> None:
        state = manager.FAILED if permanent else manager.FAILED_RETRYABLE
        try:
            manager.transition(self.path, state, error=str(exc)[:4000], traceback=traceback.format_exc()[-8000:])
        except InvalidTransition:
            log.warning("job already terminal; failure not recorded")
            return
        # A failed job keeps its rendered video so the next run does not re-render.
        include = ("final.mp4",) if (self.job_dir / "final.mp4").is_file() else ()
        try:
            self.sync(include=include)
        except Exception as sync_exc:  # noqa: BLE001 - never mask the original failure
            log.error("could not sync failed job to Drive", error=str(sync_exc)[:300])

    def complete(self, **extra) -> None:
        manager.transition(self.path, manager.COMPLETED, **extra)
        self.sync()
