"""Local, API-free Kokoro TTS backend.

Kokoro runs inside the GitHub Actions runner. The model/voice assets are cached
by the runner when possible and are never committed to the repository.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import soundfile as sf

from src.errors import ConfigError, InvalidResponseError


class KokoroTTS:
    """Chapter-oriented Kokoro TTS provider.

    The provider keeps one KPipeline per language so a long audiobook does not
    repeatedly load the model. Output is MP3 to preserve the existing pipeline.
    """

    def __init__(self, voice: str = "am_adam", speed: float = 0.94):
        self.voice = voice
        self.speed = float(speed)
        self.sample_rate = 24000
        self._pipelines: dict = {}
        self._audio_format = os.getenv("TTS_AUDIO_FORMAT", "mp3").lower()
        if self._audio_format not in {"mp3", "wav"}:
            raise ValueError("TTS_AUDIO_FORMAT must be mp3 or wav")

    @staticmethod
    def _lang_from_voice(voice: str) -> str:
        # Kokoro voice IDs begin with a language code: a,b,e,f,h,i,j,p,z.
        return voice.split("_", 1)[0][0].lower()

    def _pipeline(self, lang: str):
        # Imported lazily: the model is only needed when audio is actually
        # generated, so tests and dry runs do not require the kokoro package.
        if lang not in self._pipelines:
            try:
                from kokoro import KPipeline
            except ImportError as exc:
                raise ConfigError("The 'kokoro' package is required for narration. "
                                  "Install it with: pip install kokoro soundfile") from exc
            self._pipelines[lang] = KPipeline(lang_code=lang)
        return self._pipelines[lang]

    def synthesize(self, text: str, out_path: str, voice: Optional[str] = None, speed: Optional[float] = None):
        text = (text or "").strip()
        if not text:
            raise ValueError("Cannot synthesize empty text")

        selected_voice = voice or self.voice
        selected_speed = float(self.speed if speed is None else speed)
        lang = self._lang_from_voice(selected_voice)
        pipeline = self._pipeline(lang)

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="kokoro-") as tmp:
            chunks: list[Path] = []
            for idx, (_graphemes, _phonemes, audio) in enumerate(
                pipeline(text, voice=selected_voice, speed=selected_speed)
            ):
                chunk = Path(tmp) / f"chunk-{idx:05d}.wav"
                sf.write(chunk, audio, self.sample_rate, subtype="PCM_16")
                chunks.append(chunk)

            if not chunks:
                raise InvalidResponseError("Kokoro produced no audio chunks")

            wav_path = Path(tmp) / "combined.wav"
            with sf.SoundFile(chunks[0], "r") as first:
                channels = first.channels
                samplerate = first.samplerate
                subtype = first.subtype

            with sf.SoundFile(wav_path, mode="w", samplerate=samplerate, channels=channels, subtype=subtype) as dst:
                for chunk in chunks:
                    with sf.SoundFile(chunk, "r") as src:
                        while True:
                            data = src.read(8192, dtype="int16")
                            if len(data) == 0:
                                break
                            dst.write(data)

            if self._audio_format == "wav":
                wav_path.replace(out)
            else:
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_path),
                        "-codec:a", "libmp3lame", "-b:a", os.getenv("TTS_MP3_BITRATE", "192k"),
                        str(out),
                    ],
                    check=True,
                )

        if not out.exists() or out.stat().st_size < 1000:
            raise InvalidResponseError(f"Kokoro generated an invalid audio file: {out}")
        return str(out)
