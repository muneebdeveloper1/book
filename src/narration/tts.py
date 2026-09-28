"""Backward-compatible TTS entry point, now backed by local Kokoro."""
from .kokoro_tts import KokoroTTS
from .voice_profiles import get_profile


class TTSProvider(KokoroTTS):
    def __init__(self, profile=None):
        p = get_profile(profile)
        super().__init__(voice=p.voice, speed=p.speed)


__all__ = ["TTSProvider", "KokoroTTS"]
