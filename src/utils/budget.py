"""Global API/bandwidth budgets.

A bad prompt, a malformed response loop or a stuck retry can burn a free-tier
quota in minutes. Every metered call goes through ``Budget.spend``; exceeding
a limit raises ``BudgetExceeded`` (never retried) and the job resumes later.
Counters persist in the job directory so a resumed job keeps its history.
"""
from __future__ import annotations

import threading
from pathlib import Path

from src.errors import BudgetExceeded
from src.utils.io import read_json, write_json


class Budget:
    def __init__(self, limits: dict[str, float] | None = None, path: Path | None = None):
        self.limits = dict(limits or {})
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.used: dict[str, float] = {}
        if self.path and self.path.is_file():
            data = read_json(self.path, {}) or {}
            self.used = {k: float(v) for k, v in data.get("used", {}).items()}

    def spend(self, key: str, amount: float = 1) -> None:
        with self._lock:
            limit = self.limits.get(key)
            new_total = self.used.get(key, 0) + amount
            if limit is not None and new_total > limit:
                raise BudgetExceeded(f"Budget '{key}' exhausted: {new_total:g} > {limit:g}")
            self.used[key] = new_total
            if self.path:
                write_json(self.path, {"used": self.used, "limits": self.limits})

    def remaining(self, key: str) -> float | None:
        limit = self.limits.get(key)
        return None if limit is None else limit - self.used.get(key, 0)


class NullBudget(Budget):
    """Unlimited budget for tests and ad-hoc use."""

    def spend(self, key: str, amount: float = 1) -> None:  # noqa: D401
        self.used[key] = self.used.get(key, 0) + amount
