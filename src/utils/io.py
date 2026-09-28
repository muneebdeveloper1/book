from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(path, data) -> None:
    """Atomically write JSON: temp file in the same dir, fsync, then rename."""
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def write_text(path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _digest(path, algo) -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def sha256(path) -> str:
    return _digest(path, "sha256")


def md5(path) -> str:
    # Drive exposes md5Checksum, so md5 is used purely as a change detector.
    return _digest(path, "md5")


def valid_file(path, min_bytes: int = 1) -> bool:
    p = Path(path)
    return p.is_file() and p.stat().st_size >= min_bytes
