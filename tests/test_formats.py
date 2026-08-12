"""Text extraction is where provenance either survives or quietly dies."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingest.formats import UnsupportedFormat, detect_format, extract_pages
from tests.conftest import CORPORA


def test_detects_format_from_extension():
    assert detect_format(Path("a.md")) == "md"
    assert detect_format(Path("a.HTML")) == "html"
    assert detect_format(Path("a.txt")) == "txt"


def test_magic_bytes_beat_a_lying_extension():
    """A PDF named .txt must be read as a PDF. Reading it as text would produce
    binary noise that becomes confidently wrong facts."""
    assert detect_format(Path("invoice.txt"), b"%PDF-1.7\n...") == "pdf"


def test_unsupported_extension_raises():
    with pytest.raises(UnsupportedFormat):
        detect_format(Path("sheet.xlsx"))


def test_char_offsets_into_markdown_are_exact():
    """The offsets a span will later cite have to land on the right characters."""
    data = (CORPORA / "pile_acme" / "msa_acme_2026.md").read_bytes()
    pages = extract_pages(data, "md")
    assert len(pages) == 1
    text = pages[0].text
    start = text.index("USD 120")
    assert text[start : start + 7] == "USD 120"


def test_extraction_preserves_line_wrapping_rather_than_reflowing():
    """A constraint discovered from the corpus, kept deliberately.

    "USD 120 per hour" wraps across a newline in the MSA, so it is not a
    contiguous substring. Real documents wrap, and silently reflowing them here
    would shift every character offset downstream and corrupt provenance. The
    burden belongs on the span matcher, which must search whitespace-tolerantly
    -- this test exists so that requirement cannot be forgotten.
    """
    data = (CORPORA / "pile_acme" / "msa_acme_2026.md").read_bytes()
    text = extract_pages(data, "md")[0].text
    assert "USD 120 per hour" not in text
    assert "USD 120 per\nhour" in text
    assert " ".join(text.split()).count("USD 120 per hour") == 1


def test_html_extraction_drops_markup_but_keeps_content():
    data = (CORPORA / "pile_acme" / "sow_014.html").read_bytes()
    pages = extract_pages(data, "html")
    text = pages[0].text
    assert "<h1>" not in text and "<p>" not in text
    assert "four hundred (400) hours" in text
    assert "USD 54,000" in text


def test_empty_input_yields_no_pages():
    assert extract_pages(b"   \n  ", "txt") == []
