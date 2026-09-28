"""Typed domain models. Every JSON artifact that crosses a stage boundary is
validated through one of these on write AND on resume, so a corrupt or partial
checkpoint fails loudly instead of reaching FFmpeg or YouTube."""
from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.errors import IntegrityError


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Job(_Model):
    job_id: str
    channel: Literal["audiobook"]
    topic: str = Field(min_length=1)
    state: str = "QUEUED"
    book: str | None = None
    mode: Literal["summary", "original"] | None = None


class RightsResult(_Model):
    rights_status: Literal["safe", "reject", "unknown"]
    source: str = ""
    source_url: str = ""
    verification_method: str = ""
    notes: str = ""
    google_evidence: list[dict] = Field(default_factory=list)
    # The gate is a conservative content-policy screen, never legal clearance.
    disclaimer: str = "Automated content-policy screen; not legal copyright clearance."


class ResearchPackage(_Model):
    central_subject: str = ""
    major_concepts: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    facts: list[dict | str] = Field(default_factory=list)
    source_information: list[dict | str] = Field(default_factory=list)
    chapter_candidates: list[str] = Field(default_factory=list)
    content_boundaries: list[str] = Field(default_factory=list)
    research_confidence: str = ""

    @field_validator("major_concepts", "examples", "chapter_candidates", "content_boundaries", mode="before")
    @classmethod
    def _stringify(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [x if isinstance(x, str) else str(x) for x in value]
        raise ValueError("expected a list")

    @field_validator("research_confidence", "central_subject", mode="before")
    @classmethod
    def _text(cls, value):
        return "" if value is None else str(value)

    @model_validator(mode="after")
    def _has_substance(self):
        if not (self.major_concepts or self.facts or self.examples):
            raise ValueError("research package contains no concepts, facts or examples")
        return self


class Production(_Model):
    channel: Literal["audiobook"]
    job_id: str
    topic: str = Field(min_length=1)
    book: str | None = None
    mode: Literal["summary", "original"] | None = None
    rights: RightsResult | None = None
    coverage_signals: dict = Field(default_factory=dict)


class SentenceRecord(_Model):
    index: int = Field(ge=1)
    sentence: str = Field(min_length=1)
    file: str
    duration: float = Field(gt=0)

    @field_validator("duration")
    @classmethod
    def _finite(cls, v):
        if not math.isfinite(v):
            raise ValueError("duration must be finite")
        return v


class TimelineSegment(_Model):
    asset: str
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    sentence: str = ""
    query: str = ""
    media_type: Literal["video", "photo"] = "video"
    source_start: float = Field(default=0, ge=0)
    score: float = 0
    provider: str = ""
    source_url: str = ""
    download_url: str = ""
    provider_id: str = ""
    license: str = ""
    author: str = ""
    sha256: str = ""
    fingerprint: str = ""
    fallback: bool = False
    semantic_score: float | None = None

    @field_validator("start", "end")
    @classmethod
    def _finite(cls, v):
        if not math.isfinite(v):
            raise ValueError("timestamps must be finite")
        return v

    @model_validator(mode="after")
    def _ordered(self):
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


def validate_timeline(segments: list[TimelineSegment], narration_duration: float,
                      tolerance: float = 0.05) -> None:
    """Segments must start at 0, be contiguous (no gap/overlap) and end at the
    narration duration. Raises ``IntegrityError`` otherwise."""
    if not segments:
        raise IntegrityError("timeline is empty")
    if abs(segments[0].start) > tolerance:
        raise IntegrityError(f"timeline starts at {segments[0].start:.3f}s, expected 0")
    for prev, cur in zip(segments, segments[1:]):
        gap = cur.start - prev.end
        if abs(gap) > tolerance:
            kind = "gap" if gap > 0 else "overlap"
            raise IntegrityError(f"timeline {kind} of {abs(gap):.3f}s between {prev.asset} and {cur.asset}")
    end_tolerance = max(tolerance, 0.1)
    if abs(segments[-1].end - narration_duration) > end_tolerance:
        raise IntegrityError(
            f"timeline ends at {segments[-1].end:.3f}s but narration is {narration_duration:.3f}s"
        )


def load_timeline(items: list[dict], narration_duration: float | None = None) -> list[TimelineSegment]:
    try:
        segments = [TimelineSegment.model_validate(x) for x in items]
    except ValueError as exc:
        raise IntegrityError(f"visual timeline failed validation: {exc}") from exc
    if narration_duration is not None:
        validate_timeline(segments, narration_duration)
    return segments


class Metadata(_Model):
    title: str = Field(min_length=1)
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)
    chapters: list[dict | str] = Field(default_factory=list)
    thumbnail_hook: str = ""
    thumbnail_prompt: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v):
        text = re.sub(r"[<>]", "", str(v or "")).strip()
        return text[:100].rstrip()

    @field_validator("description", mode="before")
    @classmethod
    def _description(cls, v):
        text = re.sub(r"[<>]", "", str(v or ""))
        return text.encode("utf-8")[:4800].decode("utf-8", "ignore")

    @field_validator("keywords", "hashtags", mode="before")
    @classmethod
    def _tags(cls, v):
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        out, total = [], 0
        for tag in v or []:
            tag = re.sub(r"[<>\"]", "", str(tag)).strip()
            if not tag:
                continue
            total += len(tag) + 1
            if total > 480:  # YouTube limit is 500 characters in total
                break
            out.append(tag)
        return out

    @field_validator("chapters", mode="before")
    @classmethod
    def _chapters(cls, v):
        return v if isinstance(v, list) else []


class YouTubeState(_Model):
    job_id: str
    video_id: str
    credential_env: str = ""
    uploaded_at: str = ""
    thumbnail: Literal["pending", "set", "skipped", "failed"] = "pending"
    thumbnail_error: str = ""
    verified: bool = False
    adopted_existing: bool = False


class QAResult(_Model):
    ok: bool
    errors: list[str] = Field(default_factory=list)
