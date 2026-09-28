"""Provider compatibility wrapper. No external TTS API is used."""
from src.narration.kokoro_tts import KokoroTTS
from src.narration.voice_profiles import get_profile


class KokoroTTSProvider(KokoroTTS):
    def __init__(self, profile=None):
        p = get_profile(profile)
        super().__init__(voice=p.voice, speed=p.speed)
