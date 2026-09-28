import hashlib
import re

from src.research.google_search import GoogleBrowserSearch


def slug(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:70]


def job_id(topic):
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y%m%d') + '-' + slug(topic) + '-' + hashlib.sha1(topic.encode()).hexdigest()[:8]


def discover(gemini, history, language='English'):
    used = set(history.get('completed_topics', []) + history.get('rejected_topics', []) + history.get('currently_processing', []))
    candidates = gemini.json(
        f"""Generate 12 original educational audiobook topic candidates for {language}.
Prefer evergreen subjects that can be researched and explained originally. Do not suggest copying a book or transcript.
Avoid: {sorted(used)}.
Return JSON array with topic and rationale."""
    )
    if not isinstance(candidates, list):
        candidates = candidates.get('topics', []) if isinstance(candidates, dict) else []

    # Google Search is used to validate that each candidate has discoverable source material.
    search = GoogleBrowserSearch()
    out = []
    for candidate in candidates:
        topic = candidate.get('topic', '').strip()
        if not topic or topic in used:
            continue
        try:
            results = search.search(f'"{topic}"', limit=5)
            candidate['search_evidence_count'] = len(results)
            candidate['demand_signals'] = [r['title'] for r in results[:3]]
        except Exception as exc:
            candidate['search_evidence_count'] = 0
            candidate['demand_signals'] = []
            candidate['search_error'] = str(exc)
        out.append(candidate)
    out.sort(key=lambda x: x.get('search_evidence_count', 0), reverse=True)
    return out
