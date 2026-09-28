"""Google Drive persistence with a commit protocol.

Design
------
* **Delta sync.** Files are compared by md5 (Drive's ``md5Checksum``) and only
  changed files are uploaded. A local ledger (``.commit.json``) remembers each
  file's size+mtime so unchanged files are not even re-hashed.
* **Commit marker.** After the data files are uploaded, ``_commit.json`` is
  written LAST. It carries a monotonically increasing ``commit_seq`` and the
  manifest (path, size, md5). Stage completion is additionally verified by
  ``Checkpoint`` against per-artifact hashes recorded in ``state.json``, so a
  half-uploaded snapshot can never be mistaken for a finished stage.
* **Newest wins on restore.** A local job directory is only overwritten when
  Drive holds a higher ``commit_seq``.
* **Race-safe folders.** After creating a folder we re-list; if a concurrent
  runner also created one, the oldest folder wins and ours is removed.
* **Merge-sync for small shared JSON** (queues, history): local and remote are
  merged with a caller-supplied function and both sides are updated, so a stale
  copy can never overwrite newer state.
"""
from __future__ import annotations

import fnmatch
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

from src.drive.backends import Backend, Entry, GoogleBackend
from src.errors import ConfigError, IntegrityError
from src.utils.io import md5 as file_md5, read_json, write_json
from src.utils.log import get_logger

log = get_logger("drive")
COMMIT_NAME = "_commit.json"
LEDGER_NAME = ".commit.json"

# Never synchronised: scratch space, partial files, locks, our own ledger.
ALWAYS_EXCLUDE = (".tmp/", "*.part", "*.tmp", ".*.lock", LEDGER_NAME)


@dataclass
class RestoreResult:
    restored: bool
    commit_seq: int = 0
    reason: str = ""


