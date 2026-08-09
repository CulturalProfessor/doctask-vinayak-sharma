"""Vector search: the embedder, the index, and the split it is there to stop.

Two halves. The first needs no database -- the embedder is a pure function and
its properties are what everything above it relies on. The second needs
Postgres, because the claim being tested is that pgvector actually returns the
right rows, and a test that mocked the database would only prove the SQL string
was constructed.
"""
from __future__ import annotations

import pytest

from app.retrieval.embed import DIMENSIONS, HashedNgramEmbedder, embed_or_none, get_embedder
from app.retrieval.search import (
    index_status,
    nearest_entity,
    remember_entity,
    search_spans,
    vector_literal,
)
from app.stages.entities import resolve_entity

# ---------------------------------------------------------- the embedder --

def test_embedding_is_deterministic():
    """The same string gives the same vector, always.

    Load-bearing rather than pedantic: `span.embedding` is written once and
    queried much later, so an embedder that varied by process would make a
    document's own text stop matching itself across a restart.
    """
    a = HashedNgramEmbedder().embed("Acme Fabrication Services LLC")
    b = HashedNgramEmbedder().embed("Acme Fabrication Services LLC")
    assert a == b
    assert len(a) == DIMENSIONS


def test_embedding_is_unit_length():
    vector = get_embedder().embed("Harbourline Freight Holdings")
    assert sum(v * v for v in vector) == pytest.approx(1.0, abs=1e-6)


def test_empty_text_refuses_rather_than_returning_zeros():
    """A zero vector would be worse than an error.

    Cosine distance against zero is undefined and pgvector answers NaN, which
    sorts as an excellent match -- so a silent zero vector puts punctuation at
    the top of every search result.
    """
    with pytest.raises(ValueError):
        get_embedder().embed("   ...   ")
    assert embed_or_none("   ...   ") is None


def _similarity(a: str, b: str) -> float:
    embedder = get_embedder()
    x, y = embedder.embed(a), embedder.embed(b)
    return sum(p * q for p, q in zip(x, y))


def test_abbreviated_name_stays_near_and_unrelated_name_stays_far():
    """The property the entity threshold is set against.

    Not a tuned number: the gap between these two cases is what makes 0.55 a
    defensible line rather than a lucky one.
    """
    near = _similarity("Acme Fabrication Services LLC", "Acme Fabrication Svcs")
    far = _similarity("Acme Fabrication Services LLC", "Harbourline Freight Ltd")
    assert near > 0.55
    assert far < 0.2
    assert near > far * 2


def test_vector_literal_round_trips_dimension():
    literal = vector_literal(get_embedder().embed("anything at all"))
    assert literal.startswith("[") and literal.endswith("]")
    assert len(literal.split(",")) == DIMENSIONS


# ------------------------------------------------- resolution without a db --

def test_near_match_is_only_consulted_when_lexical_rules_fail():
    """Exact and containment still win, and the index is never asked.

    The order matters: a similarity score that could override an exact match
    would make identity depend on a threshold, which is exactly what entity
    resolution exists to avoid.
    """
    asked: list[str] = []

    def near(name: str):
        asked.append(name)
        return {"entity_key": "engagement:someone-else", "name": "Someone Else",
                "similarity": 0.99}

    known = ["engagement:acme-fabrication-services-llc"]
    exact = resolve_entity("Acme Fabrication Services LLC", known,
                           "engagement:{counterparty_slug}", near_match=near)
    assert exact.method == "exact"

    contains = resolve_entity("Brightwell Manufacturing Inc. and Acme Fabrication "
                              "Services LLC", known,
                              "engagement:{counterparty_slug}", near_match=near)
    assert contains.method == "contains"
    assert asked == []


def test_near_match_escalates_and_never_merges():
    """A similarity hit produces a question, not a decision.

    The whole point of the fourth step: it must convert a silent split into a
    stated escalation without ever attaching a document to an engagement on the
    strength of a score.
    """
    def near(name: str):
        return {"entity_key": "engagement:acme-fabrication-services-llc",
                "name": "Acme Fabrication Services LLC", "similarity": 0.71}

    result = resolve_entity("Acme Fabrication Svcs",
                            ["engagement:acme-fabrication-services-llc"],
                            "engagement:{counterparty_slug}", near_match=near)

    assert result.method == "near"
    assert result.escalate is True
    # Not merged.
    assert result.entity_key is None
    # But the reviewer is told which engagement to compare it against.
    assert result.candidates == ["engagement:acme-fabrication-services-llc"]
    assert "Acme Fabrication Services LLC" in (result.note or "")


