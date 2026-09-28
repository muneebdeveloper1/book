"""Validated configuration.

* ``config/channels.yaml`` supplies defaults (unknown keys are rejected).
* Any scalar field can be overridden with ``<SECTION>_<FIELD>`` in the
  environment; values are type-checked by pydantic.
* Environment variables that start with a known prefix but do not match a real
  setting are rejected with a "did you mean" hint, so a typo can no longer
  silently fall back to a default.
"""
from __future__ import annotations

import difflib
import os
import typing
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.errors import ConfigError

ROOT = Path(__file__).resolve().parents[1]
YAML_PATH = ROOT / "config" / "channels.yaml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RuntimeSettings(_Section):
    work_root: str = "work"


class DriveSettings(_Section):
    require_persistence: bool = False
    require_narrow_scope: bool = False


class LLMSettings(_Section):
    gemini_models: list[str] = Field(default_factory=lambda: ["gemini-3.5-flash"])
    groq_models: list[str] = Field(default_factory=lambda: ["openai/gpt-oss-120b"])
    request_timeout: int = Field(120, ge=5, le=900)
    max_retries: int = Field(1, ge=0, le=5)
    retry_base_seconds: float = Field(2.0, ge=0, le=60)


class YouTubeSettings(_Section):
    category_id: str = "27"
    thumbnail_required: bool = False


class RenderSettings(_Section):
    width: int = Field(1920, ge=320)
    height: int = Field(1080, ge=180)
    fps: int = Field(30, ge=10, le=60)
    crf: int = Field(20, ge=10, le=35)
    preset: str = "veryfast"
    chunk_workers: int = Field(2, ge=1, le=8)


class AudiobookSettings(_Section):
    books_root: str = "assets/audiobook/books"
    asset_root: str = "assets/audiobook"
    video_library_folder_name: str = "Audiobook Videos"
    video_library_folder_id: str = ""
    video_extensions: list[str] = Field(default_factory=lambda: [".mp4", ".mov", ".m4v", ".mkv", ".webm"])
    target_minutes: int = Field(30, ge=1, le=240)
    test_minutes: int = Field(3, ge=1, le=30)
    wpm: int = Field(130, ge=60, le=260)
    tts_profile: str = "audiobook"
    background_volume: float = Field(0.08, ge=0, le=1)
    music_volume: float = Field(0.08, ge=0, le=1)
    intro_video: str = "intro.mp4"
    avatar_video: str = "avatar.mp4"
    background_video: str = "background.mp4"
    background_music: str = "background_music.mp3"
    thumbnail_template: str = "thumbnail_template.png"
    thumbnail_backgrounds_dir: str = "thumbnail_backgrounds"
    thumbnail_cover_side: Literal["random", "left", "right"] = "random"
    thumbnail_cover_box_left: tuple[int, int, int, int] = (60, 70, 500, 650)
    thumbnail_cover_box_right: tuple[int, int, int, int] = (780, 70, 1220, 650)
    thumbnail_text_box_left: tuple[int, int, int, int] = (560, 90, 1230, 640)
    thumbnail_text_box_right: tuple[int, int, int, int] = (50, 90, 720, 640)
    thumbnail_hook_words_min: int = Field(3, ge=1, le=8)
    thumbnail_hook_words_max: int = Field(5, ge=1, le=10)
    strict_optional_assets: bool = False
    allow_unknown_rights: bool = False
    max_attempts: int = Field(3, ge=1, le=20)
    stale_hours: float = Field(6, ge=0.1)
    budgets: dict[str, float] = Field(default_factory=dict)

    def asset(self, name: str) -> Path:
        p = Path(getattr(self, name))
        return p if p.is_absolute() else Path(self.asset_root) / p


