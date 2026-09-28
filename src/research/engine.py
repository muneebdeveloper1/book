"""Rights screening and source-grounded research.

The rights check is a conservative *content-policy screen*, never a legal
copyright determination: no authoritative database is consulted.
"""
from __future__ import annotations

import re

from src.domain.models import ResearchPackage, RightsResult
from src.errors import InvalidResponseError, RetryableError
from src.utils.log import get_logger

log = get_logger("research")

# Requests that ask for reproduction are refused deterministically, before any LLM opinion.
REPRODUCTION_PATTERNS = [
    r"\bfull (text|audiobook|book)\b", r"\bentire (book|text|chapter)\b", r"\bverbatim\b",
    r"\bword[- ]for[- ]word\b", r"\bread(ing)? (the )?(whole |entire |full )?(book|novel|chapter)s? aloud\b",
    r"\bcomplete (audiobook|text|reading) of\b", r"\bchapter[- ]by[- ]chapter (reading|narration|recitation)\b",
    r"\b(copy|reproduce|transcribe)\b.*\b(book|chapter|article|transcript)\b",
]


def reproduction_requested(topic: str) -> str | None:
    lowered = (topic or "").lower()
    for pattern in REPRODUCTION_PATTERNS:
        if re.search(pattern, lowered):
            return pattern
    return None


def validate_rights(llm, searcher, topic: str) -> RightsResult:
    hit = reproduction_requested(topic)
    if hit:
        return RightsResult(rights_status="reject", source="policy", verification_method="deterministic pattern check",
                            notes=f"Topic asks for reproduction of existing material (matched {hit!r}).")
    evidence: list[dict] = []
    try:
        evidence = searcher.search(f"{topic} public domain copyright original educational use", limit=5)
    except Exception as exc:  # noqa: BLE001 - search outage downgrades to "unknown", never to "safe"
        log.warning("rights search unavailable", error=str(exc)[:160])
    compact = [{"title": e.get("title", ""), "url": e.get("url", ""), "snippet": e.get("snippet", "")} for e in evidence]
    raw = llm.json(f"""Assess whether we can create an ORIGINAL educational/explanatory audiobook-style script about
this topic without reproducing copyrighted expression.

Topic: {topic}
Search evidence: {compact}

Rules:
1. The output must be original educational/explanatory content.
2. Never reproduce a copyrighted book, audiobook, transcript, article or other expression verbatim.
3. A topic being searchable online does NOT make it safe.
4. General knowledge, history, science, psychology, business, philosophy and biography can normally be treated
   originally; a summary/analysis of a named book is acceptable only as summary/analysis in our own words.
5. If the request asks for a copyrighted work to be reproduced, answer "reject".
6. If evidence is insufficient or you are unsure, answer "unknown". Never guess "safe".

Return ONLY JSON: {{"rights_status":"safe|reject|unknown","source":"search|llm|mixed","source_url":"","verification_method":"","notes":""}}""",
                   max_tokens=2000, temperature=0.1)
    if not isinstance(raw, dict):
        raw = {"rights_status": "unknown", "notes": f"non-object LLM reply: {str(raw)[:200]}"}
    status = str(raw.get("rights_status", "unknown")).strip().lower()
    raw["rights_status"] = status if status in ("safe", "reject", "unknown") else "unknown"
    raw["google_evidence"] = compact
    return RightsResult.model_validate({k: raw.get(k, "") for k in RightsResult.model_fields if k in raw})


def build_research(llm, searcher, topic: str) -> dict:
    queries = [
        f"{topic} overview history facts", f"{topic} key concepts evidence research",
        f"{topic} examples statistics background", f"{topic} important developments timeline",
        f"{topic} expert sources primary sources",
    ]
    try:
        sources = searcher.deep_research(queries, max_sources=10)
    except Exception as exc:  # noqa: BLE001 - reported as a retryable research failure below
        raise RetryableError(f"Web research failed for '{topic}': {exc}") from exc
    compact = []
    for src in sources:
        # deep_research() returns fetched page text under "content" (the old code read "text" and always got "").
        body = (src.get("content") or src.get("snippet") or "").strip()
        if len(body) >= 120:
            compact.append({"title": src.get("title", ""), "url": src.get("url", ""), "text": body[:6000]})
    if len(compact) < 2:
        raise RetryableError(f"Only {len(compact)} usable research source(s) found for '{topic}'; need at least 2")
    prompt = f"""Build source-grounded research for an ORIGINAL educational video about: {topic}

Use ONLY the supplied source extracts as evidence. Do NOT invent sources, URLs, statistics, quotes or expert
opinions, do NOT copy article wording. Attach the source URL to each factual claim whenever possible.

Return ONLY JSON:
{{"central_subject":"","major_concepts":[],"examples":[],"facts":[{{"claim":"","source_url":""}}],
"source_information":[{{"title":"","url":""}}],"chapter_candidates":[],"content_boundaries":[],"research_confidence":""}}

SOURCES:
{compact}"""
    last: Exception | None = None
    for attempt in range(2):
        raw = llm.json(prompt, max_tokens=12000, temperature=0.15)
        try:
            return ResearchPackage.model_validate(raw).model_dump()
        except ValueError as exc:
            last = exc
            log.warning("research package failed validation; retrying", attempt=attempt + 1, error=str(exc)[:200])
    raise InvalidResponseError(f"Research package invalid after retries: {last}")
