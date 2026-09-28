"""FFmpeg rendering.

Two things the previous renderer got wrong are fixed here:

1. **Zoompan duration.** ``-loop 1 -t N -i image`` feeds N*fps frames into
   ``zoompan``, which then emits ``d`` frames *per input frame* - a 2 second
   segment came out 20 seconds long. Each still is now decoded as a SINGLE
   frame and ``zoompan`` is asked for exactly the frames the segment needs.
2. **One giant filter graph.** A 50-input graph made FFmpeg allocate every
   decoder at once and lost all work if it died. Segments are now rendered to
   individual chunks (resumable, cheap to retry) and joined with a stream copy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from src.errors import PipelineError, RetryableError
from src.utils.log import get_logger

log = get_logger("render")
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v", ".mkv")


class RenderError(PipelineError):
    """FFmpeg refused to produce the output."""


def run(cmd: list[str], label: str, timeout: int = 3600) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise RenderError(f"{label} failed (exit {proc.returncode}): " + " | ".join(tail))


def media_info(path) -> dict:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RenderError(f"ffprobe failed for {path}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def duration_of(path) -> float:
    info = media_info(path)
    value = info.get("format", {}).get("duration")
    if value in (None, "N/A"):
        candidates = [float(s["duration"]) for s in info.get("streams", []) if s.get("duration") not in (None, "N/A")]
        value = max(candidates) if candidates else 0
    return float(value)


def has_audio(path) -> bool:
    try:
        return any(s.get("codec_type") == "audio" for s in media_info(path).get("streams", []))
    except RenderError:
        return False


def _concat_manifest(files: list[Path], manifest: Path) -> None:
    lines = ["file '" + str(Path(f).resolve()).replace("'", "'\\''") + "'" for f in files]
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_audio(files: list[str | Path], out) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = out.with_suffix(out.suffix + ".txt")
    _concat_manifest([Path(f) for f in files], manifest)
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
             "-c:a", "libmp3lame", "-b:a", "192k", str(out)], "audio concat")
    finally:
        manifest.unlink(missing_ok=True)
    return out


def scale_pad(width: int, height: int) -> str:
    return (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def render_still(image, seconds: float, out, width: int, height: int, fps: int, crf: int, preset: str,
                 zoom: bool = True) -> Path:
    """Render one still image as a clip of exactly ``seconds``.

    The image is decoded ONCE (no ``-loop``) and prescaled before ``zoompan``,
    so the output is exactly ``round(seconds * fps)`` frames with a smooth
    Ken Burns move instead of the jittery crop zoompan does at native size.
    """
    frames = max(1, round(seconds * fps))
    if zoom:
        # Prescale x2 so the zoom crop stays sharp; zoompan emits `frames` frames from one input frame.
        vf = (f"{scale_pad(width, height)},scale={width * 2}:{height * 2},"
              f"zoompan=z='min(zoom+{1.0 / max(frames, 1) * 0.08:.6f},1.08)':d={frames}:"
              f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},format=yuv420p")
    else:
        vf = f"{scale_pad(width, height)},fps={fps},format=yuv420p"
        return _encode(["-loop", "1", "-framerate", str(fps), "-i", str(image)], vf, frames, out, fps, crf, preset)
    return _encode(["-i", str(image)], vf, frames, out, fps, crf, preset)


def render_clip(video, seconds: float, out, width: int, height: int, fps: int, crf: int, preset: str,
                source_start: float = 0.0) -> Path:
    """Render one video segment of exactly ``seconds`` from ``source_start``."""
    frames = max(1, round(seconds * fps))
    vf = f"{scale_pad(width, height)},fps={fps},format=yuv420p"
    pre = ["-stream_loop", "-1", "-ss", f"{max(source_start, 0):.3f}", "-i", str(video)]
    return _encode(pre, vf, frames, out, fps, crf, preset)


def _encode(inputs: list[str], vf: str, frames: int, out, fps: int, crf: int, preset: str) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part.mp4")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-vf", vf, "-frames:v", str(frames),
         "-an", "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-r", str(fps),
         "-pix_fmt", "yuv420p", str(tmp)], f"segment render -> {out.name}")
    tmp.replace(out)
    return out


def render_segments(timeline: list[dict], assets_dir, chunk_dir, width: int, height: int, fps: int,
                    crf: int, preset: str, workers: int = 1) -> list[Path]:
    """Render every timeline segment to its own chunk, skipping chunks already done."""
    assets_dir, chunk_dir = Path(assets_dir), Path(chunk_dir)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for i, item in enumerate(timeline):
        chunk = chunk_dir / f"seg_{i:05d}.mp4"
        seconds = float(item["end"]) - float(item["start"])
        jobs.append((i, item, chunk, seconds))

    def one(job):
        i, item, chunk, seconds = job
        expected = max(1, round(seconds * fps))
        if chunk.is_file():
            try:
                if abs(duration_of(chunk) - expected / fps) <= 1.5 / fps:
                    return chunk
            except RenderError:
                pass
            chunk.unlink(missing_ok=True)
        asset = assets_dir / item["asset"]
        if not asset.is_file():
            raise RenderError(f"segment {i} asset missing: {asset}")
        is_video = item.get("media_type") == "video" or asset.suffix.lower() in VIDEO_EXTS
        if is_video:
            render_clip(asset, seconds, chunk, width, height, fps, crf, preset, float(item.get("source_start", 0)))
        else:
            render_still(asset, seconds, chunk, width, height, fps, crf, preset)
        return chunk

    if workers > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            chunks = list(pool.map(one, jobs))
    else:
        chunks = [one(job) for job in jobs]
    log.info("segments rendered", count=len(chunks))
    return chunks


def concat_video(chunks: list[Path], out) -> Path:
    """Join chunks without re-encoding (identical codec parameters by construction)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    manifest = out.with_suffix(".concat.txt")
    _concat_manifest(chunks, manifest)
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(manifest),
             "-c", "copy", "-movflags", "+faststart", str(out)], "video concat")
    finally:
        manifest.unlink(missing_ok=True)
    return out


