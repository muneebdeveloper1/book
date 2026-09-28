"""Shared runtime wiring for both channels."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from src.config import Settings, get_settings
from src.drive import DriveStore
from src.errors import ConfigError
from src.providers.gemini import Gemini
from src.research.google_search import GoogleBrowserSearch
from src.utils.budget import Budget
from src.utils.io import read_json, write_json
from src.utils.log import bind, get_logger

log = get_logger("channel")


@dataclass
class Runtime:
    settings: Settings
    channel: str
    root: Path
    env: Mapping[str, str]
    budget: Budget
    llm: Gemini
    searcher: GoogleBrowserSearch
    drive: DriveStore | None
    test: bool = False

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"


def build_runtime(channel: str, root: str | Path = "work", test: bool = False,
                  env: Mapping[str, str] | None = None, settings: Settings | None = None,
                  budget_path: Path | None = None) -> Runtime:
    env = os.environ if env is None else env
    settings = settings or get_settings()
    channel_cfg = getattr(settings, channel, None)
    if channel_cfg is None:
        raise ConfigError(f"Unknown channel '{channel}'")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    budget = Budget(dict(getattr(channel_cfg, "budgets", {}) or {}), budget_path)
    llm = Gemini(settings, budget,
                 gemini_key=env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY", ""),
                 groq_key=env.get("GROQ_API_KEY", ""))
    searcher = GoogleBrowserSearch()
    drive = DriveStore.for_channel(channel, settings, env, budget)
    if drive is None and settings.drive.require_persistence:
        raise ConfigError(f"Drive persistence is required for {channel} but is not configured")
    bind(channel=channel)
    return Runtime(settings, channel, root, env, budget, llm, searcher, drive, test)


def attach_budget(runtime: Runtime, job_dir: Path) -> None:
    """Move budget accounting into the job directory so a resume keeps its spend."""
    runtime.budget.path = job_dir / "budget.json"
    existing = read_json(runtime.budget.path, {}) or {}
    for key, value in (existing.get("used") or {}).items():
        runtime.budget.used.setdefault(key, float(value))


def restore_job(runtime: Runtime, job_id: str, job_dir: Path) -> None:
    """Pull a job back from Drive when this runner has never seen it."""
    if not runtime.drive:
        return
    try:
        result = runtime.drive.restore_job(job_id, job_dir)
        if result.restored:
            log.info("resumed job from Drive", job=job_id, commit=result.commit_seq)
    except Exception as exc:  # noqa: BLE001 - a missing snapshot must not stop a fresh run
        log.warning("could not restore job from Drive", job=job_id, error=str(exc)[:200])


def target_words(runtime: Runtime, channel_cfg) -> int:
    minutes = channel_cfg.test_minutes if runtime.test else channel_cfg.target_minutes
    return int(minutes * channel_cfg.wpm)


def write_licenses(job_dir: Path, timeline: list[dict]) -> None:
    """Keep an attribution ledger for every third-party asset used."""
    write_json(job_dir / "asset_licenses.json", [
        {"asset": x.get("asset"), "provider": x.get("provider"), "provider_id": x.get("provider_id"),
         "source_url": x.get("source_url"), "license": x.get("license"), "author": x.get("author"),
         "score": x.get("score"), "semantic_score": x.get("semantic_score"), "fallback": x.get("fallback")}
        for x in timeline])
