"""Final video quality gate."""
from __future__ import annotations

from pathlib import Path

from src.domain.models import QAResult, TimelineSegment, validate_timeline
from src.errors import IntegrityError
from src.rendering.ffmpeg import RenderError, media_info

MIN_BYTES = 100_000


def _fps(stream: dict) -> float:
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        num, den = raw.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def validate_video(path, expected_w: int = 1920, expected_h: int = 1080,
                   expected_duration: float | None = None, tolerance: float | None = None) -> QAResult:
    path = Path(path)
    if not path.is_file() or path.stat().st_size < MIN_BYTES:
        return QAResult(ok=False, errors=["missing_or_too_small"])
    try:
        info = media_info(path)
    except RenderError as exc:
        return QAResult(ok=False, errors=[f"ffprobe:{exc}"])

    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    errors: list[str] = []
    if not video:
        errors.append("no_video")
    if not audio:
        errors.append("no_audio")
    if video and (video.get("width") != expected_w or video.get("height") != expected_h):
        errors.append(f"resolution:{video.get('width')}x{video.get('height')}")
    if video and _fps(video) <= 0:
        errors.append("invalid_frame_rate")

    duration = float(info.get("format", {}).get("duration") or 0)
    if duration < 1:
        errors.append("zero_duration")
    elif expected_duration:
        allowed = tolerance if tolerance is not None else max(2.0, expected_duration * 0.02)
        delta = abs(duration - expected_duration)
        if delta > allowed:
            errors.append(f"duration_mismatch:{delta:.2f}s(allowed {allowed:.2f}s)")
    return QAResult(ok=not errors, errors=errors)


def validate_timeline_file(items: list[dict], narration_duration: float) -> QAResult:
    """Re-check a persisted timeline before rendering or after a resume."""
    try:
        validate_timeline([TimelineSegment.model_validate(x) for x in items], narration_duration)
    except (IntegrityError, ValueError) as exc:
        return QAResult(ok=False, errors=[str(exc)])
    return QAResult(ok=True)
