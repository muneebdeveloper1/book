"""Chapter-level narration for the audiobook channel."""
from __future__ import annotations

from pathlib import Path

from src.errors import InvalidResponseError
from src.rendering.ffmpeg import duration_of
from src.utils.io import read_json, valid_file, write_json
from src.utils.log import get_logger
from src.utils.retry import retry

log = get_logger("narration")
MIN_AUDIO_BYTES = 1000


def generate_all(tts, chapters_dir, audio_dir, attempts: int = 3) -> list[Path]:
    """Synthesise each chapter JSON to MP3, reusing anything already valid."""
    chapters_dir, audio_dir = Path(chapters_dir), Path(audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    produced: list[Path] = []
    chapters = sorted(chapters_dir.glob("*.json"))
    if not chapters:
        raise InvalidResponseError(f"No chapter files found in {chapters_dir}")
    for chapter_file in chapters:
        data = read_json(chapter_file, {}) or {}
        script = (data.get("script") or "").strip()
        if not script:
            raise InvalidResponseError(f"Chapter {chapter_file.name} has no script text")
        audio = audio_dir / f"{chapter_file.stem}.mp3"
        if not valid_file(audio, MIN_AUDIO_BYTES):
            audio.unlink(missing_ok=True)
            retry(lambda s=script, a=audio: tts.synthesize(s, str(a)), attempts=attempts,
                  label=f"tts chapter {chapter_file.stem}")
            if not valid_file(audio, MIN_AUDIO_BYTES):
                raise InvalidResponseError(f"TTS produced no usable audio for {chapter_file.name}")
        write_json(audio_dir / f"{chapter_file.stem}.json",
                   {"file": audio.name, "duration": duration_of(audio), "bytes": audio.stat().st_size})
        produced.append(audio)
    log.info("chapter narration ready", chapters=len(produced))
    return produced
