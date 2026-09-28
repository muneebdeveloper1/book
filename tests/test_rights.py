"""The rights gate is a policy screen with a hard floor: 'reject' always blocks."""
import pytest

from src.channels.audiobook import enforce_rights
from src.config import load_settings
from src.domain.models import RightsResult
from src.errors import RightsBlocked
from src.research.engine import reproduction_requested, validate_rights
from tests.conftest import FakeLLM, FakeSearcher


@pytest.mark.parametrize("topic", [
    "Read the full text of Atomic Habits",
    "A word-for-word reading of chapter 3",
    "Complete audiobook of The Hobbit",
    "chapter-by-chapter reading of the book",
    "Reproduce the article verbatim",
])
def test_reproduction_requests_are_caught_before_any_model_call(topic):
    assert reproduction_requested(topic)
    result = validate_rights(None, None, topic)  # no LLM/searcher needed: the check is deterministic
    assert result.rights_status == "reject"


def test_ordinary_educational_topics_are_not_caught():
    assert reproduction_requested("How compound interest works") is None


def test_reject_cannot_be_overridden_even_with_the_unknown_override():
    rights = RightsResult(rights_status="reject", notes="asks for reproduction")
    with pytest.raises(RightsBlocked):
        enforce_rights(rights, allow_unknown=True)


def test_unknown_blocks_by_default_and_can_be_allowed_explicitly():
    rights = RightsResult(rights_status="unknown", notes="not enough evidence")
    with pytest.raises(RightsBlocked):
        enforce_rights(rights, allow_unknown=False)
    enforce_rights(rights, allow_unknown=True)  # explicit opt-in only


def test_safe_passes():
    enforce_rights(RightsResult(rights_status="safe"), allow_unknown=False)


def test_search_outage_downgrades_to_unknown_never_to_safe():
    class BrokenSearcher(FakeSearcher):
        def search(self, query, limit=5):
            raise ConnectionError("search down")

    llm = FakeLLM(rights_status="unknown")
    result = validate_rights(llm, BrokenSearcher(), "Some ordinary topic")
    assert result.rights_status == "unknown"


def test_result_carries_a_disclaimer():
    assert "not legal" in RightsResult(rights_status="safe").disclaimer.lower()


def test_unexpected_status_is_treated_as_unknown():
    class OddLLM(FakeLLM):
        def json(self, *args, **kwargs):
            return {"rights_status": "definitely fine", "notes": ""}

    result = validate_rights(OddLLM(), FakeSearcher(), "A topic")
    assert result.rights_status == "unknown"
