"""Span matching is the hinge the whole provenance claim turns on.

The property that matters most is the negative one: a quote that is not in the
document must return nothing. A matcher that always finds something somewhere
would make every citation in the system worthless while looking like it worked.
"""
from __future__ import annotations

from app.domain.spans import find_all_spans, find_span
from app.ingest.formats import extract_pages
from tests.conftest import CORPORA

MSA = (CORPORA / "pile_acme" / "msa_acme_2026.md").read_bytes()
MSA_TEXT = extract_pages(MSA, "md")[0].text


def test_exact_match_reports_exact_offsets():
    match = find_span(MSA_TEXT, "USD 250,000")
    assert match is not None and match.method == "exact"
    assert MSA_TEXT[match.char_start:match.char_end] == "USD 250,000"


def test_finds_a_quote_that_wraps_across_a_line_break():
    """The case that motivated this module. "USD 120 per hour" is
    "USD 120 per\\nhour" in the source, so `str.index` cannot find it."""
    assert "USD 120 per hour" not in MSA_TEXT
    match = find_span(MSA_TEXT, "USD 120 per hour")
    assert match is not None
    assert match.method == "whitespace"
    # The offsets must point at the original text, newline and all.
    assert MSA_TEXT[match.char_start:match.char_end] == "USD 120 per\nhour"


def test_offsets_always_index_the_original_text():
    """Whatever strategy matched, slicing the source by the reported offsets has
    to reproduce the reported text. Otherwise a reviewer shown the citation sees
    something other than what was cited."""
    for quote in ["USD 120 per hour", "sixty (60) days prior written notice",
                  "twelve (12) months", "State of Delaware"]:
        match = find_span(MSA_TEXT, quote)
        assert match is not None, quote
        assert MSA_TEXT[match.char_start:match.char_end] == match.text


def test_tolerates_case_and_punctuation_drift():
    match = find_span(MSA_TEXT, "state of delaware")
    assert match is not None
    assert match.text.lower() == "state of delaware"


def test_a_quote_that_is_not_in_the_document_returns_nothing():
    """The load-bearing negative. This is what stops an invented quote from
    becoming a citation."""
    assert find_span(MSA_TEXT, "Supplier shall provide unlimited free labour") is None
    assert find_span(MSA_TEXT, "USD 999,999 per hour") is None
    assert find_span(MSA_TEXT, "arbitration in Singapore") is None


def test_a_quote_from_a_different_document_is_not_matched():
    """Words from the same domain, present in the corpus, but not in *this*
    document. A bag-of-words matcher would happily match this."""
    invoice_quote = "Payment Terms:    Net 45 days from receipt."
    assert find_span(MSA_TEXT, invoice_quote) is None


def test_empty_input_is_not_a_match():
    assert find_span(MSA_TEXT, "") is None
    assert find_span(MSA_TEXT, "   \n ") is None
    assert find_span("", "anything") is None


def test_paraphrased_whitespace_and_wording_still_matches():
    """Models reflow and lightly reword quotes even when told not to."""
    match = find_span(MSA_TEXT, "Invoices are payable within thirty (30) days of receipt")
    assert match is not None
    assert "thirty (30) days" in match.text


def test_a_near_miss_below_threshold_is_rejected():
    """Same shape, different number. Accepting this would cite the wrong clause
    for a value -- the most damaging possible failure here."""
    match = find_span(MSA_TEXT, "Invoices are payable within ninety (90) days of receipt",
                      min_score=0.95)
    assert match is None


def test_find_all_keeps_the_misses_visible():
    """Unfound quotes stay in the result as None so the caller can count gaps
    rather than silently ending up with fewer citations than facts."""
    result = find_all_spans(MSA_TEXT, ["USD 120 per hour", "a clause that does not exist"])
    assert result["USD 120 per hour"] is not None
    assert result["a clause that does not exist"] is None
    assert len(result) == 2


def test_repeated_text_matches_the_first_occurrence_deterministically():
    haystack = "rate is USD 120 per hour. Again: rate is USD 120 per hour."
    first = find_span(haystack, "rate is USD 120 per hour")
    assert first is not None and first.char_start == 0
    assert find_span(haystack, "rate is USD 120 per hour").char_start == first.char_start
