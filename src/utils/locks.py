"""Cross-platform advisory file lock with stale-lock stealing.

Uses O_CREAT|O_EXCL (works on Windows and POSIX). A lock older than
``stale_seconds`` is assumed to belong to a crashed process and is removed, so
a killed run can never wedge the queue permanently.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from src.errors import RetryableError


@contextmanager
def file_lock(path, timeout: float = 60, stale_seconds: float = 900):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{os.getpid()} {time.time()}".encode())
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_seconds:
                    path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.time() >= deadline:
                raise RetryableError(f"Could not acquire lock {path} within {timeout}s")
            time.sleep(0.2)
    try:
        yield
    finally:
        os.close(fd)
        try:
            path.unlink()
        except OSError:
            pass
