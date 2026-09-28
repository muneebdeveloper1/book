"""Drive-backed reusable audiobook video library.

A production selects one random subfolder from the configured Drive library,
downloads its video clips once into the job, and builds a fresh randomized
sequence for the narration. The sequence is deterministic after checkpointing.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from src.errors import ConfigError, InvalidResponseError
from src.rendering.ffmpeg import VIDEO_EXTS, duration_of
from src.utils.io import read_json, write_json


def _is_video(name: str, extensions: list[str]) -> bool:
    return Path(name).suffix.lower() in {x.lower() for x in extensions or VIDEO_EXTS}


def select_and_download(runtime, job: Path, cfg) -> tuple[Path, list[Path], dict]:
    """Select one Drive subfolder and download all video clips in it."""
    if not runtime.drive:
        raise ConfigError("Audiobook video library requires Drive persistence/credentials")
    manifest_path = job / "visuals" / "video_library.json"
    cached = read_json(manifest_path, None)
    cached_folder_id = cached.get("folder_id") if isinstance(cached, dict) else ""
    cached_library_id = cached.get("library_folder_id") if isinstance(cached, dict) else ""
    if isinstance(cached, dict) and cached_folder_id and cached.get("clips"):
        clips = [job / "videos" / x["local_name"] for x in cached["clips"]]
        if all(p.is_file() for p in clips):
            return job / "videos", clips, cached

    parent_id = (cfg.video_library_folder_id or runtime.env.get("AUDIOBOOK_VIDEO_LIBRARY_FOLDER_ID", "")).strip()
    if parent_id:
        parent = runtime.drive.find_folder(cfg.video_library_folder_name, parent_id) or None
        # A supplied folder ID is itself allowed to be the main library folder.
        library_id = parent_id
        library_name = cfg.video_library_folder_name
    else:
        entry = runtime.drive.find_folder(cfg.video_library_folder_name)
        if not entry:
            raise ConfigError(f"Drive video library folder not found: {cfg.video_library_folder_name!r}")
        library_id, library_name = entry.id, entry.name

    folders = [e for e in runtime.drive.list_folder(library_id) if e.is_folder]
    if not folders:
        raise ConfigError(f"No video subfolders found inside Drive library {library_name!r}")
    if cached_folder_id:
        folder = next((e for e in folders if e.id == cached_folder_id), None)
        if folder is None:
            raise ConfigError(f"The previously selected audiobook video folder no longer exists: {cached_folder_id}")
    else:
        rng = random.SystemRandom()
        folder = rng.choice(folders)
    entries = [e for e in runtime.drive.list_folder(folder.id) if not e.is_folder and _is_video(e.name, cfg.video_extensions)]
    if not entries:
        raise ConfigError(f"Selected video folder {folder.name!r} contains no supported video clips")

    outdir = job / "videos"
    outdir.mkdir(parents=True, exist_ok=True)
    clips, records = [], []
    for i, entry in enumerate(sorted(entries, key=lambda e: (e.name.lower(), e.id))):
        local = outdir / f"{i:04d}_{Path(entry.name).name}"
        if not local.is_file() or (entry.size and local.stat().st_size != entry.size):
            runtime.drive.download_entry(entry, local)
        try:
            dur = duration_of(local)
        except Exception as exc:
            local.unlink(missing_ok=True)
            raise InvalidResponseError(f"Downloaded library clip is not readable: {entry.name}: {exc}") from exc
        if dur <= 0.05:
            raise InvalidResponseError(f"Library clip has invalid duration: {entry.name}")
        clips.append(local)
        records.append({"drive_id": entry.id, "name": entry.name, "local_name": local.name, "duration": dur})

    manifest = {"folder_id": folder.id, "folder_name": folder.name, "library_folder_id": library_id, "clips": records}
    write_json(manifest_path, manifest)
    return outdir, clips, manifest


def random_sequence(clips: list[Path], total_seconds: float, seed=None) -> list[dict]:
    """Create a fresh random clip sequence until total_seconds is covered.

    Each round is a shuffled deck rather than 1,2,3... repetition. A new deck
    is shuffled independently, and the same clip is avoided consecutively when
    more than one clip exists. The final occurrence is trimmed to exact duration.
    """
    if not clips or total_seconds <= 0:
        raise InvalidResponseError("Cannot build video sequence without clips and positive duration")
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    remaining = float(total_seconds)
    sequence: list[dict] = []
    deck: list[Path] = []
    last: Path | None = None
    while remaining > 0.0005:
        if not deck:
            deck = list(clips)
            rng.shuffle(deck)
            if len(deck) > 1 and last is not None and deck[0] == last:
                swap = next((i for i, x in enumerate(deck[1:], 1) if x != last), None)
                if swap is not None:
                    deck[0], deck[swap] = deck[swap], deck[0]
        clip = deck.pop(0)
        clip_duration = duration_of(clip)
        use = min(clip_duration, remaining)
        sequence.append({"asset": clip.name, "media_type": "video", "source_start": 0.0,
                         "duration": use})
        remaining -= use
        last = clip
    return sequence
