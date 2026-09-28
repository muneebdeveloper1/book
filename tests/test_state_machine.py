"""The state machine must refuse illegal moves, not merely record them."""
import json

import pytest

from src.errors import InvalidTransition
from src.state import manager


@pytest.fixture
def state_file(tmp_path):
    path = tmp_path / "state.json"
    manager.init(path, "job-1")
    return path


def test_cannot_skip_a_stage(state_file):
    with pytest.raises(InvalidTransition):
        manager.transition(state_file, "RENDERING")


def test_cannot_finish_a_stage_that_never_started(state_file):
    with pytest.raises(InvalidTransition):
        manager.transition(state_file, "SOURCE_VALIDATED")


def test_cannot_complete_with_stages_outstanding(state_file):
    with pytest.raises(InvalidTransition) as exc:
        manager.transition(state_file, "COMPLETED")
    assert "not done" in str(exc.value)


def test_cannot_return_to_queued(state_file):
    manager.transition(state_file, "VALIDATING_SOURCE")
    with pytest.raises(InvalidTransition):
        manager.transition(state_file, "QUEUED")


def test_terminal_states_reject_everything(state_file):
    for stage in manager.STAGES:
        manager.transition(state_file, stage.active)
        manager.transition(state_file, stage.done)
    manager.transition(state_file, "COMPLETED")
    with pytest.raises(InvalidTransition):
        manager.transition(state_file, "CLEANUP")


def test_full_happy_path_records_every_stage(state_file):
    for stage in manager.STAGES:
        manager.transition(state_file, stage.active)
        manager.transition(state_file, stage.done)
    data = manager.transition(state_file, "COMPLETED")
    assert data["completed"] == list(manager.STAGE_KEYS)
    assert data["state"] == "COMPLETED"


def test_failure_then_resume_continues_where_it_stopped(state_file):
    manager.transition(state_file, "VALIDATING_SOURCE")
    manager.transition(state_file, "SOURCE_VALIDATED")
    manager.transition(state_file, "RESEARCHING")
    manager.transition(state_file, "FAILED_RETRYABLE", error="boom")
    data = manager.get(state_file)
    assert data["retry_count"] == 1
    manager.transition(state_file, "RESEARCHING")
    with pytest.raises(InvalidTransition):
        manager.transition(state_file, "AUDIO_READY")


def test_invalidate_rolls_back_later_stages(state_file):
    for key in ("source", "research", "script"):
        stage = manager.stage(key)
        manager.transition(state_file, stage.active)
        manager.transition(state_file, stage.done)
    manager.invalidate_from(state_file, "research", "artifact missing")
    assert manager.get(state_file)["completed"] == ["source"]


def test_legacy_state_is_migrated(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"job_id": "x", "state": "RENDERING", "last_successful_step": "VISUALS_READY"}))
    data = manager.get(path)
    assert data["schema"] == manager.SCHEMA
    assert data["completed"] == ["source", "research", "script", "audio", "visuals"]


def test_skipping_a_stage_is_explicit(state_file):
    manager.mark_skipped(state_file, "source")
    assert manager.get(state_file)["completed"] == ["source"]
    with pytest.raises(InvalidTransition):
        manager.mark_skipped(state_file, "script")
