"""The human gate, against a real database.

Graded behaviour 3. The properties here are the ones a naive implementation
gets wrong by treating a review as a single yes/no over the whole run.
"""
from __future__ import annotations

import pytest

from app.domain.config import load_domain
from app.graph.pipeline import run_understand
from app.llm.fake import FakeProvider, MissingFixture
from app.stages import gate as gate_module
from app.stages.gate import Decision
from app.store import repository as repo
from app.store.engine import fetch_all, fetch_one
from tests.conftest import CORPORA

pytestmark = pytest.mark.db

ACME = sorted(p for p in (CORPORA / "pile_acme").glob("*") if p.is_file())


@pytest.fixture
def cfg():
    return load_domain("vendor_contracts")


@pytest.fixture
def run(conn, pile, cfg):
    try:
        return run_understand(conn, FakeProvider(), cfg, pile, ACME)
    except MissingFixture as exc:
        pytest.skip(f"fixtures not recorded: {exc}")


# ------------------------------------------------- nothing before review --

def test_the_run_halts_awaiting_approval(conn, run):
    assert repo.get_run(conn, run.run_id)["status"] == "awaiting_approval"
    assert run.gate.is_open is True


def test_no_deliverable_exists_before_anyone_decides(conn, pile, run):
    """A process killed at this point has written nothing to approve away."""
    assert fetch_one(conn, "SELECT count(*) AS n FROM deliverable WHERE pile_id = %s",
                     (pile,))["n"] == 0
    # Scoped to this pile. An unscoped count passes only while the rest of the
    # database happens to be empty, which makes it a test of the environment
    # rather than of the gate.
    assert fetch_one(conn, """
        SELECT count(*) AS n FROM section s
        JOIN deliverable d ON d.id = s.deliverable_id
        WHERE d.pile_id = %s
    """, (pile,))["n"] == 0
    assert fetch_one(conn, "SELECT count(*) AS n FROM audit WHERE pile_id = %s",
                     (pile,))["n"] == 0


def test_every_conflict_and_every_section_is_its_own_item(conn, run):
    """A reviewer who can only accept or reject the whole deliverable is not
    reviewing it."""
    kinds = [p["kind"] for p in run.gate.proposals]
    assert kinds.count("conflict") == 3
    assert kinds.count("section_patch") == 6


def test_a_conflict_proposal_carries_its_evidence(conn, run):
    """The reviewer decides from the proposal, so the quotes and offsets have to
    travel with it rather than living only in the register."""
    proposal = next(p for p in run.gate.proposals
                    if p["kind"] == "conflict" and p["payload"]["field"] == "hourly_rate")
    payload = proposal["payload"]
    assert set(payload["values"]) == {"USD 120.00", "USD 135.00"}
    assert payload["proposed"] == "USD 135.00"
    assert payload["rationale"]
    for member in payload["members"]:
        assert member["quote"] and member["char_end"] > member["char_start"]


def test_conflicts_are_recorded_open(conn, pile, run):
    rows = fetch_all(conn, "SELECT status FROM conflict WHERE pile_id = %s", (pile,))
    assert len(rows) == 3
    assert {r["status"] for r in rows} == {"open"}


# -------------------------------------------------------- per-item review --

def test_rejecting_one_item_does_not_discard_the_rest(conn, pile, run, cfg):
    """The property the brief calls out by name."""
    conflicts = [p for p in run.gate.proposals if p["kind"] == "conflict"]
    sections = [p for p in run.gate.proposals if p["kind"] == "section_patch"]

    decisions = [Decision(str(conflicts[0]["id"]), False, "rate change is historical")]
    decisions += [Decision(str(p["id"]), True) for p in conflicts[1:]]
    decisions += [Decision(str(p["id"]), True) for p in sections]

    counts = gate_module.decide(conn, run.run_id, decisions, decided_by="vinayak")
    assert counts == {"approved": 8, "rejected": 1, "ignored": 0}

    result = gate_module.commit(conn, pile, run.run_id, run.register)
    assert result["approved"] == 8 and result["rejected"] == 1
    assert result["sections_written"] == 6

    statuses = {r["status"] for r in
                fetch_all(conn, "SELECT status FROM conflict WHERE pile_id = %s", (pile,))}
    assert statuses == {"approved", "rejected"}


