"""Structured key=value logging with job/channel/stage context.

GitHub Actions logs are the main debugging surface, so every line carries the
context needed to grep one job or one stage. Errors are also emitted as
``::error::`` workflow annotations when running in Actions.
"""
from __future__ import annotations

import contextvars
import logging
import os
import sys
from datetime import datetime, timezone

_context: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})


def bind(**values) -> None:
    """Attach context (channel/job/stage/...) to all following log lines."""
    merged = dict(_context.get())
    merged.update({k: v for k, v in values.items() if v is not None})
    _context.set(merged)


def clear_context() -> None:
    _context.set({})


def _fmt(value) -> str:
    text = str(value).replace("\n", "\\n")
    if not text or any(c in text for c in ' ="'):
        text = '"' + text.replace('"', "'") + '"'
    return text


class Logger:
    def __init__(self, component: str):
        self.component = component
        self._log = logging.getLogger(f"pipeline.{component}")

    def _emit(self, level: int, msg: str, fields: dict) -> None:
        ctx = dict(_context.get())
        ctx.update(fields)
        parts = [
            f"ts={datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            f"level={logging.getLevelName(level)}",
            f"comp={self.component}",
        ]
        parts += [f"{k}={_fmt(v)}" for k, v in ctx.items()]
        parts.append(f"msg={_fmt(msg)}")
        self._log.log(level, " ".join(parts))
        if level >= logging.ERROR and os.getenv("GITHUB_ACTIONS"):
            print(f"::error title={self.component}::{msg}", file=sys.stdout, flush=True)

    def debug(self, msg, **f): self._emit(logging.DEBUG, msg, f)
    def info(self, msg, **f): self._emit(logging.INFO, msg, f)
    def warning(self, msg, **f): self._emit(logging.WARNING, msg, f)
    def error(self, msg, **f): self._emit(logging.ERROR, msg, f)


_configured = False


def get_logger(component: str) -> Logger:
    global _configured
    if not _configured:
        root = logging.getLogger("pipeline")
        if not root.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            root.addHandler(handler)
        root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        root.propagate = False
        _configured = True
    return Logger(component)