def test_without_a_near_match_hook_a_new_name_is_still_a_new_engagement():
    """The step is additive. Nothing about the old paths changed."""
    result = resolve_entity("Someone Entirely Different",
                            ["engagement:acme-fabrication-services-llc"],
                            "engagement:{counterparty_slug}")
    assert result.method == "new"
    assert result.entity_key == "engagement:someone-entirely-different"
    assert result.confident is True


# ------------------------------------------------------ the index, for real --

@pytest.mark.db
def test_entity_index_finds_an_abbreviation_in_postgres(conn, pile):
    """pgvector returns the right row, not merely a row.

    This is the half that cannot be faked: the vector is written as text, cast
    by Postgres, and compared with `<=>`. If the cast, the dimension or the
    distance operator were wrong, the pure-Python tests above would still pass.
    """
    remember_entity(conn, pile, "engagement:acme-fabrication-services-llc",
                    "Acme Fabrication Services LLC")
    remember_entity(conn, pile, "engagement:harbourline-freight-ltd",
                    "Harbourline Freight Ltd")

    hit = nearest_entity(conn, pile, "Acme Fabrication Svcs", min_similarity=0.55)
    assert hit is not None
    assert hit["entity_key"] == "engagement:acme-fabrication-services-llc"
    assert 0.55 <= hit["similarity"] <= 1.0

    # And the threshold is a real filter, not decoration.
    assert nearest_entity(conn, pile, "Zeta Logistics GmbH",
                          min_similarity=0.55) is None


@pytest.mark.db
def test_first_name_wins_so_the_index_does_not_drift(conn, pile):
    """Re-resolving the same engagement must not rewrite what it is known as.

    Otherwise the index tracks whatever the most recent document happened to
    say, and the comparison a later document is judged against changes under it.
    """
    key = "engagement:acme-fabrication-services-llc"
    remember_entity(conn, pile, key, "Acme Fabrication Services LLC")
    remember_entity(conn, pile, key, "Brightwell Manufacturing Inc. and Acme "
                                     "Fabrication Services LLC")

    hit = nearest_entity(conn, pile, "Acme Fabrication Services LLC",
                         min_similarity=0.5)
    assert hit["name"] == "Acme Fabrication Services LLC"


@pytest.mark.db
def test_search_returns_provenance_and_scopes_to_the_pile(conn, pile, make_pile):
    """A hit is citable, and one pile cannot see another's documents."""
    other = make_pile()
    _write_span(conn, pile, "msa_acme_2026.md",
                "The hourly rate is USD 120 per hour for all engineering work.")
    _write_span(conn, other, "msa_northwind_2026.md",
                "The hourly rate is EUR 95 per hour for all engineering work.")

    hits = search_spans(conn, pile, "hourly rate per hour", limit=5)
    assert hits, "the pile's own span should be found"
    assert {h["document"] for h in hits} == {"msa_acme_2026.md"}
    top = hits[0]
    # The provenance every other claim in this system carries.
    assert top["char_start"] < top["char_end"]
    assert top["page_no"] == 1
    assert 0.0 < top["similarity"] <= 1.0


@pytest.mark.db
def test_index_status_distinguishes_an_empty_pile_from_an_empty_answer(conn, pile):
    """Why the search operation returns it.

    "No hits" from an indexed pile and "no hits" from a pile with nothing in it
    are different claims, and a caller that cannot tell them apart will report
    the second as the first.
    """
    assert index_status(conn, pile)["spans"] == 0
    _write_span(conn, pile, "msa_acme_2026.md", "Termination requires 60 days notice.")
    status = index_status(conn, pile)
    assert status["spans"] == 1
    assert status["spans_embedded"] == 1
    assert status["embedder"] == get_embedder().name