def test_a_rejected_section_is_not_written(conn, pile, run):
    """Every decision is respected, including the negative ones."""
    for proposal in run.gate.proposals:
        approved = not (proposal["kind"] == "section_patch"
                        and proposal["payload"]["section_key"] == "billing")
        gate_module.decide(conn, run.run_id, [Decision(str(proposal["id"]), approved)],
                           decided_by="vinayak")
    gate_module.commit(conn, pile, run.run_id, run.register)

    written = {row["section_key"] for row in repo.sections_for_version(conn, pile)}
    assert "billing" not in written
    assert "commercials" in written


def test_a_run_cannot_commit_while_anything_is_undecided(conn, pile, run):
    """Partial review is a state worth being explicit about. A default here is a
    decision nobody made."""
    first = run.gate.proposals[0]
    gate_module.decide(conn, run.run_id, [Decision(str(first["id"]), True)], "vinayak")
    with pytest.raises(ValueError, match="still pending"):
        gate_module.commit(conn, pile, run.run_id, run.register)


def test_a_decision_is_final(conn, run):
    """A second call does not silently flip a judgement something downstream may
    already have acted on."""
    proposal = run.gate.proposals[0]
    gate_module.decide(conn, run.run_id, [Decision(str(proposal["id"]), True)], "vinayak")
    counts = gate_module.decide(
        conn, run.run_id, [Decision(str(proposal["id"]), False, "changed my mind")],
        "vinayak")
    assert counts["ignored"] == 1
    row = fetch_one(conn, "SELECT status FROM proposal WHERE id = %s", (proposal["id"],))
    assert row["status"] == "approved"


def test_rejecting_everything_writes_nothing(conn, pile, run):
    for proposal in run.gate.proposals:
        gate_module.decide(conn, run.run_id, [Decision(str(proposal["id"]), False)],
                           "vinayak")
    result = gate_module.commit(conn, pile, run.run_id, run.register)
    assert result["sections_written"] == 0
    assert repo.sections_for_version(conn, pile) == []


# ------------------------------------------------------------ the audit --

def test_the_audit_says_what_changed_and_why(conn, pile, run):
    for proposal in run.gate.proposals:
        gate_module.decide(conn, run.run_id, [Decision(str(proposal["id"]), True)],
                           "vinayak")
    gate_module.commit(conn, pile, run.run_id, run.register)

    trail = repo.audit_trail(conn, pile)
    assert len(trail) == 6
    for row in trail:
        assert row["from_hash"] is None, "first version has no predecessor"
        assert row["to_hash"] and row["committed_at"] and row["run_id"]


def test_committed_sections_match_the_hashes_that_were_reviewed(conn, pile, run):
    """What was approved is what landed, byte for byte."""
    for proposal in run.gate.proposals:
        gate_module.decide(conn, run.run_id, [Decision(str(proposal["id"]), True)],
                           "vinayak")
    gate_module.commit(conn, pile, run.run_id, run.register)

    stored = {row["section_key"]: row["content_hash"]
              for row in repo.sections_for_version(conn, pile)}
    assert stored == run.register.hashes


# ------------------------------------------------------ facts and report --

def test_every_persisted_fact_has_a_span_in_its_own_document(conn, pile, run):
    rows = repo.facts_for_pile(conn, pile)
    assert len(rows) == 48
    for row in rows:
        assert row["span_id"] and row["char_end"] > row["char_start"]
        page = fetch_one(conn, "SELECT text FROM page WHERE document_id = %s",
                         (row["document_id"],))
        assert page["text"][row["char_start"]:row["char_end"]] == row["span_text"]


def test_the_run_report_covers_every_stage(conn, run):
    report = repo.run_report(conn, run.run_id)
    stages = {row["stage"] for row in report["stages"]}
    assert {"ingest", "classify", "extract", "reconcile", "compose"} <= stages
    assert report["totals"]["tokens_in"] > 0


def test_the_run_report_records_which_branch_each_stage_took(conn, run):
    report = repo.run_report(conn, run.run_id)
    by_stage = {row["stage"]: row["paths"] for row in report["stages"]}
    assert by_stage["classify"] == ["classified"]
    assert by_stage["extract"] == ["extracted"]
