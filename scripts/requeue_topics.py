#!/usr/bin/env python3
"""Return parked (failed) audiobook topics to the queue.

Useful after fixing whatever made them fail; without this a topic that hit its
attempt limit stays skipped forever.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.channels import topic_queue  # noqa: E402
from src.config import get_settings  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Requeue parked audiobook topics")
    parser.add_argument("--book", help="only this book directory name")
    parser.add_argument("--topic", help="only this topic")
    args = parser.parse_args(argv)

    books_root = Path(get_settings().audiobook.books_root)
    if not books_root.is_dir():
        print(f"No books directory at {books_root}")
        return 1
    total = 0
    for book in sorted(p for p in books_root.iterdir() if p.is_dir()):
        if args.book and book.name != args.book:
            continue
        revived = topic_queue.requeue(book, args.topic)
        for topic in revived:
            print(f"requeued: {book.name} :: {topic}")
        total += len(revived)
    print(f"{total} topic(s) returned to the queue")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
