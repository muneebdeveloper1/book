"""LLM gateway, image fallback and HTTP safety."""
from types import SimpleNamespace

import pytest

from src.errors import AuthError, InvalidResponseError, PermanentMediaError
from src.providers.gemini import Gemini
from src.utils.budget import Budget, NullBudget
from src.utils.http import assert_public_url


class RecordingClient:
    def __init__(self, text="plain text", finish="STOP"):
        self.text = text
        self.finish = finish
        self.configs = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.configs.append(dict(config))
        return SimpleNamespace(text=self.text, candidates=[SimpleNamespace(finish_reason=self.finish)])


def test_plain_text_is_not_requested_in_json_mode(settings):
    """The old gateway forced application/json on narration too, mangling prose."""
    client = RecordingClient()
    llm = Gemini(settings, NullBudget(), gemini_key="k", client=client)
    llm.text("write a script")
    assert "response_mime_type" not in client.configs[-1]


def test_json_mode_is_requested_for_json_calls(settings):
    client = RecordingClient(text='{"a": 1}')
    llm = Gemini(settings, NullBudget(), gemini_key="k", client=client)
    assert llm.json("give me json") == {"a": 1}
    assert client.configs[-1]["response_mime_type"] == "application/json"


def test_truncated_output_is_rejected(settings):
    client = RecordingClient(text="a script cut off mid-", finish="FinishReason.MAX_TOKENS")
    llm = Gemini(settings, NullBudget(), gemini_key="k", client=client, sleep=lambda s: None)
    with pytest.raises(InvalidResponseError, match="truncated"):
        llm.text("write")


def test_json_fences_are_stripped(settings):
    client = RecordingClient(text='```json\n{"ok": true}\n```')
    llm = Gemini(settings, NullBudget(), gemini_key="k", client=client)
    assert llm.json("x") == {"ok": True}


def test_budget_stops_runaway_calls(settings):
    from src.errors import BudgetExceeded

    llm = Gemini(settings, Budget({"gemini_calls": 2}), gemini_key="k", client=RecordingClient())
    llm.text("one")
    llm.text("two")
    with pytest.raises(BudgetExceeded):
        llm.text("three")


def test_missing_credentials_is_an_auth_error(settings):
    with pytest.raises(AuthError):
        Gemini(settings, NullBudget())


@pytest.mark.parametrize("url,addresses", [
    ("https://metadata.example/x", ["169.254.169.254"]),   # cloud metadata service
    ("http://localhost/x", ["127.0.0.1"]),
    ("https://internal.example/x", ["10.0.0.5"]),
    ("https://internal.example/x", ["192.168.1.7"]),
])
def test_private_addresses_are_refused(url, addresses):
    with pytest.raises(PermanentMediaError):
        assert_public_url(url, lambda host: addresses)


def test_non_http_schemes_are_refused():
    with pytest.raises(PermanentMediaError):
        assert_public_url("file:///etc/passwd", lambda host: ["1.1.1.1"])


def test_public_address_is_allowed():
    assert assert_public_url("https://cdn.example/a.mp4", lambda host: ["93.184.216.34"])