@pytest.mark.db
def test_unrelated_wording_is_filtered_out_rather_than_ranked(conn, pile):
    """The floor exists because character trigrams never score zero.

    Any two English strings share letters, so an unanswerable query still
    returns *something* unless a threshold stops it. Found by driving the UI: a
    search for "certificate of insurance" over this corpus came back with
    "NOTICE OF NON-RENEWAL" at 0.18 and "AMENDMENT NO. 2" at 0.15, rendered
    under the heading "passages found". Three unrelated headings presented as
    evidence is worse than an empty result, because the empty result is the
    honest answer and the list is a fabricated one.
    """
    _write_span(conn, pile, "notice_nonrenewal.md", "NOTICE OF NON-RENEWAL")
    _write_span(conn, pile, "amendment_01.md", "AMENDMENT NO. 2")
    _write_span(conn, pile, "msa_acme_2026.md",
                "Services are billed at a standard rate of USD 120 per hour.")

    from app.operations import MIN_SEARCH_SIMILARITY

    noise = search_spans(conn, pile, "certificate of insurance",
                         min_similarity=MIN_SEARCH_SIMILARITY)
    assert noise == [], "an unanswerable query must return nothing, not letters in common"

    # And the floor is not so high that it swallows real matches.
    real = search_spans(conn, pile, "hourly rate per hour",
                        min_similarity=MIN_SEARCH_SIMILARITY)
    assert [h["document"] for h in real] == ["msa_acme_2026.md"]


@pytest.mark.db
def test_a_span_with_no_features_is_stored_and_simply_not_searchable(conn, pile):
    """A NULL embedding is an honest answer, not a dropped row."""
    _write_span(conn, pile, "invoice_1043.txt", "----------")
    status = index_status(conn, pile)
    assert status["spans"] == 1
    assert status["spans_embedded"] == 0
    assert search_spans(conn, pile, "anything") == []


@pytest.mark.db
def test_the_pipeline_wires_the_index_to_resolution(conn, pile):
    """The link the two halves above do not prove on their own.

    One test asserts that `nearest_entity` returns the right row; another that
    `resolve_entity` escalates when handed a hit. Neither says the graph
    actually connects them, and a `near_match=` argument that was never passed
    would leave both passing while the pile split exactly as before.

    So this builds the closure `Nodes` builds, from the same configuration, and
    drives resolution through it.
    """
    from app.domain.config import load_domain
    from app.graph.nodes import Nodes
    from app.llm.fake import FakeProvider

    cfg = load_domain("vendor_contracts")
    nodes = Nodes(conn=conn, provider=FakeProvider(), cfg=cfg)

    # The pile has met this party once, under this spelling.
    remember_entity(conn, pile, "engagement:acme-fabrication-services-llc",
                    "Acme Fabrication Services LLC")

    # A later document spells it differently. Containment fails -- "svcs" is not
    # "services" -- so before the fourth step this fell straight through to a
    # second engagement, and every disagreement between the two halves of the
    # pile stopped being found.
    resolution = resolve_entity(
        "Acme Fabrication Svcs",
        ["engagement:acme-fabrication-services-llc"],
        "engagement:{counterparty_slug}",
        cfg.reconciliation.get("entity"),
        near_match=nodes._near_entity(pile),
    )

    assert resolution.method == "near"
    assert resolution.escalate is True
    assert resolution.entity_key is None
    assert resolution.candidates == ["engagement:acme-fabrication-services-llc"]
    assert resolution.similarity is not None and resolution.similarity >= 0.55

    # And an unrelated counterparty is still just a new engagement. The step must
    # not turn every new party into an escalation.
    fresh = resolve_entity(
        "Zeta Logistics GmbH",
        ["engagement:acme-fabrication-services-llc"],
        "engagement:{counterparty_slug}",
        cfg.reconciliation.get("entity"),
        near_match=nodes._near_entity(pile),
    )
    assert fresh.method == "new"
    assert fresh.escalate is False


def _write_span(conn, pile_id: str, filename: str, text: str) -> str:
    """One document, one page, one span -- through the same insert the pipeline
    uses, so the embedding is written the way production writes it."""
    from app.domain.models import SourcedFact
    from app.domain.spans import SpanMatch
    from app.ingest.ingest import ingest_bytes
    from app.stages.extract import ExtractedFact
    from app.store import repository as repo

    result = ingest_bytes(conn, pile_id, filename, text.encode("utf-8"))
    fact = ExtractedFact(
        field_name="hourly_rate", value_raw="USD 120", quote=text,
        span=SpanMatch(0, len(text), text, "exact", 1.0), normalised=None,
        value_type="money", unit=None, confidence=0.9)
    repo.persist_facts(conn, pile_id, None, result.document_id,
                       [SourcedFact(document=filename, doc_type="msa",
                                    entity_key="engagement:test", fact=fact)])
    return result.document_id
