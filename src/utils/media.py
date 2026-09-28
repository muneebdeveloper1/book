"""FFprobe/FFmpeg helpers: validation, frame sampling and perceptual hashing."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageStat

from src.errors import PermanentMediaError


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def probe(path) -> dict:
    p = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], timeout=60)
    if p.returncode != 0:
        raise PermanentMediaError(f"ffprobe failed: {p.stderr.strip()[:200]}")
    return json.loads(p.stdout)


def duration_of(path) -> float:
    info = probe(path)
    value = info.get("format", {}).get("duration")
    if value in (None, "N/A"):
        streams = [float(s["duration"]) for s in info.get("streams", []) if s.get("duration") not in (None, "N/A")]
        value = max(streams) if streams else 0
    return float(value)


def _fps(stream: dict) -> float:
    raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "0/1"
    try:
        num, den = raw.split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def extract_frame(path, at: float, out, width: int | None = None) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{max(at, 0):.3f}", "-i", str(path), "-frames:v", "1"]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    cmd.append(str(out))
    p = run(cmd, timeout=60)
    if p.returncode != 0 or not Path(out).is_file():
        raise PermanentMediaError(f"cannot extract frame at {at:.2f}s: {p.stderr.strip()[:160]}")
    return Path(out)


def dhash(image_path, size: int = 8) -> str:
    """64-bit difference hash as 16 hex chars (robust to rescale/recompress)."""
    im = Image.open(image_path).convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    px = list(im.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (1 if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1] else 0)
    return f"{bits:0{size * size // 4}x}"


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def video_fingerprint(path, duration: float | None = None) -> str:
    duration = duration if duration is not None else duration_of(path)
    with tempfile.TemporaryDirectory() as tmp:
        frame = extract_frame(path, max(0.0, duration * 0.35), Path(tmp) / "f.jpg", width=320)
        return dhash(frame)


def validate_video_asset(path, need_seconds: float, min_width: int = 1280, min_fps: float = 15,
                         max_fps: float = 120, source_start: float = 0.0) -> dict:
    """Reject clips that are technically 'valid files' but unusable footage.

    Checks: has a video stream, big enough, sane frame rate, long enough for
    the segment, decodes cleanly, and is not (nearly) black or flat.
    Returns ``{"duration", "width", "height", "fps"}``.
    """
    info = probe(path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise PermanentMediaError("no video stream")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width < min_width or height < min_width * 9 // 16 - 8:
        raise PermanentMediaError(f"resolution too low: {width}x{height}")
    fps = _fps(video)
    if not (min_fps <= fps <= max_fps):
        raise PermanentMediaError(f"unusable frame rate: {fps:.2f}")
    duration = duration_of(path)
    if duration + 0.05 < need_seconds + source_start:
        raise PermanentMediaError(f"clip too short: {duration:.2f}s < needed {need_seconds:.2f}s")
    decode = run(["ffmpeg", "-v", "error", "-xerror", "-ss", f"{source_start:.3f}", "-t", f"{min(need_seconds + 0.5, 6):.2f}",
                  "-i", str(path), "-f", "null", "-"], timeout=180)
    if decode.returncode != 0 or decode.stderr.strip():
        raise PermanentMediaError(f"decode errors: {decode.stderr.strip()[:160]}")
    with tempfile.TemporaryDirectory() as tmp:
        lumas, spreads = [], []
        for i, frac in enumerate((0.15, 0.5, 0.85)):
            at = source_start + max(0.0, min(need_seconds * frac, duration - source_start - 0.1))
            frame = extract_frame(path, at, Path(tmp) / f"s{i}.jpg", width=96)
            stat = ImageStat.Stat(Image.open(frame).convert("L"))
            lumas.append(stat.mean[0])
            spreads.append(stat.stddev[0])
    if max(lumas) < 14:
        raise PermanentMediaError("clip is (nearly) black")
    if max(spreads) < 4:
        raise PermanentMediaError("clip is flat/blank")
    return {"duration": duration, "width": width, "height": height, "fps": fps}
