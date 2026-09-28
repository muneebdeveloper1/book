"""YouTube metadata generation, validated through the Metadata model."""
from __future__ import annotations

from src.domain.models import Metadata
from src.errors import InvalidResponseError
from src.utils.log import get_logger

log = get_logger("metadata")


def generate(llm, topic: str, script: str, channel: str = "audiobook", book: str | None = None) -> dict:
    context = f"Book: {book}\n" if book else ""
    disclaimer = ("Do not imply this is a reading of a copyrighted book; it is original summary/analysis."
                  if channel == "audiobook" else
                  "This is educational content, not personalised financial advice. No guaranteed returns, "
                  "no price predictions presented as certainty.")
    raw = llm.json(
        f"""Generate honest YouTube metadata for an original educational video about {topic}.
{context}{disclaimer}
The title must reflect what the script actually covers, with no clickbait claims the script does not support.
Make thumbnail_prompt visual-only: no text, no logos. Make thumbnail_hook EXACTLY 3 to 5 strong, readable words suitable for a YouTube thumbnail.
Use the script for accurate chapter titles and claims.

Return JSON: {{"title":"","description":"","chapters":[{{"time":"0:00","title":""}}],
"keywords":[],"hashtags":[],"thumbnail_hook":"","thumbnail_prompt":""}}

SCRIPT:
{script[:50000]}""",
        max_tokens=6000, temperature=0.35)
    if not isinstance(raw, dict):
        raise InvalidResponseError(f"Metadata generator returned {type(raw).__name__}, expected an object")
    if not str(raw.get("title", "")).strip():
        raw["title"] = topic
    meta = Metadata.model_validate(raw)
    log.info("metadata ready", title_chars=len(meta.title), keywords=len(meta.keywords))
    return meta.model_dump()
