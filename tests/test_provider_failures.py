"""An unreachable model and a badly-behaved model are different failures.

Discovered by running the container without its recordings: every document
"escalated" and the run reported success with zero facts. It looked healthy.
That is the worst possible shape for a broken deployment, and the reason these
two live in separate branches now.
"""
from __future__ import annotations

import pytest

from app.domain.config import load_domain
from app.llm.base import ProviderError, ProviderUnavailable
from app.llm.fake import FakeProvider, MissingFixture
from app.stages.classify import classify_document
from app.stages.extract import extract_document
from tests.support import ScriptedProvider

TEXT = "Master Services Agreement between Brightwell and Acme, at USD 120 per hour."


@pytest.fixture(scope="module")
def cfg():
    return load_domain("vendor_contracts")


def test_a_missing_recording_is_an_unavailable_provider():
    assert issubclass(MissingFixture, ProviderUnavailable)
    assert issubclass(ProviderUnavailable, ProviderError)


def test_classify_refuses_to_escalate_an_unreachable_provider(cfg, tmp_path):
    """Escalating would file seven documents for human review and report a
    successful run, when the truth is the deployment cannot reach a model."""
    provider = FakeProvider(root=tmp_path)  # no recordings here
    with pytest.raises(ProviderUnavailable):
        classify_document(provider, cfg, "msa.md", TEXT)


def test_extract_refuses_to_retry_an_unreachable_provider(cfg, tmp_path):
    """Retrying a broken deployment just burns the retry budget before failing."""
    provider = FakeProvider(root=tmp_path)
    with pytest.raises(ProviderUnavailable):
        extract_document(provider, cfg, "msa", "msa.md", TEXT)


def test_bad_model_output_still_escalates_rather_than_raising(cfg):
    """The other side of the split: the model is reachable and simply unhelpful.
    That is a document-level problem and a person should look at it."""
    result = classify_document(ScriptedProvider("not json at all"), cfg, "msa.md", TEXT)
    assert result.escalate is True
    assert result.path == "escalate"


def test_bad_model_output_still_retries_in_extraction(cfg):
    provider = ScriptedProvider("prose", "more prose")
    result = extract_document(provider, cfg, "msa", "msa.md", TEXT)
    assert result.path == "skipped_unparseable"
    assert result.attempts == 2


def test_recordings_ship_with_the_application_not_the_tests():
    """They are what make the demo and the container run offline, so they cannot
    live somewhere only the test suite looks."""
    from app.settings import settings

    assert settings.fixture_root.name == "llm"
    assert settings.fixture_root.parent.name == "recordings"
    assert settings.fixture_root.is_dir(), "recordings directory is missing"
    assert list(settings.fixture_root.glob("*.json")), "no recordings committed"
