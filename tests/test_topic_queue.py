"""Audiobook queue: claims, attempts, stale recovery and lost-update safety."""
import json
import shutil

import pytest

from src.channels import topic_queue as q
from src.drive import DriveStore, MemoryBackend


@pytest.fixture
def book(tmp_path):
    root = tmp_path / "books"
    book_dir = root / "My Book"
    book_dir.mkdir(parents=True)
    (book_dir / "topics.txt").write_text("One\nTwo\n# a comment\nThree\n")
    return book_dir


def test_pick_book_uses_the_documented_layout(book):
    assert q.pick_book(book.parent) == book
    assert q.topics(book) == ["One", "Two", "Three"]


def test_claim_is_resumed_not_duplicated(book):
    assert q.claim_book_topic(book, now=1000) == "One"
    assert q.claim_book_topic(book, now=1100) == "One"


def test_topic_is_parked_after_max_attempts(book):
    q.claim_book_topic(book, max_attempts=2, now=1000)
    assert q.mark_failed(book, "One", "boom", max_attempts=2, now=1100) == "in_progress"
    assert q.mark_failed(book, "One", "boom", max_attempts=2, now=1200) == "failed"
    assert q.claim_book_topic(book, max_attempts=2, now=1300) == "Two"


def test_crashed_claim_is_recycled_after_the_stale_window(book):
    q.claim_book_topic(book, max_attempts=2, stale_seconds=3600, now=0)
    assert q.claim_book_topic(book, max_attempts=2, stale_seconds=3600, now=10_000) == "One"
    assert q._load(book)["topics"]["One"]["attempts"] == 1
    # A second crash exhausts the budget and the topic is parked, not retried forever.
    assert q.claim_book_topic(book, max_attempts=2, stale_seconds=3600, now=20_000) == "Two"
    assert q._load(book)["topics"]["One"]["status"] == "failed"


def test_requeue_returns_failed_topics(book):
    q.claim_book_topic(book, max_attempts=1, now=0)
    q.mark_failed(book, "One", "boom", max_attempts=1, now=10)
    assert q.requeue(book, now=20) == ["One"]
    assert q.claim_book_topic(book, now=30) == "One"


def test_completed_topics_are_not_reclaimed(book):
    q.claim_book_topic(book, now=0)
    q.mark_done(book, "One", now=10)
    assert q.claim_book_topic(book, now=20) == "Two"
    assert json.loads((book / "completed_topics.json").read_text()) == ["One"]


def test_stale_local_copy_cannot_undo_a_remote_completion(book, tmp_path):
    store = DriveStore(MemoryBackend(), "audiobook")
    q.claim_book_topic(book, now=1)
    q.mark_done(book, "One", now=2)
    q.sync_queue(book, store)

    stale = tmp_path / "stale_runner" / book.name
    shutil.copytree(book, stale)
    (stale / q.STATE_FILE).write_text(json.dumps({
        "version": 3, "topics": {"One": {"status": "in_progress", "attempts": 0, "rev": 1, "updated_at": 1}}}))

    merged = q.sync_queue(stale, store)
    assert merged["topics"]["One"]["status"] == "completed"


def test_merge_keeps_the_highest_attempt_count():
    local = {"version": 3, "topics": {"A": {"status": "in_progress", "attempts": 2, "rev": 5, "updated_at": 9}}}
    remote = {"version": 3, "topics": {"A": {"status": "in_progress", "attempts": 3, "rev": 2, "updated_at": 3}}}
    merged = q.merge_queue(local, remote)
    assert merged["topics"]["A"]["attempts"] == 3
    assert merged["topics"]["A"]["rev"] == 5


def test_job_id_is_stable_and_unique(book):
    assert q.job_id(book, "One") == q.job_id(book, "One")
    assert q.job_id(book, "One") != q.job_id(book, "Two")
