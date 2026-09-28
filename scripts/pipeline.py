#!/usr/bin/env python3
"""Runner for the audiobook production channel."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.channels.audiobook import produce as produce_audiobook  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.errors import PipelineError  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Audiobook video production runner")
    parser.add_argument("--channel", choices=["audiobook"], default="audiobook")
    parser.add_argument("--test", action="store_true", help="short test-length video")
    parser.add_argument("--root", default="work")
    args = parser.parse_args(argv)
    get_settings()  # validate config/environment before any API call
    try:
        print(produce_audiobook(root=args.root, test=args.test))
        return 0
    except PipelineError:
        raise


if __name__ == "__main__":
    raise SystemExit(main())
