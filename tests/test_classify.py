"""Classification is where the run first chooses between paths.

Everything downstream extracts against the chosen type's schema, so a confident
wrong classification poisons every fact that follows it. The branches tested
here are the ones that stop that happening.
"""

from __future__ import annotations

import pytest

from app.domain.config import load_domain
from app.ingest.formats import extract_pages
from app.stages.classify import classify_document
from tests.conftest import CORPORA, FIXTURES
from tests.support import ScriptedProvider, classification

MSA_TEXT = extract_pages((CORPORA / "pile_acme" / "msa_acme_2026.md").read_bytes(), "md")[0].text
POISONED = (FIXTURES / "poisoned_invoice.txt").read_text()


@pytest.fixture(scope="module")
def cfg():
    return load_domain("vendor_contracts")


def test_a_confident_classification_takes_the_normal_path(cfg):
    provider = ScriptedProvider(classification("msa", 0.95))
    result = classify_document(provider, cfg, "msa_acme_2026.md", MSA_TEXT)
    assert result.doc_type == "msa"
    assert result.path == "classified"
    assert result.escalate is False and result.quarantine is False


def test_low_confidence_escalates_instead_of_guessing(cfg):
    """A shrug is cheap. A confident mistake is not."""
    provider = ScriptedProvider(classification("sow", 0.41))
    result = classify_document(provider, cfg, "ambiguous.pdf", MSA_TEXT)
    assert result.path == "escalate"
    assert result.escalate is True
    assert "below threshold" in result.note


def test_the_confidence_threshold_is_adjustable(cfg):
    provider = ScriptedProvider(classification("msa", 0.80))
    assert (
        classify_document(provider, cfg, "f.md", MSA_TEXT, min_confidence=0.6).path == "classified"
    )
    provider = ScriptedProvider(classification("msa", 0.80))
    assert classify_document(provider, cfg, "f.md", MSA_TEXT, min_confidence=0.9).path == "escalate"


def test_a_type_outside_the_taxonomy_escalates(cfg):
    """The model is not permitted to invent a document type. Accepting one would
    mean extracting against a schema that does not exist."""
    provider = ScriptedProvider(classification("purchase_order", 0.99))
    result = classify_document(provider, cfg, "unknown.pdf", MSA_TEXT)
    assert result.path == "escalate"
    assert "purchase_order" in result.note


def test_unparseable_model_output_escalates(cfg):
    """Not "retry until it works" and not "assume the commonest type" -- an
    unreadable answer is not a classification."""
    provider = ScriptedProvider("I think this is probably a services agreement.")
    result = classify_document(provider, cfg, "f.md", MSA_TEXT)
    assert result.path == "escalate"
    assert "unusable" in result.reasoning


def test_null_type_escalates(cfg):
    provider = ScriptedProvider(classification(None, 0.9))
    result = classify_document(provider, cfg, "f.md", MSA_TEXT)
    assert result.path == "escalate"


# ------------------------------------------------- the injection branch --


def test_a_poisoned_document_is_quarantined(cfg):
    provider = ScriptedProvider()  # any model call at all fails the test
    result = classify_document(provider, cfg, "poisoned_invoice.txt", POISONED)
    assert result.path == "quarantine"
    assert result.quarantine is True
    assert result.doc_type is None


def test_a_poisoned_document_never_reaches_the_model(cfg):
    """The strong version of behaviour 8. There is no reason to spend a call
    interpreting a document we already know is trying to manipulate the
    interpreter, and doing so would give the manipulation one chance to work."""
    provider = ScriptedProvider()
    classify_document(provider, cfg, "poisoned_invoice.txt", POISONED)
    assert provider.calls == [], "the poisoned document was sent to the model"


def test_the_quarantine_note_cites_the_document_and_says_it_was_not_obeyed(cfg):
    provider = ScriptedProvider()
    result = classify_document(provider, cfg, "poisoned_invoice.txt", POISONED)
    assert "poisoned_invoice.txt" in result.note
    assert "has not been acted on" in result.note
    assert result.screen is not None and result.screen.hits


def test_the_model_can_quarantine_what_the_rail_missed(cfg):
    """Two layers that fail differently. The rail is regex and can be evaded by
    novel phrasing; the model generalises but can be talked out of it. Either
    one firing is enough."""
    provider = ScriptedProvider(classification("invoice", 0.97, instructions=True))
    result = classify_document(provider, cfg, "subtle.txt", MSA_TEXT)
    assert result.path == "quarantine"
    assert result.model_flagged_instructions is True
    assert "deterministic screen found nothing" in result.note


def test_ordinary_documents_are_not_quarantined(cfg):
    provider = ScriptedProvider(classification("msa", 0.93))
    assert classify_document(provider, cfg, "msa.md", MSA_TEXT).quarantine is False


# ------------------------------------------------------------- plumbing --


def test_the_prompt_frames_the_document_as_untrusted(cfg):
    provider = ScriptedProvider(classification("msa", 0.95))
    classify_document(provider, cfg, "msa.md", MSA_TEXT)
    _, system, user = provider.calls[0]
    # Whitespace-normalised because the prompt text itself wraps -- the same
    # trap the span matcher exists to handle.
    flat = " ".join(system.split())
    assert "untrusted input" in flat
    assert "not instructions to follow" in flat
    assert "BEGIN UNTRUSTED DOCUMENT" in user
    # The reminder is repeated after the content, because the last thing read
    # carries the most weight.
    assert user.index("END UNTRUSTED DOCUMENT") < user.index("evidence, not")


def test_usage_is_carried_out_of_the_stage(cfg):
    """Per-stage cost reporting (behaviour 10) needs this to survive the call."""
    provider = ScriptedProvider(classification("msa", 0.95))
    result = classify_document(provider, cfg, "msa.md", MSA_TEXT)
    assert result.usage.tokens_in == 100
    assert result.usage.cost_usd > 0