def mux(video, audio, out, music: str | None = None, music_volume: float = 0.06, crf: int = 20,
        preset: str = "veryfast") -> Path:
    """Attach narration (plus optional ducked music bed) to a silent video."""
    out = Path(out)
    inputs = ["-i", str(video), "-i", str(audio)]
    if music and Path(music).is_file():
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        # asplit: the narration feeds BOTH the mix and the sidechain key input,
        # and an FFmpeg filter output label may only be consumed once.
        graph = ("[1:a]aresample=48000,asplit=2[narr][key];"
                 f"[2:a]aresample=48000,volume={float(music_volume):.3f}[bed];"
                 "[bed][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300:makeup=1[duck];"
                 "[narr][duck]amix=inputs=2:duration=first:dropout_transition=2[aout]")
        maps = ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]"]
    else:
        maps = ["-map", "0:v", "-map", "1:a"]
    run(["ffmpeg", "-y", "-v", "error", *inputs, *maps, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-movflags", "+faststart", str(out)], "mux")
    return out


def render_timeline(audio, assets_dir, timeline: list[dict], out, width: int = 1920, height: int = 1080,
                    fps: int = 30, crf: int = 20, preset: str = "veryfast", music: str | None = None,
                    music_volume: float = 0.06, chunk_dir=None, workers: int = 1, keep_chunks: bool = False) -> Path:
    """Render a validated timeline to a finished video."""
    if not timeline:
        raise RenderError("Timeline is empty")
    out = Path(out)
    chunk_dir = Path(chunk_dir) if chunk_dir else out.parent / "render" / "chunks"
    chunks = render_segments(timeline, assets_dir, chunk_dir, width, height, fps, crf, preset, workers)
    silent = out.parent / "render" / "silent.mp4"
    concat_video(chunks, silent)
    mux(silent, audio, out, music, music_volume, crf, preset)
    if not keep_chunks:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        silent.unlink(missing_ok=True)
    return out
