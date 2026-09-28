from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.utils import http as safe_http
from src.utils.log import get_logger

log = get_logger("search")


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""


class GoogleBrowserSearch:
    SEARXNG_INSTANCES = [
        "https://priv.au",
        "https://search.lumy.live",
        "https://search.pi.vps.pw",
        "https://search.minus27315.dev",
        "https://search.liuzj.net",
        "https://searx.oloke.xyz",
    ]

    TAVILY_URL = "https://api.tavily.com/search"

    def __init__(self, timeout: int = 20, max_retries: int = 2, user_agent: str | None = None):
        self.timeout = timeout
        self.max_retries = max_retries
        self.tavily_api_key = os.getenv("TAVILY_API_KEY", "").strip()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent
                or (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/139.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/json",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None):
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    return response

                last_error = RuntimeError(f"HTTP {response.status_code}")

                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                break
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(1.5 * (attempt + 1))

        raise last_error or RuntimeError("HTTP request failed")

    @staticmethod
    def _clean_url(url: str) -> str | None:
        if not url:
            return None

        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            return None

        try:
            parsed = urlparse(url)
            if not parsed.hostname:
                return None
            host = parsed.hostname.lower()
            blocked = {
                "google.com",
                "www.google.com",
                "google.co.uk",
                "www.google.co.uk",
                "searx.space",
            }
            if host in blocked:
                return None
            return url
        except Exception:
            return None

    @staticmethod
    def _normalize_results(results: list[SearchResult]) -> list[dict]:
        return [
            {"title": r.title, "url": r.url, "snippet": r.snippet, "source": r.source}
            for r in results
        ]

    def _tavily_search(self, query: str, limit: int) -> list[SearchResult]:
        if not self.tavily_api_key:
            return []

        payload = {
            "api_key": self.tavily_api_key,
            "query": query,
            "search_depth": "advanced",
            "topic": "general",
            "max_results": min(max(limit, 5), 10),
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }

        try:
            response = self.session.post(self.TAVILY_URL, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException:
            return []

        try:
            data = response.json()
        except ValueError:
            return []

        results = []
        for item in data.get("results", []):
            url = self._clean_url(item.get("url", ""))
            if not url:
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title", "")).strip(),
                    url=url,
                    snippet=str(item.get("content", "")).strip()[:3000],
                    source="tavily",
                )
            )
            if len(results) >= limit:
                break
        return results

    def _searxng_search(self, query: str, limit: int) -> list[SearchResult]:
        for instance in self.SEARXNG_INSTANCES:
            try:
                endpoint = instance.rstrip("/") + "/search"
                response = self._get(
                    endpoint,
                    params={"q": query, "format": "json", "language": "en", "safesearch": 1},
                    headers={"Accept": "application/json"},
                )
                data = response.json()
                results = []
                for item in data.get("results", []):
                    url = self._clean_url(item.get("url", ""))
                    if not url:
                        continue
                    results.append(
                        SearchResult(
                            title=str(item.get("title", "")).strip(),
                            url=url,
                            snippet=str(item.get("content", "")).strip()[:3000],
                            source="searxng",
                        )
                    )
                    if len(results) >= limit:
                        break
                if results:
                    return results
            except Exception:
                continue
        return []

    def _duckduckgo_search(self, query: str, limit: int) -> list[SearchResult]:
        try:
            response = self._get("https://html.duckduckgo.com/html/", params={"q": query})
            soup = BeautifulSoup(response.text, "html.parser")
            results = []

            for item in soup.select(".result, .results_links"):
                link = item.select_one("a.result__a[href]")
                if not link:
                    continue

                url = self._clean_url(link.get("href", ""))
                if not url:
                    continue

                title = link.get_text(" ", strip=True)
                snippet_node = item.select_one(".result__snippet")
                snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""

                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet[:3000],
                        source="duckduckgo",
                    )
                )
                if len(results) >= limit:
                    break
            return results
        except Exception:
            return []

    def search(self, query: str, limit: int = 8, *, raise_on_empty: bool = False) -> list[dict]:
        query = str(query).strip()
        if not query:
            return []

        for name, fn in (("tavily", self._tavily_search), ("searxng", self._searxng_search)):
            try:
                results = fn(query, limit)
                if results:
                    return self._normalize_results(results)
            except requests.RequestException as exc:
                log.warning("search provider failed", provider=name, error=str(exc)[:160])

        results = self._duckduckgo_search(query, limit)
        if results:
            return self._normalize_results(results)

        if raise_on_empty:
            raise RuntimeError(
                "Web research failed: Tavily, SearXNG, and DuckDuckGo returned no usable results."
            )

        return []

    def fetch(self, url: str, max_chars: int = 7000) -> str:
        """Fetch readable text. Redirect hops are validated against private address space."""
        try:
            _, content_type, body = safe_http.get_public(url, timeout=self.timeout, headers=dict(self.session.headers))
        except Exception as exc:  # noqa: BLE001 - one bad source must not abort research
            log.warning("source fetch failed", url=url[:120], error=str(exc)[:160])
            return ""
        if not any(v in content_type for v in ("text/html", "text/plain", "application/xhtml")):
            return ""
        soup = BeautifulSoup(body, "html.parser")
        for element in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"]):
            element.decompose()
        main = soup.find("article") or soup.find("main") or soup.body or soup
        return re.sub(r"\s+", " ", main.get_text(" ", strip=True))[:max_chars]

    def deep_research(self, queries: Iterable[str], max_sources: int = 10) -> list[dict]:
        collected = []
        seen_urls = set()

        for query in queries:
            if len(collected) >= max_sources:
                break

            query = str(query).strip()
            if not query:
                continue

            try:
                results = self.search(query, limit=8, raise_on_empty=False)
            except Exception:
                continue

            for result in results:
                url = result.get("url", "").strip()
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)

                content = self.fetch(url, max_chars=7000)
                collected.append(
                    {
                        "query": query,
                        "title": result.get("title", ""),
                        "url": url,
                        "snippet": result.get("snippet", ""),
                        "source": result.get("source", ""),
                        "content": content,
                    }
                )
                if len(collected) >= max_sources:
                    break

        if not collected:
            raise RuntimeError("Deep research failed: no usable web sources found.")

        return collected
