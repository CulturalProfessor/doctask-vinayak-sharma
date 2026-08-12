"""A new document arrives.

The brief is precise about what this must not be: not a rewrite, and not a full
re-run that happens to reproduce the same bytes. These tests are the proof of
the parts that are easy to claim and hard to demonstrate -- that model work is
genuinely incremental, and that the sections the new source did not affect are
byte-identical rather than merely believed unchanged.
"""

from __future__ import annotations

import pytest

from app.domain.config import load_domain
from app.graph import pipeline
from app.llm.fake import FakeProvider, MissingFixture
from app.stages import gate as gate_module
from app.stages.gate import Decision
from app.store import repository as repo
from app.store.engine import fetch_all, fetch_one
from tests.conftest import CORPORA

pytestmark = pytest.mark.db

ACME = sorted(p for p in (CORPORA / "pile_acme").glob("*") if p.is_file())
ARRIVAL = CORPORA / "arrivals" / "amendment_02.md"


@pytest.fixture
def cfg():
    return load_domain("vendor_contracts")


@pytest.fixture
def committed(conn, pile, cfg):
    """A pile with version 1 of the register committed.

    Approved and committed through the run itself -- decide the items, then
    resume past the gate -- rather than by calling the committer directly. That
    is the path a reviewer actually takes, and it is the one that has to work
    after a restart.
    """
    try:
        result = pipeline.run_understand(FakeProvider(), cfg, pile, ACME)
    except MissingFixture as exc:
        pytest.skip(f"fixtures not recorded: {exc}")
    _approve_everything(conn, result)
    return pipeline.resume(FakeProvider(), cfg, run_id=result.run_id)


@pytest.fixture
def arrival(conn, pile, cfg, committed):
    return pipeline.run_incremental(FakeProvider(), cfg, pile, ARRIVAL)


def _approve_everything(conn, result) -> None:
    """Decide every open item yes, on a connection of the reviewer's own."""
    for proposal in repo.list_proposals(conn, result.run_id, status="pending"):
        gate_module.decide(conn, result.run_id, [Decision(str(proposal["id"]), True)], "test")
    conn.commit()


# ------------------------------------------- an update costs like an update --


def test_only_the_arriving_document_reaches_the_model(conn, arrival):
    """Where the cost of an update actually lives. The other seven documents are
    never sent anywhere -- one classify and one extract, not eight of each."""
    assert arrival.model_calls == 2


def test_the_untouched_sections_are_byte_identical(conn, pile, committed, arrival):
    """Recomputed from scratch and still matching, not skipped and assumed."""
    before = {
        row["section_key"]: row["content_hash"]
        for row in repo.sections_for_version(conn, pile, version=1)
    }
    assert arrival.delta.unchanged, "the arrival should leave some sections alone"
    for key in arrival.delta.unchanged:
        assert arrival.register.section(key).content_hash == before[key]


def test_something_actually_changed(conn, arrival):
    """The mirror of the test above. A delta that changes nothing would make
    byte-identity trivially true and prove nothing at all."""
    assert arrival.delta.changed
    assert "commercials" in arrival.delta.changed, "a new rate must move commercials"
    assert "billing" in arrival.delta.unchanged, "no invoice arrived"


def test_only_what_moved_is_proposed(conn, arrival):
    """A reviewer of an update should see the update. Re-proposing six unchanged
    sections would bury the ones that moved."""
    proposed = {
        p["payload"]["section_key"] for p in arrival.gate.proposals if p["kind"] == "section_patch"
    }
    assert proposed == set(arrival.delta.changed + arrival.delta.added)
    assert "billing" not in proposed


def test_the_new_contradiction_is_surfaced(conn, arrival):
    """The arriving amendment raises the rate again, contradicting what the
    register already says. That is surfaced, not silently resolved."""
    fields = {c.field for c in arrival.proposed_conflicts}
    assert "hourly_rate" in fields
    assert "termination_notice_days" in fields
    rate = next(c for c in arrival.proposed_conflicts if c.field == "hourly_rate")
    assert "USD 145.00" in rate.distinct_values
    assert rate.proposed.document == "amendment_02.md"


