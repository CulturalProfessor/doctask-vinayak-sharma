"""Normalisation decides what counts as the same value, which means it also
decides what counts as a disagreement. Both directions are load-bearing: a
false agreement hides a real conflict, a false disagreement floods the gate
with noise until a reviewer stops reading it."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.config import load_domain
from app.domain.normalize import (
    NormalisationError,
    normalise,
    parse_date,
    parse_duration,
    parse_money,
    parse_number,
    values_agree,
)


@pytest.fixture(scope="module")
def cfg():
    return load_domain("vendor_contracts").normalization


@pytest.fixture(scope="module")
def recon():
    return load_domain("vendor_contracts").reconciliation


# ------------------------------------------------------------------ numbers --


def test_prefers_the_parenthetical_numeral_contracts_actually_use():
    """Contracts write numbers twice. "thirty (30) days" must read as 30, and
    naive digit-scraping would work here but fail on "Section 6 ... sixty (60)"."""
    assert parse_number("thirty (30) days") == 30
    assert parse_number("twelve (12) months") == 12
    assert parse_number("four hundred (400) hours") == 400


def test_reads_spelled_numbers_with_no_numeral():
    assert parse_number("sixty days") == 60
    assert parse_number("ninety days written notice") == 90


def test_thousands_separators_survive():
    assert parse_number("USD 19,200") == Decimal("19200")
    assert parse_number("250,000") == Decimal("250000")


def test_unparseable_number_is_none_not_zero():
    """Zero would be a silently wrong value that compares cleanly against other
    numbers. None forces the caller to record a gap."""
    assert parse_number("to be agreed") is None
    assert parse_number("") is None


# -------------------------------------------------------------------- money --


def test_money_normalises_across_notations(cfg):
    forms = ["USD 120", "$120", "US$120.00", "120 USD"]
    canon = {parse_money(f, cfg).canonical for f in forms}
    assert canon == {"USD 120.00"}


def test_money_keeps_its_currency(cfg):
    assert parse_money("£5,000", cfg).currency == "GBP"
    assert parse_money("€900", cfg).currency == "EUR"
    assert parse_money("120", cfg).currency == "USD"  # configured default


def test_currencies_are_never_silently_converted(cfg, recon):
    """Comparing USD to EUR without a rate and a date is how you produce a
    confident wrong answer. The group has to become a conflict instead."""
    assert values_agree(parse_money("USD 100", cfg), parse_money("EUR 100", cfg), recon) is False


# ----------------------------------------------------------------- duration --


def test_duration_carries_its_unit(cfg):
    thirty = parse_duration("thirty (30) days", cfg)
    assert thirty.number == 30 and thirty.unit == "days"
    twelve = parse_duration("twelve (12) months", cfg)
    assert twelve.number == 12 and twelve.unit == "months"


def test_business_days_are_not_calendar_days(cfg):
    """Treating them as equal would understate every deadline built from them."""
    business = parse_duration("10 business days", cfg)
    calendar = parse_duration("10 days", cfg)
    assert business.unit == "business_days"
    assert calendar.unit == "days"
    assert business.canonical != calendar.canonical


# --------------------------------------------------------------------- date --


def test_reads_the_date_formats_the_corpus_uses(cfg):
    assert parse_date("1 March 2026", cfg).as_date == date(2026, 3, 1)
    assert parse_date("2026-06-01", cfg).as_date == date(2026, 6, 1)


def test_ambiguous_numeric_date_is_flagged_not_guessed(cfg):
    """03/04/2026 is March in one country and April in another. Guessing makes a
    termination date wrong by a month, so the doubt has to travel with the value."""
    parsed = parse_date("03/04/2026", cfg)
    assert parsed is not None
    assert parsed.ambiguous is True
    assert parsed.note and "ambiguous" in parsed.note


def test_unambiguous_numeric_date_is_not_flagged(cfg):
    parsed = parse_date("25/12/2026", cfg)
    assert parsed is None or parsed.ambiguous is False


def test_unparseable_date_is_none(cfg):
    assert parse_date("on or about the feast of St Swithin", cfg) is None


# --------------------------------------------------------------- agreement --


def test_the_acme_rate_change_reads_as_a_disagreement(cfg, recon):
    """The conflict the demo corpus is built around: the MSA says 120, the
    amendment says 135, and invoice 1043 still bills 120."""
    msa = parse_money("USD 120 per hour", cfg)
    amendment = parse_money("USD 135 per hour", cfg)
    invoice = parse_money("USD 120", cfg)
    assert values_agree(msa, amendment, recon) is False
    assert values_agree(msa, invoice, recon) is True


def test_rounding_is_not_a_disagreement(cfg, recon):
    """Without tolerance, a rounded invoice total floods the gate with noise."""
    assert (
        values_agree(parse_money("USD 19,200.00", cfg), parse_money("USD 19,200.01", cfg), recon)
        is True
    )


def test_a_real_difference_survives_tolerance(cfg, recon):
    assert (
        values_agree(parse_money("USD 19,200", cfg), parse_money("USD 21,000", cfg), recon) is False
    )


def test_payment_terms_30_versus_45_disagree(cfg, recon):
    """Duration tolerance is zero on purpose: 30 days and 45 days are never the
    same payment term, however close a relative tolerance might call them."""
    assert (
        values_agree(
            parse_duration("thirty (30) days", cfg), parse_duration("Net 45 days", cfg), recon
        )
        is False
    )


def test_text_agreement_ignores_case_and_spacing(cfg, recon):
    a = normalise("text", "Acme Fabrication Services LLC", cfg)
    b = normalise("text", "acme  fabrication   services llc", cfg)
    assert values_agree(a, b, recon) is True


def test_different_types_never_agree(cfg, recon):
    assert (
        values_agree(normalise("money", "120", cfg), normalise("number", "120", cfg), recon)
        is False
    )


# ---------------------------------------------------------------- dispatch --


def test_normalise_dispatches_on_declared_type(cfg):
    assert normalise("money", "USD 135", cfg).canonical == "USD 135.00"
    assert normalise("date", "1 June 2026", cfg).canonical == "2026-06-01"
    assert normalise("number", "160", cfg).canonical == "160"


def test_blank_values_normalise_to_none(cfg):
    assert normalise("money", "", cfg) is None
    assert normalise("date", "   ", cfg) is None


def test_unknown_value_type_is_a_loud_error(cfg):
    with pytest.raises(NormalisationError):
        normalise("colour", "blue", cfg)
