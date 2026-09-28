from __future__ import annotations

import random
import time
from typing import Callable, TypeVar

from src.errors import RateLimitError, classify_exception
from src.utils.log import get_logger

T = TypeVar("T")
log = get_logger("retry")


def retry(
    fn: Callable[[], T],
    attempts: int = 3,
    base_seconds: float = 2.0,
    max_seconds: float = 90.0,
    label: str = "operation",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn`` and retry ONLY errors classified as retryable.

    Unknown exceptions (programming errors) and permanent errors propagate on
    the first occurrence. ``Retry-After`` is honoured for rate limits.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            classified = classify_exception(exc)
            if classified is None or not classified.retryable or attempt == attempts:
                raise
            delay = min(max_seconds, base_seconds * (2 ** (attempt - 1)) + random.random())
            if isinstance(classified, RateLimitError) and classified.retry_after:
                delay = min(max_seconds, max(delay, classified.retry_after))
            log.warning("retrying", label=label, attempt=attempt, of=attempts,
                        delay=round(delay, 1), error=str(classified)[:200])
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