class Settings(_Section):
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    drive: DriveSettings = Field(default_factory=DriveSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    youtube: YouTubeSettings = Field(default_factory=YouTubeSettings)
    render: RenderSettings = Field(default_factory=RenderSettings)
    audiobook: AudiobookSettings = Field(default_factory=AudiobookSettings)


# Environment names that are not derived from a section field.
SECRET_ENV = {
    "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "TAVILY_API_KEY",
    "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_IMAGE_MODEL", "CLOUDFLARE_IMAGE_STEPS",
    "POLLINATIONS_API_KEY", "POLLINATIONS_BASE_URL", "POLLINATIONS_IMAGE_MODEL",
    "AUDIOBOOK_DRIVE_CREDENTIALS", "AUDIOBOOK_DRIVE_ROOT_FOLDER_ID", "AUDIOBOOK_VIDEO_LIBRARY_FOLDER_ID",
    "YOUTUBE_TOKEN_JSON_BOOKS", "YOUTUBE_VISIBILITY_BOOKS",
    "LOG_LEVEL",
}
# Prefixes whose members must all be recognised (typo protection).
GUARDED_PREFIXES = (
    "AUDIOBOOK_", "LLM_", "YOUTUBE_", "DRIVE_", "RENDER_", "RUNTIME_",
    "GEMINI_", "GROQ_", "TAVILY_", "CLOUDFLARE_", "POLLINATIONS_",
)
# Env aliases that map onto list-valued fields (comma separated).
LIST_ENV = {
    "GEMINI_MODEL": ("llm", "gemini_models", "head"),
    "GEMINI_FALLBACK_MODELS": ("llm", "gemini_models", "tail"),
    "GROQ_MODEL": ("llm", "groq_models", "head"),
    "GROQ_FALLBACK_MODELS": ("llm", "groq_models", "tail"),
}


def _is_scalar(annotation) -> bool:
    origin = typing.get_origin(annotation)
    if origin in (list, dict):
        return False
    if origin is typing.Union or str(origin) == "types.UnionType":
        return all(_is_scalar(a) for a in typing.get_args(annotation) if a is not type(None))
    return True


def env_names() -> set[str]:
    """Every environment variable this program understands."""
    names = set(SECRET_ENV) | set(LIST_ENV)
    for section, field in Settings.model_fields.items():
        model = field.annotation
        for name, info in model.model_fields.items():
            if _is_scalar(info.annotation):
                names.add(f"{section}_{name}".upper())
    return names


def check_environment(env: Mapping[str, str]) -> None:
    known = env_names()
    unknown = sorted(k for k in env if k.startswith(GUARDED_PREFIXES) and k not in known)
    if not unknown:
        return
    hints = []
    for name in unknown:
        close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        hints.append(f"{name}" + (f" (did you mean {close[0]}?)" if close else ""))
    raise ConfigError("Unknown configuration environment variable(s): " + ", ".join(hints))


def load_settings(env: Mapping[str, str] | None = None, yaml_path: Path | None = None) -> Settings:
    env = os.environ if env is None else env
    check_environment(env)
    path = yaml_path or YAML_PATH
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.is_file() else {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping")
    for section, model_field in Settings.model_fields.items():
        data = raw.setdefault(section, {})
        if not isinstance(data, dict):
            raise ConfigError(f"'{section}' in {path.name} must be a mapping")
        for name, info in model_field.annotation.model_fields.items():
            var = f"{section}_{name}".upper()
            if var in env and _is_scalar(info.annotation):
                data[name] = env[var]
    for var, (section, name, where) in LIST_ENV.items():
        value = env.get(var)
        if value is None:
            continue
        items = [x.strip() for x in value.split(",") if x.strip()]
        current = list(raw.setdefault(section, {}).get(name, []))
        raw[section][name] = (items[:1] + current[1:] if where == "head" else current[:1] + items)
        raw[section][name] = list(dict.fromkeys(raw[section][name]))
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            section, _, field = loc.partition(".")
            problems.append(f"{loc}: {err['msg']} (env override: {f'{section}_{field}'.upper()})")
        raise ConfigError("Invalid configuration: " + "; ".join(problems)) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def is_github_actions(env: Mapping[str, str] | None = None) -> bool:
    return bool((os.environ if env is None else env).get("GITHUB_ACTIONS"))
