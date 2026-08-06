"""Extraction, and the rule that a fact without a locatable quote is discarded.

That rule throws away values that are probably correct. It is worth it: a value
that is probably correct and definitely uncitable is exactly what an ungrounded
register is made of, and the whole claim of this system is that every cell
traces to a real place in a real document.
"""
from __future__ import annotations

import pytest

from app.domain.config import load_domain
from app.ingest.formats import extract_pages
from app.stages.extract import extract_document, slugify
from tests.conftest import CORPORA
from tests.support import ScriptedProvider, extraction

MSA_TEXT = extract_pages((CORPORA / "pile_acme" / "msa_acme_2026.md").read_bytes(), "md")[0].text


@pytest.fixture(scope="module")
def cfg():
    return load_domain("vendor_contracts")


def _facts(result):
    return {f.field_name: f for f in result.facts}


def test_extracts_facts_bound_to_real_spans(cfg):
    provider = ScriptedProvider(extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC"),
        hourly_rate=("USD 120", "USD 120 per hour"),
        payment_terms_days=("30", "Invoices are payable within thirty (30) days"),
    ))
    result = extract_document(provider, cfg, "msa", "msa_acme_2026.md", MSA_TEXT)
    facts = _facts(result)
    assert set(facts) == {"counterparty", "hourly_rate", "payment_terms_days"}
    for fact in result.facts:
        assert MSA_TEXT[fact.span.char_start:fact.span.char_end] == fact.span.text


def test_a_quote_that_wraps_a_line_is_still_located(cfg):
    """The reason spans.py is whitespace-tolerant. Losing this fact to a line
    break would drop the single most important value in the pile."""
    provider = ScriptedProvider(extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC"),
        hourly_rate=("USD 120", "USD 120 per hour"),
    ))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    rate = _facts(result)["hourly_rate"]
    assert rate.span.method == "whitespace"
    assert rate.span.text == "USD 120 per\nhour"


def test_an_invented_quote_discards_the_fact(cfg):
    """The load-bearing discard. The value 180 might even be right; without a
    locatable source it does not enter the register."""
    provider = ScriptedProvider(extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC"),
        termination_notice_days=("180", "either party may terminate on 180 days notice"),
    ))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert "termination_notice_days" not in _facts(result)
    gap = next(g for g in result.gaps if g.field_name == "termination_notice_days")
    assert gap.reason == "quote not found in the document"
    assert "discarded" in gap.detail


def test_a_fact_with_no_quote_is_discarded(cfg):
    provider = ScriptedProvider({"fields": {
        "counterparty": {"value": "Acme Fabrication Services LLC",
                         "quote": "Acme Fabrication Services LLC", "confidence": 1.0},
        "liability_cap": {"value": "USD 250,000", "confidence": 0.9},
    }})
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert "liability_cap" not in _facts(result)
    assert any(g.reason == "no quote supplied" for g in result.gaps)


def test_a_value_that_cannot_be_normalised_becomes_a_gap(cfg):
    """Quote is genuine, value is not usable. It cannot be compared to anything,
    so carrying it forward would put an incomparable cell in the register."""
    provider = ScriptedProvider(extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC"),
        effective_date=("whenever the parties agree", "effective as of 1 March 2026"),
    ))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert "effective_date" not in _facts(result)
    assert any("could not be read as date" in g.reason for g in result.gaps)


def test_a_missing_required_field_is_reported_as_a_gap(cfg):
    provider = ScriptedProvider(extraction(hourly_rate=("USD 120", "USD 120 per hour")))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert any(g.field_name == "counterparty" and "required" in g.reason
               for g in result.gaps)


def test_absent_optional_fields_are_silent(cfg):
    """Omission is the correct behaviour for a field the document does not
    state, so it must not produce noise."""
    provider = ScriptedProvider(extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC"),
    ))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert [g.field_name for g in result.gaps] == []


# ------------------------------------------------------- retry and skip --

def test_malformed_output_retries_once_with_a_stricter_instruction(cfg):
    provider = ScriptedProvider(
        "Here are the fields you asked for:",
        extraction(counterparty=("Acme Fabrication Services LLC",
                                 "Acme Fabrication Services LLC")),
    )
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert result.attempts == 2
    assert provider.purposes == ["extract", "extract_retry"]
    assert "not valid JSON" in provider.calls[1][2]
    assert _facts(result)["counterparty"]


def test_it_gives_up_rather_than_retrying_forever(cfg):
    provider = ScriptedProvider("not json", "still not json")
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert result.path == "skipped_unparseable"
    assert result.attempts == 2
    assert result.facts == []
    assert any(g.field_name == "*" for g in result.gaps)


def test_a_wrongly_shaped_fields_value_is_a_gap_not_a_crash(cfg):
    provider = ScriptedProvider({"fields": ["hourly_rate", "counterparty"]})
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert result.path == "skipped_unparseable"
    assert result.facts == []


# ---------------------------------------------------------- entity keys --

def test_the_entity_key_groups_documents_by_counterparty(cfg):
    """Without this, an amendment's rate and the MSA's rate never meet, and the
    disagreement the system exists to find stays invisible."""
    provider = ScriptedProvider(extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC"),
    ))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert result.entity_key == "engagement:acme-fabrication-services-llc"


def test_the_same_counterparty_spelled_differently_still_groups(cfg):
    assert slugify("Acme Fabrication Services LLC") == slugify("acme  fabrication SERVICES llc")


def test_no_counterparty_means_no_entity_key(cfg):
    """Guessing a key would silently merge two engagements into one register."""
    provider = ScriptedProvider(extraction(hourly_rate=("USD 120", "USD 120 per hour")))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert result.entity_key is None


def test_usage_accumulates_across_attempts(cfg):
    provider = ScriptedProvider("bad", extraction(
        counterparty=("Acme Fabrication Services LLC", "Acme Fabrication Services LLC")))
    result = extract_document(provider, cfg, "msa", "msa.md", MSA_TEXT)
    assert result.usage.tokens_in == 200, "a retry's cost has to be counted too"
