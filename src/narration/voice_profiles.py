"""Genre-aware Kokoro voice profiles."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceProfile:
    voice: str
    speed: float
    description: str


PROFILES = {
    "audiobook": VoiceProfile("am_adam", 0.94, "Deep, steady American-English narrator"),
    "relaxation": VoiceProfile("am_michael", 0.88, "Warm, calm American-English narrator"),
    "motivation": VoiceProfile("am_adam", 1.02, "Strong, confident American-English narrator"),
    "horror": VoiceProfile("am_fenrir", 0.91, "Deep, dark, controlled American-English narrator"),
    "mystery": VoiceProfile("am_onyx", 0.94, "Rich, sophisticated American-English narrator"),
    "documentary": VoiceProfile("am_eric", 0.98, "Clear, professional American-English narrator"),
    "storytelling": VoiceProfile("bm_fable", 0.94, "Expressive British-English storytelling voice"),
    "hindi": VoiceProfile("hm_omega", 0.94, "Hindi male narrator"),
}


def get_profile(name: str | None = None) -> VoiceProfile:
    key = (name or os.getenv("TTS_PROFILE", "audiobook")).strip().lower()
    if key not in PROFILES:
        raise ValueError(f"Unknown TTS_PROFILE '{key}'. Available: {', '.join(sorted(PROFILES))}")
    return PROFILES[key]


def infer_profile(topic: str, language: str = "English") -> str:
    """Conservative genre routing based on topic keywords.

    The caller can always override this with TTS_PROFILE. This intentionally
    avoids claiming that an LLM classification is authoritative.
    """
    if language.lower().startswith("hindi"):
        return "hindi"
    t = (topic or "").lower()
    rules = {
        "horror": ("horror", "ghost", "haunted", "demon", "scary", "terrifying", "dark story"),
        "relaxation": ("sleep", "relax", "calm", "meditation", "anxiety relief", "peaceful"),
        "motivation": ("motivation", "discipline", "success", "mindset", "confidence", "self improvement"),
        "mystery": ("mystery", "unsolved", "investigation", "detective", "missing", "crime"),
        "documentary": ("history", "documentary", "science", "explained", "biography"),
        "storytelling": ("story", "tale", "fiction", "novel"),
    }
    for profile, keywords in rules.items():
        if any(k in t for k in keywords):
            return profile
    return "audiobook"
