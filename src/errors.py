"""Error taxonomy.

Every failure that crosses a module boundary is classified so callers can make
a deliberate decision instead of catching ``Exception``:

* retryable           -> retry with backoff (network blips, 5xx, rate limits)
* permanent           -> stop; retrying cannot help (bad credentials, bad config)
* candidate-specific  -> reject this asset/answer and try the next one
"""
from __future__ import annotations

import socket
import ssl


class PipelineError(Exception):
    """Base class. ``retryable`` says whether an identical retry may succeed."""

    retryable = False


class RetryableError(PipelineError):
    retryable = True


class RateLimitError(RetryableError):
    def __init__(self, message: str = "rate limited", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(PipelineError):
    """Credentials are missing, expired or lack permission. Never retried."""


class ConfigError(PipelineError):
    """The repository/environment is misconfigured. Never retried."""


class InvalidResponseError(PipelineError):
    """A provider answered, but the answer was unusable (bad JSON, wrong shape)."""


class PermanentMediaError(PipelineError):
    """A downloaded/selected media asset is unusable; try another candidate."""


class BudgetExceeded(PipelineError):
    """A configured API/bandwidth budget was exhausted. Stops the run."""


class RightsBlocked(PipelineError):
    """The rights gate refused this topic."""


class InvalidTransition(PipelineError):
    """The state machine refused an illegal state change."""


class IntegrityError(PipelineError):
    """Persisted data failed validation (corrupt/partial checkpoint)."""


class UploadAmbiguous(RetryableError):
    """A YouTube upload may or may not have happened; refuse to guess."""


def classify_http_status(status: int, body: str = "", retry_after: float | None = None) -> PipelineError:
    text = f"HTTP {status}: {body[:300]}"
    if status == 429:
        return RateLimitError(text, retry_after)
    if status in (401, 403):
        # Quota exhaustion is reported as 403 by Google APIs; treat it as a
        # rate-limit so the job resumes later instead of being declared broken.
        lowered = body.lower()
        if "quota" in lowered or "ratelimit" in lowered or "rate limit" in lowered:
            return RateLimitError(text, retry_after)
        return AuthError(text)
    if status in (408, 409, 425) or 500 <= status <= 599:
        return RetryableError(text)
    return PipelineError(text)


def classify_exception(exc: BaseException) -> PipelineError | None:
    """Return a classified error for well-known exception types, else ``None``.

    ``None`` means "this is a programming error or something unexpected": the
    caller must not retry it and must let it propagate unchanged.
    """
    if isinstance(exc, PipelineError):
        return exc
    try:  # requests is a hard dependency, but keep this module import-light
        import requests

        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            resp = exc.response
            retry_after = None
            try:
                retry_after = float(resp.headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            return classify_http_status(resp.status_code, resp.text or "", retry_after)
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return RetryableError(str(exc))
    except ImportError:  # pragma: no cover
        pass
    try:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError):
            status = getattr(exc.resp, "status", 0) or 0
            body = ""
            try:
                body = exc.content.decode("utf-8", "replace") if exc.content else ""
            except Exception:  # pragma: no cover - defensive decode only
                body = str(exc)
            retry_after = None
            try:
                retry_after = float(exc.resp.get("retry-after", ""))
            except (TypeError, ValueError, AttributeError):
                pass
            return classify_http_status(int(status), body, retry_after)
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (ssl.SSLError, socket.timeout, TimeoutError, ConnectionError)):
        return RetryableError(str(exc))
    return None
