"""A second pile of documents, and the claim that only a second pile can support.

Everything else in this suite runs against `pile_acme`. A system tuned until one
corpus comes out right is not a system that works -- it is a corpus that has been
fitted to. `pile_northwind` is a different counterparty, a different sector, a
different document set, and above all a **different answer**: the rules that fire
on acme are the rules that pass here, and the rules that pass on acme are the
rules that fire here.

That inversion is the point. A playbook whose findings were an artefact of the
code rather than of the documents would produce the same shape on both piles.

The corpus also carries two things acme has none of -- a document that tries to
give the system orders, and one in a format the system refuses -- plus a `.docx`,
so a format the README claims support for is exercised end to end rather than
only in a unit test.
"""

from __future__ import annotations

import uuid

import pytest

from app import operations as ops
from app.domain.config import load_domain
from app.store import repository as repo
from app.store.engine import fetch_one

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def piles(db_available, _test_piles):
    """Both corpora understood once, offline, and then compared.

    Module-scoped because these are two complete runs. Every test below reads
    the same two answers instead of recomputing them, and the piles are handed
    to the session-scoped cleanup the rest of the suite uses.
    """
    if not db_available:
        pytest.skip("Postgres not reachable; start it with `docker compose up -d db`")
    out = {}
    for name, corpus in (("northwind", "pile_northwind"), ("acme", "pile_acme")):
        pile_id = ops.create_pile(f"test-{name}-{uuid.uuid4().hex[:8]}")["pile_id"]
        _test_piles.append(pile_id)
        out[name] = (pile_id, ops.start_run(pile_id, corpus))
    return out


def outcomes(pile_id: str) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = {}
    for row in ops.findings(pile_id)["findings"]:
        grouped.setdefault(row["outcome"], set()).add(row["rule_key"])
    return grouped


# ------------------------------------------------------- the pile itself --


def test_the_second_corpus_runs_offline_with_no_key(piles):
    """Behaviour 7, on documents whose recordings were made later than the rest.
    If this needs a network it will say so by failing, not by being slow."""
    _, run = piles["northwind"]
    assert run["status"] == "awaiting_approval"
    assert run["documents"] == 9
    assert run["facts"] == 46
    assert run["model_calls"] == 14, (
        "seven readable documents, classified and extracted; the quarantined and "
        "the unsupported one must cost nothing"
    )


def test_the_answers_are_not_the_same_answers(piles):
    """The load-bearing test in this file.

    Acme's violations are Northwind's satisfied rules and the reverse, exactly.
    A playbook that always found the same four problems would pass every test
    written against one corpus and be worthless.
    """
    north = outcomes(piles["northwind"][0])
    acme = outcomes(piles["acme"][0])

    assert (
        north["violated"] == acme["satisfied"]
    ), "every rule acme satisfies is a rule northwind breaks"
    assert (
        north["satisfied"] == acme["violated"]
    ), "and every rule acme breaks is a rule northwind satisfies"
    assert north["violated"] == {
        "invoice_within_term",
        "liability_cap_present",
        "no_instructions_in_sources",
    }
    # The one rule neither pile can answer, because no extraction schema asks
    # for the field. Same on both, and honestly reported on both.
    assert (
        north["not_enough_evidence"]
        == acme["not_enough_evidence"]
        == {"amendment_references_parent"}
    )


def test_a_disagreement_is_not_automatically_a_violation(piles):
    """The case acme does not contain.

    The MSA says USD 95 and the amendment says USD 110, so the documents
    disagree and that reaches the gate as a conflict. But every invoice bills at
    the amended rate, so the *rule* is satisfied. A conflict is a question about
    what the documents say; a finding is a judgement about whether someone is
    complying. Collapsing the two would report this engagement as broken when it
    is working exactly as amended.
    """
    pile_id, run = piles["northwind"]
    assert run["conflicts"] == 1

    conflicts = [
        p for p in ops.list_proposals(run["run_id"])["proposals"] if p["kind"] == "conflict"
    ]
    assert [c["payload"]["field"] for c in conflicts] == ["hourly_rate"]
    assert set(conflicts[0]["payload"]["values"]) == {"USD 110.00", "USD 95.00"}

    satisfied = outcomes(pile_id)["satisfied"]
    assert "rate_matches_current_agreement" in satisfied


# ------------------------------------------- documents that never got read --


def test_a_document_that_gives_orders_is_quarantined_and_reported(piles):
    """Behaviour 8, reached through a full run rather than a unit test.

    The letter is a real covering letter with an injected block. It never
    reaches the model -- the deterministic screen fires first -- so it is also
    the reason this corpus costs fourteen calls for nine documents.
    """
    pile_id, run = piles["northwind"]
    assert run["quarantined"] == ["vendor_letter.md"]

    violated = [
        f
        for f in ops.findings(pile_id)["findings"]
        if f["rule_key"] == "no_instructions_in_sources"
    ]
    assert violated and violated[0]["outcome"] == "violated"
    assert "vendor_letter.md" in violated[0]["detail"]

    facts = ops.list_documents(pile_id)["documents"]
    letter = next(d for d in facts if d["filename"] == "vendor_letter.md")
    assert letter["status"] == "quarantined" and letter["is_gap"]


def test_a_refused_format_is_a_gap_and_not_a_silent_loss(piles):
    """A pile that quietly dropped a document would produce a register that is
    confidently wrong. The csv is recorded, counted, and named."""
    pile_id, _ = piles["northwind"]
    documents = ops.list_documents(pile_id)["documents"]
    csv = next(d for d in documents if d["filename"] == "rate_card_2026.csv")
    assert csv["status"] == "unsupported" and csv["is_gap"]
    assert ".csv" in (csv["ingest_note"] or "")


def test_the_register_says_which_documents_it_did_not_read(piles):
    """The section is called "What Could Not Be Established", and until this
    corpus existed it only ever listed missing *fields*. Two documents sat in
    the pile, unread, and the deliverable was silent about both."""
    _, run = piles["northwind"]
    gaps = next(
        p
        for p in ops.list_proposals(run["run_id"])["proposals"]
        if p["kind"] == "section_patch" and p["payload"]["section_key"] == "gaps"
    )
    body = gaps["payload"]["body"]

    assert "rate_card_2026.csv" in body and "format is not supported" in body
    assert "vendor_letter.md" in body and "quarantined" in body
    # And the third kind of gap, which was already reported: a field no document
    # in the pile states at all.
    assert "liability_cap" in body


# ---------------------------------------------------------------- formats --


def test_a_docx_is_read_with_exact_offsets(conn, piles):
    """The README claims docx support. Acme's corpus is markdown, text and html,
    so nothing proved that claim in a real run until now -- and a citation whose
    offsets do not land on the quoted text is a citation that cannot be
    checked."""
    pile_id, _ = piles["northwind"]
    conn.rollback()
    facts = repo.load_sourced_facts(conn, load_domain("vendor_contracts"), pile_id)
    from_docx = [f for f in facts if f.document.endswith(".docx")]
    assert {f.field for f in from_docx} >= {"notice_period_days", "notice_type"}

    page = fetch_one(
        conn,
        """
        SELECT p.text FROM page p JOIN document d ON d.id = p.document_id
        WHERE d.pile_id = %s AND d.filename = %s AND p.page_no = 1
    """,
        (pile_id, "notice_nonrenewal.docx"),
    )["text"]

    for sourced in from_docx:
        span = sourced.fact.span
        assert (
            page[span.char_start : span.char_end] == span.text
        ), f"{sourced.field}: the offsets do not land on the quoted text"
