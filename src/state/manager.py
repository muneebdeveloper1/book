"""Job state machine: the single authority for stage order and legal moves.

A job walks a fixed list of stages. Each stage has an *active* state (work in
progress) and a *done* state (work committed). ``state.json`` records the
current state plus the set of completed stages, so "is this stage finished?"
never depends on guessing from a state name.

Legal moves
-----------
* ``QUEUED`` -> active state of the first incomplete stage
* active(stage) -> done(stage)              (records the stage as completed)
* done(stage)   -> active(next stage)       (no skipping)
* FAILED_RETRYABLE -> active(first incomplete stage)   (resume, no skipping)
* any non-terminal state -> FAILED_RETRYABLE / FAILED
* done(last stage) -> COMPLETED, only when every stage is completed
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.errors import InvalidTransition
from src.utils.io import read_json, write_json

SCHEMA = 2
HISTORY_LIMIT = 60


@dataclass(frozen=True)
class Stage:
    key: str
    active: str
    done: str


STAGES: tuple[Stage, ...] = (
    Stage("source", "VALIDATING_SOURCE", "SOURCE_VALIDATED"),
    Stage("research", "RESEARCHING", "RESEARCH_READY"),
    Stage("script", "WRITING_SCRIPT", "SCRIPT_READY"),
    Stage("audio", "GENERATING_AUDIO", "AUDIO_READY"),
    Stage("visuals", "GENERATING_VISUALS", "VISUALS_READY"),
    Stage("render", "RENDERING", "RENDER_READY"),
    Stage("qa", "QUALITY_CHECK", "QA_PASSED"),
    Stage("package", "PACKAGING", "PACKAGE_READY"),
    Stage("upload", "UPLOADING", "UPLOADED"),
    Stage("thumbnail", "SETTING_THUMBNAIL", "THUMBNAIL_DONE"),
    Stage("verify", "UPLOAD_VERIFICATION", "VERIFIED"),
    Stage("cleanup", "CLEANUP", "CLEANED"),
)
STAGE_KEYS = tuple(s.key for s in STAGES)
_BY_KEY = {s.key: s for s in STAGES}
_BY_ACTIVE = {s.active: s for s in STAGES}
_BY_DONE = {s.done: s for s in STAGES}

QUEUED = "QUEUED"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
FAILED_RETRYABLE = "FAILED_RETRYABLE"
TERMINAL = (COMPLETED, FAILED)
STATES = (QUEUED, *[x for s in STAGES for x in (s.active, s.done)], COMPLETED, FAILED_RETRYABLE, FAILED)

# Checkpoints written by the previous release used state names as completion
# markers. Map them once so an in-flight job survives the upgrade.
_LEGACY_DONE = {
    "SOURCE_VALIDATED": "source", "RESEARCHING": "research", "SCRIPT_READY": "script",
    "AUDIO_READY": "audio", "VISUALS_READY": "visuals", "QUALITY_CHECK": "render",
    "PACKAGING": "qa", "UPLOAD_VERIFICATION": "upload", "CLEANUP": "verify",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage(key: str) -> Stage:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise InvalidTransition(f"Unknown stage '{key}'") from None


def init(path, job_id: str) -> dict:
    data = {
        "schema": SCHEMA, "job_id": job_id, "state": QUEUED, "completed": [],
        "retry_count": 0, "generation": 0, "updated_at": now(), "history": [],
    }
    write_json(path, data)
    return data


def _migrate(data: dict) -> dict:
    if not data or data.get("schema") == SCHEMA:
        return data
    completed: list[str] = []
    marker = data.get("last_successful_step") or data.get("state")
    if marker in _LEGACY_DONE:
        upto = STAGE_KEYS.index(_LEGACY_DONE[marker])
        completed = list(STAGE_KEYS[: upto + 1])
    if data.get("state") == "COMPLETED":
        completed = list(STAGE_KEYS)
    data = dict(data)
    data.update(schema=SCHEMA, completed=completed, generation=int(data.get("generation", 0)))
    data.setdefault("retry_count", 0)
    if data.get("state") not in STATES:
        data["state"] = FAILED_RETRYABLE
    return data


def get(path) -> dict:
    return _migrate(read_json(path, {}) or {})


load = get


def completed_stages(data: dict) -> list[str]:
    return [k for k in STAGE_KEYS if k in set(data.get("completed", []))]


def first_incomplete(data: dict) -> Stage | None:
    done = set(data.get("completed", []))
    for s in STAGES:
        if s.key not in done:
            return s
    return None


def check_transition(data: dict, target: str) -> None:
    """Raise ``InvalidTransition`` unless ``data['state'] -> target`` is legal."""
    if target not in STATES:
        raise InvalidTransition(f"Unknown state '{target}'")
    current = data.get("state", QUEUED)
    done = set(data.get("completed", []))
    if current in TERMINAL:
        raise InvalidTransition(f"{current} is terminal; cannot move to {target}")
    if target in (FAILED, FAILED_RETRYABLE):
        return
    if target == QUEUED:
        raise InvalidTransition("Cannot return to QUEUED")
    if target == COMPLETED:
        missing = [k for k in STAGE_KEYS if k not in done]
        if missing:
            raise InvalidTransition(f"Cannot complete: stages not done: {', '.join(missing)}")
        return
    if target in _BY_ACTIVE:
        st = _BY_ACTIVE[target]
        expected = first_incomplete(data)
        if expected is None or st.key != expected.key:
            raise InvalidTransition(
                f"Cannot start {st.key}: next stage is {expected.key if expected else 'none (all done)'}"
            )
        return
    if target in _BY_DONE:
        st = _BY_DONE[target]
        if current != st.active:
            raise InvalidTransition(f"Cannot finish {st.key} from state {current}")
        return
    raise InvalidTransition(f"Unhandled target {target}")  # pragma: no cover


def transition(path, target: str, **extra) -> dict:
    data = get(path)
    if not data:
        raise InvalidTransition(f"No state file at {path}")
    check_transition(data, target)
    previous = data.get("state")
    data.update(extra)
    data["state"] = target
    data["updated_at"] = now()
    data["generation"] = int(data.get("generation", 0)) + 1
    if target in _BY_DONE:
        key = _BY_DONE[target].key
        if key not in data["completed"]:
            data["completed"].append(key)
    if target == FAILED_RETRYABLE:
        data["retry_count"] = int(data.get("retry_count", 0)) + 1
    history = data.setdefault("history", [])
    history.append({"from": previous, "to": target, "at": data["updated_at"]})
    del history[:-HISTORY_LIMIT]
    write_json(path, data)
    return data


def mark_skipped(path, key: str) -> dict:
    """Record a stage as complete without doing it (e.g. a channel has no rights stage)."""
    data = get(path)
    st = stage(key)
    if data.get("state") in TERMINAL:
        raise InvalidTransition("Job is terminal")
    expected = first_incomplete(data)
    if expected is None or expected.key != st.key:
        raise InvalidTransition(f"Cannot skip {key}: next stage is {expected.key if expected else 'none'}")
    data["completed"].append(st.key)
    data["updated_at"] = now()
    data["generation"] = int(data.get("generation", 0)) + 1
    write_json(path, data)
    return data


def invalidate_from(path, key: str, reason: str = "") -> dict:
    """Mark ``key`` and every later stage as not done (artifact verification failed)."""
    data = get(path)
    cut = STAGE_KEYS.index(stage(key).key)
    data["completed"] = [k for k in data.get("completed", []) if STAGE_KEYS.index(k) < cut]
    arts = data.get("artifacts", {})
    data["artifacts"] = {k: v for k, v in arts.items() if k in data["completed"]}
    data["updated_at"] = now()
    data["generation"] = int(data.get("generation", 0)) + 1
    data.setdefault("history", []).append({"invalidated": key, "reason": reason[:200], "at": data["updated_at"]})
    del data["history"][:-HISTORY_LIMIT]
    write_json(path, data)
    return data
