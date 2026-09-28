"""Pooled HTTP session and an SSRF-aware downloader.

``requests.Session`` gives connection pooling. Redirects are followed manually
so EVERY hop (not just the first URL) is checked against private/loopback/
link-local address space, and content sniffing does not trust a generic
``application/octet-stream`` header.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.errors import PermanentMediaError, PipelineError, classify_http_status
from src.utils.log import get_logger

log = get_logger("http")
USER_AGENT = "PersonalVideoPipeline/2.0"
_local = threading.local()


def session() -> requests.Session:
    """One pooled session per thread (Session is not guaranteed thread-safe)."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": USER_AGENT})
        _local.session = s
    return s


def _default_resolver(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, None)})


def assert_public_url(url: str, resolver=_default_resolver) -> str:
    """Raise unless ``url`` is http(s) and resolves only to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PermanentMediaError(f"Unsupported URL: {url[:120]}")
    try:
        addresses = resolver(parsed.hostname)
    except OSError as exc:
        raise PermanentMediaError(f"Cannot resolve {parsed.hostname}: {exc}") from exc
    if not addresses:
        raise PermanentMediaError(f"No addresses for {parsed.hostname}")
    for raw in addresses:
        ip = ipaddress.ip_address(raw.split("%")[0])
        if not ip.is_global:
            raise PermanentMediaError(f"Refusing non-public address {ip} for {parsed.hostname}")
    return url


VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v")


def download(
    url: str,
    out,
    max_bytes: int,
    kind: str = "video",
    timeout: int = 180,
    max_redirects: int = 5,
    resolver=_default_resolver,
    http=None,
) -> dict:
    """Download ``url`` to ``out`` with size limits and redirect validation.

    Returns ``{"final_url", "content_type", "bytes"}``. ``kind`` is ``video``
    or ``image``; ``application/octet-stream`` is accepted only when the FINAL
    URL path has a matching file extension (ffprobe/Pillow still validate the
    bytes afterwards).
    """
    http = http or session()
    current = url
    response = None
    for _ in range(max_redirects + 1):
        assert_public_url(current, resolver)
        response = http.get(current, stream=True, timeout=timeout, allow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise PermanentMediaError("Redirect without Location header")
            current = urljoin(current, location)
            continue
        break
    else:
        raise PermanentMediaError("Too many redirects")

    try:
        if response.status_code >= 400:
            raise classify_http_status(response.status_code, response.text[:200] if response.text else "")
        ctype = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        path_lower = urlparse(current).path.lower()
        image_ext = (".jpg", ".jpeg", ".png", ".webp")
        if kind == "video":
            ok = ctype.startswith("video/") or (
                ctype in ("application/octet-stream", "binary/octet-stream") and path_lower.endswith(VIDEO_EXTENSIONS)
            )
        else:
            ok = ctype.startswith("image/") or (
                ctype in ("application/octet-stream", "binary/octet-stream") and path_lower.endswith(image_ext)
            )
        if not ok:
            raise PermanentMediaError(f"Unexpected content-type {ctype or '(none)'} for {kind} download")
        declared = int(response.headers.get("content-length") or 0)
        if declared > max_bytes:
            raise PermanentMediaError(f"Asset is {declared} bytes; limit is {max_bytes}")
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        total = 0
        try:
            with tmp.open("wb") as handle:
                for chunk in response.iter_content(1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise PermanentMediaError(f"Asset exceeded download limit of {max_bytes} bytes")
                    handle.write(chunk)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {"final_url": current, "content_type": ctype, "bytes": total}
    finally:
        response.close()


def check_status(response: requests.Response, what: str) -> None:
    """Raise a classified error for a non-2xx response."""
    if response.ok:
        return
    retry_after = None
    try:
        retry_after = float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        pass
    err = classify_http_status(response.status_code, response.text or "", retry_after)
    log.warning("http error", what=what, status=response.status_code)
    raise err


def get_public(url: str, timeout: int = 20, max_redirects: int = 3, max_bytes: int = 2_000_000,
               headers: dict | None = None, resolver=_default_resolver, http=None):
    """GET a web page from search results with per-hop public-address checks and a size cap.

    Returns ``(final_url, content_type, text)``.
    """
    http = http or session()
    current = url
    for _ in range(max_redirects + 1):
        assert_public_url(current, resolver)
        resp = http.get(current, timeout=timeout, allow_redirects=False, stream=True, headers=headers or {})
        try:
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                if not location:
                    raise PermanentMediaError("Redirect without Location header")
                current = urljoin(current, location)
                continue
            if resp.status_code >= 400:
                raise classify_http_status(resp.status_code, "")
            chunks, total = [], 0
            for chunk in resp.iter_content(64 * 1024):
                total += len(chunk)
                chunks.append(chunk)
                if total >= max_bytes:
                    break
            encoding = resp.encoding or "utf-8"
            return current, (resp.headers.get("content-type") or "").lower(), b"".join(chunks).decode(encoding, "replace")
        finally:
            resp.close()
    raise PermanentMediaError("Too many redirects")
