"""The recorded provider is what makes "tests run without a live key" true.

Its own contract matters: same prompt must replay the same response, and a
prompt with no recording must fail loudly rather than invent something. A fake
that quietly improvises would make every downstream test prove nothing.
"""

from __future__ import annotations

import json

import pytest

from app.llm.base import Completion, ProviderError, Usage, call_key
from app.llm.fake import FakeProvider, MissingFixture, record


def test_same_prompt_replays_the_same_response(tmp_path):
    record("classify", "sys", "user text", Completion("msa", Usage(10, 2, 0.001)), root=tmp_path)
    provider = FakeProvider(root=tmp_path)
    first = provider.complete(purpose="classify", system="sys", user="user text")
    second = provider.complete(purpose="classify", system="sys", user="user text")
    assert first.text == second.text == "msa"
    assert first.usage.tokens_in == 10


def test_missing_recording_fails_loudly_with_the_filename(tmp_path):
    provider = FakeProvider(root=tmp_path)
    with pytest.raises(MissingFixture) as exc:
        provider.complete(purpose="extract", system="sys", user="never recorded")
    assert "RECORD_FIXTURES=1" in str(exc.value)
    assert "extract" in str(exc.value)


def test_a_changed_prompt_is_a_different_call(tmp_path):
    """Editing a prompt must invalidate its recording. Otherwise a stale
    fixture would keep a test green after the thing it tests has changed."""
    record("classify", "sys", "original", Completion("msa"), root=tmp_path)
    provider = FakeProvider(root=tmp_path)
    with pytest.raises(MissingFixture):
        provider.complete(purpose="classify", system="sys", user="edited")


def test_call_key_is_stable_across_processes():
    assert call_key("p", "s", "u") == call_key("p", "s", "u")
    assert call_key("p", "s", "u") != call_key("p", "s", "u2")


def test_json_parsing_tolerates_fenced_output():
    assert Completion('```json\n{"a": 1}\n```').json() == {"a": 1}
    assert Completion('{"a": 2}').json() == {"a": 2}


def test_non_json_response_raises_rather_than_returning_junk():
    with pytest.raises(ProviderError, match="did not return JSON"):
        Completion("I'm afraid I can't do that").json()


def test_recorded_fixture_is_readable_json(tmp_path):
    path = record("classify", "sys", "u", Completion("msa", Usage(1, 1, 0.0)), root=tmp_path)
    data = json.loads(path.read_text())
    assert data["text"] == "msa"
    assert data["purpose"] == "classify"
