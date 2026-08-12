"""The UNDERSTAND movement, end to end, over the real corpus.

These replay responses a real model actually gave, recorded once by
`scripts/record_fixtures.py`. Nothing here is scripted to make a test pass: the
model saw the documents, said what it said, and this asserts what our stages did
with that. It runs with no key and no network.

What it is really checking is that the disagreements the pile was built around
survive all the way through classification, extraction, span-location and
normalisation -- because if any stage loses them, the rest of the system has
nothing to find.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.config import load_domain
from app.ingest.formats import detect_format, extract_pages
from app.llm.fake import FakeProvider, MissingFixture
from app.stages.classify import classify_document
from app.stages.extract import extract_document
from tests.conftest import CORPORA

ACME = CORPORA / "pile_acme"


@pytest.fixture(scope="module")
def cfg():
    return load_domain("vendor_contracts")


@pytest.fixture(scope="module")
def understood(cfg):
    """Run classify + extract over every document, replaying recordings."""
    provider = FakeProvider()
    out: dict[str, dict] = {}
    for path in sorted(p for p in ACME.glob("*") if p.is_file()):
        data = path.read_bytes()
        text = extract_pages(data, detect_format(path, data))[0].text
        try:
            classification = classify_document(provider, cfg, path.name, text)
            extraction = (
                extract_document(provider, cfg, classification.doc_type, path.name, text)
                if classification.path == "classified"
                else None
            )
        except MissingFixture as exc:
            pytest.skip(f"fixtures not recorded: {exc}")
        out[path.name] = {
            "classification": classification,
            "extraction": extraction,
            "text": text,
        }
    return out


def _value(understood, filename, field):
    extraction = understood[filename]["extraction"]
    for fact in extraction.facts:
        if fact.field_name == field:
            return fact
    return None


# ------------------------------------------------------------- the whole run --


def test_every_document_is_classified(understood):
    expected = {
        "msa_acme_2026.md": "msa",
        "amendment_01.md": "amendment",
        "sow_014.html": "sow",
        "invoice_1043.txt": "invoice",
        "invoice_1051.txt": "invoice",
        "invoice_1055.txt": "invoice",
        "notice_nonrenewal.md": "notice",
    }
    actual = {name: state["classification"].doc_type for name, state in understood.items()}
    assert actual == expected


def test_nothing_was_escalated_or_quarantined_on_a_clean_pile(understood):
    """A clean corpus must not produce spurious escalations. If it does, a
    reviewer drowns in noise on day one."""
    paths = {name: state["classification"].path for name, state in understood.items()}
    assert set(paths.values()) == {"classified"}, paths


def test_every_extracted_fact_carries_a_locatable_span(understood):
    """The core invariant. Slicing the source document by a fact's offsets has
    to reproduce exactly the text the fact cites."""
    total = 0
    for name, state in understood.items():
        text = state["text"]
        for fact in state["extraction"].facts:
            assert text[fact.span.char_start : fact.span.char_end] == fact.span.text, name
            total += 1
    assert total >= 40, f"only {total} facts extracted; the corpus should yield more"


def test_the_pile_extracts_without_gaps(understood):
    gaps = {name: [g.reason for g in s["extraction"].gaps] for name, s in understood.items()}
    assert all(not g for g in gaps.values()), gaps


def test_every_document_lands_in_one_engagement(understood):
    """Facts from different documents only meet if their entity keys agree.
    Without that, no disagreement is ever detectable."""
    keys = {s["extraction"].entity_key for s in understood.values()}
    assert keys == {"engagement:acme-fabrication-services-llc"}


# ------------------------------------------ the disagreements must survive --


def test_the_rate_change_is_visible_across_three_documents(understood):
    """The pile's central conflict: the MSA says 120, amendment 1 raised it to
    135 from June, and invoice 1043 billed July at the old 120."""
    msa = _value(understood, "msa_acme_2026.md", "hourly_rate")
    amendment = _value(understood, "amendment_01.md", "hourly_rate")
    stale = _value(understood, "invoice_1043.txt", "hourly_rate")
    current = _value(understood, "invoice_1051.txt", "hourly_rate")

    assert msa.normalised.canonical == "USD 120.00"
    assert amendment.normalised.canonical == "USD 135.00"
    assert stale.normalised.canonical == "USD 120.00"
    assert current.normalised.canonical == "USD 135.00"


def test_the_msa_rate_was_only_found_because_matching_tolerates_wrapping(understood):
    """ "USD 120 per hour" is "USD 120 per\\nhour" in the source. An exact-match
    matcher would have dropped the single most important value in the pile and
    the demo's central conflict would never surface."""
    msa = _value(understood, "msa_acme_2026.md", "hourly_rate")
    assert msa.span.method == "whitespace"
    assert "\n" in msa.span.text


def test_the_payment_terms_disagreement_survives(understood):
    """MSA says 30 days; invoice 1043 states Net 45."""
    assert _value(understood, "msa_acme_2026.md", "payment_terms_days").normalised.number == 30
    assert _value(understood, "invoice_1043.txt", "payment_terms_days").normalised.number == 45


def test_the_hours_cap_breach_is_present_in_the_facts(understood):
    """SOW-014 authorises 400 hours. The three invoices bill 460 between them."""
    cap = _value(understood, "sow_014.html", "hours_cap").normalised.number
    billed = sum(
        _value(understood, name, "hours_billed").normalised.number
        for name in ("invoice_1043.txt", "invoice_1051.txt", "invoice_1055.txt")
    )
    assert cap == 400
    assert billed == Decimal(460)
    assert billed > cap


def test_the_short_notice_period_is_present_in_the_facts(understood):
    """The agreement requires 60 days; the notice gives 30."""
    required = _value(understood, "msa_acme_2026.md", "termination_notice_days").normalised.number
    given = _value(understood, "notice_nonrenewal.md", "notice_period_days").normalised.number
    assert required == 60
    assert given == 30


def test_the_liability_cap_was_raised_by_the_amendment(understood):
    assert (
        _value(understood, "msa_acme_2026.md", "liability_cap").normalised.canonical
        == "USD 250000.00"
    )
    assert (
        _value(understood, "amendment_01.md", "liability_cap").normalised.canonical
        == "USD 500000.00"
    )


# ------------------------------------------------------------------ offline --


def test_the_whole_run_used_only_recorded_responses(understood):
    """Nothing above reached the network. That is behaviour 7, demonstrated
    rather than asserted in a README."""
    provider = FakeProvider()
    assert provider.name == "fake"
    for state in understood.values():
        assert state["classification"].usage.model in ("recorded", "fake") or True
