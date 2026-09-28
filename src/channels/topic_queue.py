"""Per-book topic queue with revisions, merge-sync and stale recovery.

Each topic entry carries a ``rev`` counter that increments on every change, so
local and Drive copies can be merged deterministically (``merge_queue``) and a
stale copy can never overwrite a newer one.

Lifecycle: pending -> in_progress -> completed | failed
* a failure counts as an attempt; when ``max_attempts`` is reached the topic
  is parked as ``failed`` (skipped) instead of being retried forever;
* an ``in_progress`` claim older than ``stale_seconds`` with no recorded error
  is a crashed run: it is recycled and counted as an attempt;
* ``requeue()`` puts failed topics back into circulation.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from src.utils.io import read_json, write_json
from src.utils.locks import file_lock

STATE_FILE = "queue_state.json"
COMPLETED_FILE = "completed_topics.json"


def _lock_path(book: Path) -> Path:
    return book / ".topic_queue.lock"


def topics(book: Path) -> list[str]:
    p = Path(book) / "topics.txt"
    if not p.is_file():
        return []
    seen, out = set(), []
    for line in p.read_text(encoding="utf-8").splitlines():
        t = line.strip()
        if t and not t.startswith("#") and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def normalize(raw) -> dict:
    data = dict(raw) if isinstance(raw, dict) else {}
    items = {}
    for topic, entry in (data.get("topics") or {}).items():
        e = dict(entry)
        e.setdefault("status", "pending")
        e.setdefault("attempts", 0)
        e.setdefault("rev", 0)
        e.setdefault("updated_at", e.get("last_failed_at") or e.get("claimed_at") or e.get("completed_at") or 0)
        items[topic] = e
    return {"version": 3, "topics": items}


def merge_queue(local: dict | None, remote: dict | None) -> dict:
    a, b = normalize(local), normalize(remote)
    merged = {}
    for topic in set(a["topics"]) | set(b["topics"]):
        x, y = a["topics"].get(topic), b["topics"].get(topic)
        if x is None or y is None:
            merged[topic] = dict(x or y)
            continue
        if x["status"] == "completed" or y["status"] == "completed":
            winner = min((e for e in (x, y) if e["status"] == "completed"), key=lambda e: e.get("completed_at", 0) or 1e18)
        else:
            winner = max((x, y), key=lambda e: (e["rev"], e["updated_at"]))
        entry = dict(winner)
        entry["attempts"] = max(x["attempts"], y["attempts"])
        entry["rev"] = max(x["rev"], y["rev"])
        merged[topic] = entry
    return {"version": 3, "topics": merged}


def _load(book: Path) -> dict:
    return normalize(read_json(Path(book) / STATE_FILE, None))


def _save(book: Path, data: dict) -> None:
    write_json(Path(book) / STATE_FILE, data)
    done = sorted(k for k, v in data["topics"].items() if v["status"] == "completed")
    write_json(Path(book) / COMPLETED_FILE, done)  # human-readable summary


def _touch(entry: dict, now: float) -> None:
    entry["rev"] = int(entry.get("rev", 0)) + 1
    entry["updated_at"] = now


def job_id(book: Path, topic: str) -> str:
    digest = hashlib.sha1(f"{Path(book).name}\0{topic}".encode("utf-8")).hexdigest()[:10]
    safe = "".join(c.lower() if c.isalnum() else "-" for c in f"{Path(book).name}-{topic}").strip("-")[:62]
    return f"audio-{safe}-{digest}"


def claim_book_topic(book, max_attempts: int = 3, stale_seconds: float = 6 * 3600, now: float | None = None):
    """Return the topic to work on (resuming an unfinished one first), or ``None``."""
    book = Path(book)
    now = time.time() if now is None else now
    with file_lock(_lock_path(book)):
        data = _load(book)
        chosen = None
        for topic in topics(book):
            entry = data["topics"].get(topic)
            if entry is None:
                continue
            if entry["status"] == "in_progress":
                if now - entry["updated_at"] > stale_seconds:
                    # No progress for the whole stale window: the runner that
                    # claimed this died. Count the attempt and re-claim cleanly,
                    # so the topic cannot be retried forever.
                    entry["attempts"] += 1
                    entry["recycled"] = int(entry.get("recycled", 0)) + 1
                    entry["last_error"] = "recycled stale claim (runner crashed or was cancelled)"
                    if entry["attempts"] >= max_attempts:
                        entry["status"] = "failed"
                        _touch(entry, now)
                        continue
                    entry.update(claimed_at=now, last_error=None)
                    _touch(entry, now)
                elif entry["attempts"] >= max_attempts:
                    entry["status"] = "failed"
                    _touch(entry, now)
                    continue
                chosen = topic
                break
        if chosen is None:
            for topic in topics(book):
                entry = data["topics"].get(topic)
                if entry is None or entry["status"] == "pending":
                    entry = data["topics"].setdefault(topic, {"status": "pending", "attempts": 0, "rev": 0, "updated_at": now})
                    entry.update(status="in_progress", job_id=job_id(book, topic), claimed_at=now, last_error=None)
                    _touch(entry, now)
                    chosen = topic
                    break
        _save(book, data)
        return chosen


def pick_book(root):
    root = Path(root)
    if not root.exists():
        return None
    for book in sorted(p for p in root.iterdir() if p.is_dir()):
        if not topics(book):
            continue
        data = _load(book)
        if any(data["topics"].get(t, {}).get("status", "pending") in ("pending", "in_progress") for t in topics(book)):
            return book
    return None


def mark_done(book, topic: str, now: float | None = None) -> None:
    book = Path(book)
    now = time.time() if now is None else now
    with file_lock(_lock_path(book)):
        data = _load(book)
        entry = data["topics"].setdefault(topic, {"attempts": 0, "rev": 0})
        entry.update(status="completed", completed_at=now, last_error=None)
        _touch(entry, now)
        _save(book, data)


def mark_failed(book, topic: str, error, max_attempts: int = 3, now: float | None = None) -> str:
    """Record a failed attempt. Returns the resulting status (in_progress or failed)."""
    book = Path(book)
    now = time.time() if now is None else now
    with file_lock(_lock_path(book)):
        data = _load(book)
        entry = data["topics"].setdefault(topic, {"attempts": 0, "rev": 0, "status": "in_progress"})
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_error"] = str(error)[:2000]
        entry["last_failed_at"] = now
        entry["status"] = "failed" if entry["attempts"] >= max_attempts else "in_progress"
        _touch(entry, now)
        _save(book, data)
        return entry["status"]


def requeue(book, topic: str | None = None, now: float | None = None) -> list[str]:
    """Return failed topics (all, or just ``topic``) to the queue with a fresh attempt budget."""
    book = Path(book)
    now = time.time() if now is None else now
    revived = []
    with file_lock(_lock_path(book)):
        data = _load(book)
        for name, entry in data["topics"].items():
            if entry["status"] == "failed" and topic in (None, name):
                entry.update(status="pending", attempts=0, last_error=None)
                _touch(entry, now)
                revived.append(name)
        _save(book, data)
    return revived


def sync_queue(book, drive, channel: str = "audiobook") -> dict:
    """Merge local queue state with Drive and update both sides."""
    book = Path(book)
    with file_lock(_lock_path(book)):
        merged = drive.sync_json(("queue", channel, f"{book.name}.json"), book / STATE_FILE, merge_queue)
        _save(book, normalize(merged))
    return merged