class DriveStore:
    def __init__(self, backend: Backend, channel: str = "", budget=None):
        self.backend = backend
        self.channel = channel
        self.budget = budget
        self._folders: dict[tuple[str, str], str] = {}
        self._children_cache: dict[str, dict[str, Entry]] = {}

    # ------------------------------------------------------------------ setup
    @classmethod
    def for_channel(cls, channel: str, settings, env: Mapping[str, str] | None = None, budget=None,
                    required: bool | None = None) -> "DriveStore | None":
        env = os.environ if env is None else env
        prefix = {"audiobook": "AUDIOBOOK_"}.get(channel)
        if not prefix:
            raise ConfigError(f"Unknown channel '{channel}' for Drive storage")
        required = settings.drive.require_persistence if required is None else required
        creds = env.get(f"{prefix}DRIVE_CREDENTIALS", "").strip()
        root = env.get(f"{prefix}DRIVE_ROOT_FOLDER_ID", "").strip()
        if not creds or not root:
            if required:
                raise ConfigError(f"Drive persistence is required for '{channel}' but "
                                  f"{prefix}DRIVE_CREDENTIALS / {prefix}DRIVE_ROOT_FOLDER_ID is missing")
            return None
        backend = GoogleBackend(creds, root, require_narrow_scope=settings.drive.require_narrow_scope)
        return cls(backend, channel, budget)

    # ---------------------------------------------------------------- folders
    def _list(self, parent: str, refresh: bool = False) -> dict[str, Entry]:
        if refresh or parent not in self._children_cache:
            entries: dict[str, Entry] = {}
            # Oldest first: on duplicate names the earliest entry wins deterministically.
            for e in sorted(self.backend.list_children(parent), key=lambda x: (x.created, x.id)):
                entries.setdefault((e.name + ("/" if e.is_folder else "")), e)
            self._children_cache[parent] = entries
        return self._children_cache[parent]

    def _find(self, parent: str, name: str, folder: bool, refresh: bool = False) -> Entry | None:
        return self._list(parent, refresh).get(name + ("/" if folder else ""))

    def folder(self, *parts: str, parent: str | None = None) -> str:
        current = parent or self.backend.root_id
        for part in parts:
            current = self._ensure_folder(part, current)
        return current

    def _ensure_folder(self, name: str, parent: str) -> str:
        key = (parent, name)
        if key in self._folders:
            return self._folders[key]
        existing = self._find(parent, name, True)
        if existing:
            self._folders[key] = existing.id
            return existing.id
        mine = self.backend.create_folder(name, parent)
        # Another runner may have created the same folder between our check and create.
        self._children_cache.pop(parent, None)
        winners = sorted(
            (e for e in self.backend.list_children(parent) if e.is_folder and e.name == name),
            key=lambda x: (x.created, x.id),
        )
        winner = winners[0].id if winners else mine
        if winner != mine:
            log.warning("duplicate folder race detected; keeping oldest", folder=name)
            try:
                self.backend.delete(mine)
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort only
                log.warning("could not remove duplicate folder", error=str(exc)[:120])
        self._children_cache.pop(parent, None)
        self._folders[key] = winner
        return winner

    def list_folder(self, folder_id: str | None = None, refresh: bool = True) -> list[Entry]:
        """List direct children of a Drive folder."""
        parent = folder_id or self.backend.root_id
        return list(self._list(parent, refresh=refresh).values())

    def find_folder(self, name: str, parent_id: str | None = None) -> Entry | None:
        """Find the oldest direct child folder with an exact name."""
        return self._find(parent_id or self.backend.root_id, name, True, refresh=True)

    def download_entry(self, entry: Entry, local_path: Path) -> None:
        """Download a Drive file entry without treating its bytes as text."""
        if entry.is_folder:
            raise ConfigError(f"Cannot download Drive folder as a file: {entry.name}")
        self.backend.download(entry.id, Path(local_path))

    # ------------------------------------------------------------------ files
    def put_file(self, local: Path, parent: str, name: str | None = None, md5_hint: str | None = None) -> bool:
        """Upload ``local`` unless Drive already holds identical bytes. Returns True if uploaded."""
        local = Path(local)
        name = name or local.name
        digest = md5_hint or file_md5(local)
        existing = self._find(parent, name, False)
        if existing and existing.md5 == digest:
            return False
        if self.budget:
            self.budget.spend("drive_upload_mb", local.stat().st_size / (1024 * 1024))
        entry = self.backend.upload(local, parent, name, existing.id if existing else None)
        self._list(parent)[name] = entry
        return True

    def read_file_json(self, parent: str, name: str):
        entry = self._find(parent, name, False, refresh=True)
        if not entry:
            return None
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "x.json"
            self.backend.download(entry.id, target)
            try:
                return json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise IntegrityError(f"Corrupt JSON on Drive: {name}") from exc

    def write_file_json(self, parent: str, name: str, data) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / name
            write_json(target, data)
            self.put_file(target, parent, name)

    # ------------------------------------------------------------ job commits
    @staticmethod
    def _excluded(rel: str, patterns: Iterable[str]) -> bool:
        """``dir/`` patterns exclude a whole directory; others are fnmatch globs."""
        for pattern in patterns:
            if pattern.endswith("/"):
                if rel.startswith(pattern) or f"/{pattern}" in f"/{rel}":
                    return True
            elif fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(Path(rel).name, pattern):
                return True
        return False

    def _walk(self, job_dir: Path, exclude: Iterable[str]) -> list[tuple[str, Path]]:
        patterns = tuple(exclude) + ALWAYS_EXCLUDE
        out = []
        for path in sorted(job_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(job_dir).as_posix()
                if not self._excluded(rel, patterns):
                    out.append((rel, path))
        return out

    def _job_folder(self, job_id: str) -> str:
        return self.folder("jobs", job_id)

    def commit_job(self, job_dir, exclude: Iterable[str] = (), include: Iterable[str] = ()) -> int:
        """Upload changed files, then the commit marker. Returns the new commit_seq.

        ``include`` names extra paths (relative) that override ``exclude`` once,
        e.g. ``final.mp4`` after a failure.
        """
        job_dir = Path(job_dir)
        job_id = job_dir.name
        ledger = read_json(job_dir / LEDGER_NAME, {}) or {}
        known: dict[str, dict] = dict(ledger.get("files", {}))
        seq = int(ledger.get("commit_seq", 0))
        remote_job = self._job_folder(job_id)
        include_set = set(include)
        patterns = [p for p in exclude if p not in include_set]
        current: dict[str, dict] = {}
        pending: list[tuple[str, Path, str]] = []
        for rel, path in self._walk(job_dir, patterns):
            st = path.stat()
            prior = known.get(rel)
            if prior and prior.get("size") == st.st_size and prior.get("mtime_ns") == st.st_mtime_ns:
                current[rel] = prior
                continue
            pending.append((rel, path, file_md5(path)))
        # State file last among data files: it must never describe artifacts that are not on Drive yet.
        pending.sort(key=lambda t: (t[0] == "state.json", t[0]))
        uploaded = 0
        for rel, path, digest in pending:
            parent = self.folder(*Path(rel).parts[:-1], parent=remote_job) if "/" in rel else remote_job
            if self.put_file(path, parent, Path(rel).name, md5_hint=digest):
                uploaded += 1
            st = path.stat()
            current[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "md5": digest}
        if not pending and set(current) == set(known) and ledger.get("commit_seq") is not None and uploaded == 0:
            return seq  # nothing changed; do not burn a commit
        seq += 1
        manifest = {"job_id": job_id, "commit_seq": seq, "committed_at": time.time(),
                    "files": {k: {"size": v["size"], "md5": v["md5"]} for k, v in current.items()}}
        self.write_file_json(remote_job, COMMIT_NAME, manifest)  # commit marker, written last
        write_json(job_dir / LEDGER_NAME, {"commit_seq": seq, "files": current})
        log.info("job committed", job=job_id, seq=seq, uploaded=uploaded, tracked=len(current))
        return seq

    def remote_commit_seq(self, job_id: str) -> int | None:
        parent = self._find(self.folder("jobs"), job_id, True, refresh=True)
        if not parent:
            return None
        data = self.read_file_json(parent.id, COMMIT_NAME)
        return int(data["commit_seq"]) if isinstance(data, dict) and "commit_seq" in data else 0

    def restore_job(self, job_id: str, job_dir, force: bool = False) -> RestoreResult:
        """Download a job snapshot when Drive is newer than (or missing locally)."""
        job_dir = Path(job_dir)
        remote_seq = self.remote_commit_seq(job_id)
        if remote_seq is None:
            return RestoreResult(False, 0, "no remote job")
        ledger = read_json(job_dir / LEDGER_NAME, {}) or {}
        local_seq = int(ledger.get("commit_seq", -1)) if (job_dir / "state.json").is_file() else -1
        if not force and local_seq >= remote_seq:
            return RestoreResult(False, local_seq, "local is current")
        remote_job = self._find(self.folder("jobs"), job_id, True).id
        files: dict[str, dict] = {}
        self._download_tree(remote_job, job_dir, "", files)
        manifest = self.read_file_json(remote_job, COMMIT_NAME) or {}
        for rel, meta in (manifest.get("files") or {}).items():
            got = files.get(rel)
            if not got or got["md5"] != meta.get("md5"):
                log.warning("restored file differs from commit manifest (partial commit); stage verification will redo it",
                            file=rel)
        write_json(job_dir / LEDGER_NAME, {"commit_seq": remote_seq, "files": files})
        log.info("job restored", job=job_id, seq=remote_seq, files=len(files))
        return RestoreResult(True, remote_seq, "restored")

    def _download_tree(self, remote_parent: str, local_dir: Path, prefix: str, files: dict) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        for name, entry in sorted(self._list(remote_parent, refresh=True).items()):
            if entry.is_folder:
                self._download_tree(entry.id, local_dir / entry.name, f"{prefix}{entry.name}/", files)
                continue
            if entry.name == COMMIT_NAME:
                continue
            target = local_dir / entry.name
            rel = f"{prefix}{entry.name}"
            if not (target.is_file() and file_md5(target) == entry.md5):
                self.backend.download(entry.id, target)
            st = target.stat()
            files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "md5": entry.md5 or file_md5(target)}

    # ------------------------------------------------------ shared small JSON
    def sync_json(self, parts: tuple[str, ...], local_path, merge: Callable[[dict | None, dict | None], dict],
                  write_local: bool = True) -> dict:
        """Merge local and remote JSON with ``merge(local, remote)`` and update BOTH sides."""
        local_path = Path(local_path)
        parent = self.folder(*parts[:-1])
        name = parts[-1]
        remote = self.read_file_json(parent, name)
        local = read_json(local_path, None)
        merged = merge(local if isinstance(local, dict) else None, remote if isinstance(remote, dict) else None)
        if write_local and merged != local:
            write_json(local_path, merged)
        if merged != remote:
            self.write_file_json(parent, name, merged)
        return merged

    # -------------------------------------------------------- active pointer
    def set_active_job(self, job_id: str, **info) -> None:
        self.write_file_json(self.folder("state"), "active_job.json",
                             {"job_id": job_id, "updated_at": time.time(), **info})

    def get_active_job(self) -> dict | None:
        data = self.read_file_json(self.folder("state"), "active_job.json")
        return data if isinstance(data, dict) and data.get("job_id") else None

    def clear_active_job(self, job_id: str) -> None:
        current = self.get_active_job()
        if current and current.get("job_id") == job_id:
            self.write_file_json(self.folder("state"), "active_job.json", {"job_id": None, "updated_at": time.time()})

    # ------------------------------------------------------------------ admin
    def list_jobs(self) -> list[str]:
        return sorted(e.name for e in self._list(self.folder("jobs"), refresh=True).values() if e.is_folder)

    def delete_remote(self, job_id: str, patterns: Iterable[str]) -> int:
        """Delete large regenerable files from a finished job's Drive folder."""
        parent = self._find(self.folder("jobs"), job_id, True, refresh=True)
        removed = 0
        if not parent:
            return 0

        def walk(folder_id: str, prefix: str) -> None:
            nonlocal removed
            for e in list(self._list(folder_id, refresh=True).values()):
                rel = f"{prefix}{e.name}"
                if e.is_folder:
                    walk(e.id, rel + "/")
                elif any(fnmatch.fnmatch(rel, p) for p in patterns):
                    self.backend.delete(e.id)
                    removed += 1

        walk(parent.id, "")
        self._children_cache.clear()
        return removed
