"""Shared fixtures and fake providers.

The fakes mirror the real interfaces closely enough that the channel
orchestration can be exercised end to end without a network, an API key, a
GPU or a YouTube account.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_settings  # noqa: E402
from src.utils.budget import NullBudget  # noqa: E402


@pytest.fixture
def settings():
    return load_settings({})


@pytest.fixture
def budget():
    return NullBudget()


def make_image(path, size=(1280, 720), colour=(40, 80, 140)):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path)
    return Path(path)


def make_video(path, seconds=6, size="1280x720", pattern="testsrc"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"{pattern}=size={size}:rate=30:duration={seconds}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    return Path(path)


def make_audio(path, seconds=3.0, frequency=300):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={frequency}:duration={seconds}",
                    "-c:a", "libmp3lame", "-b:a", "96k", str(path)], check=True)
    return Path(path)


class FakeLLM:
    """Deterministic stand-in for the Gemini gateway."""

    def __init__(self, rights_status="safe", concepts=60, vision_score=0.9):
        self.rights_status = rights_status
        self.concepts = concepts
        self.vision_score = vision_score
        self.text_calls = 0
        self.json_calls = 0
        self.vision_calls = 0

    def text(self, prompt, system=None, max_tokens=8192, temperature=0.5):
        self.text_calls += 1
        return " ".join(f"This is narration sentence number {i} about the subject at hand." for i in range(1, 26))

    def json(self, prompt, system=None, max_tokens=8192, temperature=0.2, attempts=2):
        self.json_calls += 1
        if "rights_status" in prompt:
            return {"rights_status": self.rights_status, "source": "llm",
                    "verification_method": "test", "notes": "test verdict"}
        if '"mode"' in prompt:
            return {"mode": "original", "reason": "test"}
        if "research" in prompt.lower() and "central_subject" in prompt:
            return {"central_subject": "test subject",
                    "major_concepts": ["one", "two", "three"],
                    "examples": ["example one"],
                    "facts": [{"claim": "a measurable claim", "source_url": "https://example.com/a"}],
                    "source_information": [{"title": "Source A", "url": "https://example.com/a"}],
                    "chapter_candidates": ["Intro"], "content_boundaries": [], "research_confidence": "medium"}
        if "metadata" in prompt.lower():
            return {"title": "A Test Title", "description": "A test description.",
                    "chapters": [{"time": "0:00", "title": "Intro"}],
                    "keywords": ["audiobook", "education"], "hashtags": ["#audiobook"],
                    "thumbnail_hook": "THE BIG IDEA", "thumbnail_prompt": "a desk"}
        return {}

    def vision_json(self, prompt, images, mime="image/jpeg", max_tokens=4000):
        self.vision_calls += 1
        return {"scores": [{"image": i + 1, "score": self.vision_score, "reason": "ok"}
                           for i in range(len(images))]}


class FakeSearcher:
    def search(self, query, limit=5):
        return [{"title": f"Result {i} for {query}", "url": f"https://example.com/{i}",
                 "snippet": "A snippet of text."} for i in range(1, min(limit, 5) + 1)]

    def deep_research(self, queries, max_sources=10):
        return [{"title": f"Source {i}", "url": f"https://example.com/src{i}",
                 "snippet": "short", "content": "Body text. " * 40} for i in range(1, 5)]


class FakeTTS:
    """Writes a real MP3 whose length scales with the sentence, so durations are measurable."""

    def __init__(self, seconds_per_word=0.35):
        self.seconds_per_word = seconds_per_word
        self.calls = 0

    def synthesize(self, text, out_path, voice=None, speed=None):
        self.calls += 1
        seconds = max(0.6, round(len(text.split()) * self.seconds_per_word, 2))
        make_audio(out_path, seconds)
        return str(out_path)


class FakeYouTubeService:
    """Minimal googleapiclient-shaped fake."""

    def __init__(self):
        self.db: dict[str, dict] = {}
        self.inserts = 0
        self.thumbnails_set: list[str] = []
        self.search_broken = False
        self.thumbnail_broken = False

    class _Request:
        def __init__(self, fn):
            self.fn = fn

        def execute(self):
            return self.fn()

    def videos(self):
        outer = self

        class Videos:
            def insert(self, part, body, media_body):
                class Upload:
                    def next_chunk(self, num_retries=0):
                        outer.inserts += 1
                        video_id = f"video{outer.inserts}"
                        outer.db[video_id] = {"snippet": {"description": body["snippet"]["description"],
                                                          "title": body["snippet"]["title"]},
                                              "status": {"privacyStatus": body["status"]["privacyStatus"]}}
                        return None, {"id": video_id}

                return Upload()

            def list(self, part, id):
                return FakeYouTubeService._Request(
                    lambda: {"items": [dict(outer.db[id], id=id)] if id in outer.db else []})

        return Videos()

    def search(self):
        outer = self

        class Search:
            def list(self, **kwargs):
                def run():
                    if outer.search_broken:
                        raise ConnectionError("search unavailable")
                    query = kwargs.get("q", "")
                    return {"items": [{"id": {"videoId": vid}} for vid, data in outer.db.items()
                                      if query in data["snippet"]["description"]]}

                return FakeYouTubeService._Request(run)

        return Search()

    def thumbnails(self):
        outer = self

        class Thumbnails:
            def set(self, videoId, media_body):
                def run():
                    if outer.thumbnail_broken:
                        raise ConnectionError("thumbnail service unavailable")
                    outer.thumbnails_set.append(videoId)
                    return {}

                return FakeYouTubeService._Request(run)

        return Thumbnails()