def test_identity_noise_is_not_reported_as_a_disagreement(conn, arrival):
    """Amendment 2 names both parties where the MSA names one. Entity resolution
    already settled that; reporting it again as a conflict is a second bite at a
    decision already made."""
    assert "counterparty" not in {c.field for c in arrival.proposed_conflicts}


# --------------------------------------------------------- entity identity --


def test_the_arrival_joins_the_existing_engagement(conn, pile, arrival):
    """The failure this guards against is silent and total: a second engagement
    means no group holds both rates, and the contradiction is never found."""
    keys = repo.known_entity_keys(conn, pile)
    assert keys == ["engagement:acme-fabrication-services-llc"]


def test_resolution_is_recorded_as_its_own_stage(conn, arrival):
    rows = fetch_all(
        conn,
        """
        SELECT stage, path_taken FROM stage_event WHERE run_id = %s AND stage = 'resolve_entity'
    """,
        (arrival.run_id,),
    )
    assert len(rows) == 1
    assert rows[0]["path_taken"] == "contains"


# ------------------------------------------------------------ idempotence --


def test_the_same_document_arriving_twice_changes_nothing(conn, pile, cfg, arrival):
    """Not "skips the file" -- changes nothing. A watcher that fires twice on one
    write must not produce a second update."""
    facts_before = fetch_one(conn, "SELECT count(*) AS n FROM fact WHERE pile_id = %s", (pile,))[
        "n"
    ]
    again = pipeline.run_incremental(FakeProvider(), cfg, pile, ARRIVAL)
    assert again.path == "noop_duplicate"
    assert again.model_calls == 0, "a duplicate must not cost a model call"
    assert (
        fetch_one(conn, "SELECT count(*) AS n FROM fact WHERE pile_id = %s", (pile,))["n"]
        == facts_before
    )


# ---------------------------------------------------------------- commit --


def test_committing_carries_unchanged_sections_forward(conn, pile, cfg, arrival):
    """Without this the new version would hold only the sections that moved and
    silently lose the rest."""
    _approve_everything(conn, arrival)
    result = pipeline.resume(FakeProvider(), cfg, run_id=arrival.run_id).committed

    assert result["version"] == 2
    assert result["sections_carried"] == len(arrival.delta.unchanged)
    sections = repo.sections_for_version(conn, pile, version=2)
    assert len(sections) == 6


def test_carried_sections_keep_their_hash_and_get_no_audit_row(conn, pile, cfg, arrival):
    """Nothing changed about them, and an audit row claiming otherwise would
    make the trail lie."""
    before = {
        row["section_key"]: row["content_hash"]
        for row in repo.sections_for_version(conn, pile, version=1)
    }
    _approve_everything(conn, arrival)
    pipeline.resume(FakeProvider(), cfg, run_id=arrival.run_id)

    after = {
        row["section_key"]: row["content_hash"]
        for row in repo.sections_for_version(conn, pile, version=2)
    }
    for key in arrival.delta.unchanged:
        assert after[key] == before[key]

    audited = {
        row["section_key"]
        for row in repo.audit_trail(conn, pile)
        if row["run_id"] and str(row["run_id"]) == arrival.run_id
    }
    assert audited == set(arrival.delta.changed + arrival.delta.added)


def test_the_audit_names_the_document_that_caused_the_change(conn, pile, cfg, arrival):
    """ "What changed, when, and because of which source" -- the third of those
    is the one a log cannot reconstruct after the fact."""
    _approve_everything(conn, arrival)
    pipeline.resume(FakeProvider(), cfg, run_id=arrival.run_id)

    rows = [
        row for row in repo.audit_trail(conn, pile) if row["cause_document"] == "amendment_02.md"
    ]
    assert rows, "no audit row names the arriving document"
    for row in rows:
        assert row["from_hash"] and row["to_hash"]
        assert row["from_hash"] != row["to_hash"]


def test_nothing_commits_before_the_update_is_reviewed(conn, pile, arrival):
    """The gate applies to updates exactly as it does to first runs."""
    versions = fetch_one(
        conn, "SELECT max(version) AS v FROM deliverable WHERE pile_id = %s", (pile,)
    )["v"]
    assert versions == 1, "version 2 must not exist until the update is approved"
