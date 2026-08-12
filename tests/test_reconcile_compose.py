"""Reconciliation and composition, over the real recorded pile.

The two properties under test are the ones the whole deliverable rests on: a
conflict is surfaced but never resolved, and a section's content hash changes if
and only if that section's content changed.
"""

from __future__ import annotations

import pytest

from app.domain.config import load_domain
from app.domain.models import SourcedFact
from app.graph import pipeline
from app.llm.fake import FakeProvider, MissingFixture
from app.stages.compose import NOT_ESTABLISHED, compose
from app.stages.reconcile import reconcile
from tests.conftest import CORPORA

pytestmark = pytest.mark.db

ACME = sorted(p for p in (CORPORA / "pile_acme").glob("*") if p.is_file())


@pytest.fixture
def cfg():
    return load_domain("vendor_contracts")


@pytest.fixture
def report(pile, cfg):
    """One real run through the graph.

    This needs the database, and that is not incidental. There is exactly one
    orchestrator now; a second, database-free one kept around for the
    convenience of these tests is how the entity-resolution fix came to exist on
    one path and not the other (see PROGRESS.md).
    """
    try:
        return pipeline.run_understand(FakeProvider(), cfg, pile, ACME)
    except MissingFixture as exc:
        pytest.skip(f"fixtures not recorded: {exc}")


def _conflict(report, field):
    return next((c for c in report.conflicts if c.field == field), None)


# ------------------------------------------------------------ reconcile --


def test_exactly_the_three_real_disagreements_are_found(report):
    """The corpus was built with three contractual disagreements in it. Finding
    more than three means noise; finding fewer means a miss."""
    assert {c.field for c in report.conflicts} == {
        "hourly_rate",
        "liability_cap",
        "payment_terms_days",
    }


def test_per_document_fields_are_not_reported_as_disagreements(report):
    """Three invoices legitimately have three invoice numbers, dates and
    amounts. Reporting those as conflicts would bury the three real ones under
    six false ones, and a reviewer stops reading long before that."""
    fields = {c.field for c in report.conflicts}
    for noisy in (
        "invoice_number",
        "invoice_date",
        "amount_due",
        "hours_billed",
        "service_period",
        "effective_date",
    ):
        assert noisy not in fields
    assert report.reconciliation.per_document["invoice_number"] == 3


def test_the_stale_rate_conflict_names_every_source(report):
    conflict = _conflict(report, "hourly_rate")
    assert set(conflict.distinct_values) == {"USD 120.00", "USD 135.00"}
    documents = {m.document for m in conflict.members}
    assert {"msa_acme_2026.md", "amendment_01.md", "invoice_1043.txt"} <= documents


def test_precedence_proposes_the_amendment_not_the_invoice(report):
    """An invoice is evidence of what was billed, never authority on the rate."""
    conflict = _conflict(report, "hourly_rate")
    assert conflict.proposed.doc_type == "amendment"
    assert conflict.proposed.canonical == "USD 135.00"


def test_the_agreement_outranks_an_invoice_on_payment_terms(report):
    conflict = _conflict(report, "payment_terms_days")
    assert conflict.proposed.doc_type == "msa"
    assert conflict.proposed.canonical == "30"


def test_a_proposal_is_only_ever_a_proposal(report):
    """The load-bearing restraint. Nothing in reconciliation writes a resolved
    value anywhere; the conflict stays open until a person acts."""
    for conflict in report.conflicts:
        assert len(conflict.members) > 1, "a resolved conflict would have collapsed"
        assert len(conflict.distinct_values) > 1
        assert conflict.rationale, "a proposal without reasoning is just an assertion"


def test_equal_authority_disagreeing_yields_no_proposal(cfg, report):
    """Two documents of the same rank stating different values. An arbitrary
    pick dressed as a recommendation is worse than admitting it is unsettled."""
    invoices = [m for m in _conflict(report, "hourly_rate").members if m.doc_type == "invoice"]
    result = reconcile(cfg, invoices)
    conflict = next(c for c in result.conflicts if c.field == "hourly_rate")
    assert conflict.proposed is None
    assert "do not settle this" in conflict.rationale


def test_a_single_source_is_not_a_conflict(cfg, report):
    facts = [m for m in report.facts if m.document == "sow_014.html"]
    assert reconcile(cfg, facts).conflicts == []


def test_a_clean_pile_produces_no_conflicts(cfg, report):
    """The honest-zero case. Facts that all agree must produce silence."""
    agreeing = [m for m in report.facts if m.field == "counterparty"]
    result = reconcile(cfg, agreeing)
    assert result.conflicts == []


def test_conflict_volume_escalates_the_whole_run(cfg, report):
    tiny = dict(cfg.reconciliation, escalate_above=1)
    narrowed = type(cfg)(**{**cfg.__dict__, "reconciliation": tiny})
    result = reconcile(narrowed, report.facts)
    assert result.escalate is True
    assert result.path == "escalate_volume"
    assert "exceeds the configured ceiling" in result.note


# -------------------------------------------------------------- compose --


def test_every_rendered_value_is_cited_or_explicitly_a_gap(report):
    """There is no third state. A blank cell would imply "nothing to say here"
    when the truth is "we could not establish this"."""
    for section in report.register.sections:
        for line in section.body.splitlines():
            if not line.startswith("|") or line.startswith("|-"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if cells[0] in ("Term", "Field", "Type"):
                continue
            if len(cells) == 4 and cells[1] != NOT_ESTABLISHED and section.key != "gaps":
                assert cells[3] not in ("", "—") or cells[1] == NOT_ESTABLISHED, line


def test_the_register_displays_readable_values_not_comparison_keys(report):
    """Text values casefold for grouping. Rendering the casefolded form would
    put "acme fabrication services llc" in a document a person reads."""
    body = report.register.section("parties").body
    assert "Acme Fabrication Services LLC" in body
    assert "acme fabrication services llc" not in body


def test_conflicted_fields_are_marked_in_their_section(report):
    assert "hourly_rate ⚠" in report.register.section("commercials").body


def test_the_disagreements_section_says_nothing_is_resolved(report):
    body = report.register.section("disagreements").body
    assert "**Suggested:**" in body
    assert "No value has been resolved or applied" in body
    assert "Still open" in body


def test_composition_is_deterministic(cfg, report):
    """Load-bearing for the incremental update: identical facts must render
    identical bytes, or every section would look changed on every run."""
    again = compose(cfg, report.facts, report.conflicts, report.gap_pairs)
    assert again.hashes == report.register.hashes
    assert again.render() == report.register.render()


def test_changing_one_fact_changes_only_its_section_hash(cfg, report):
    """The property the whole "an update should cost like an update" claim is
    built on. Proven by hash, not asserted."""
    edited = []
    for sourced in report.facts:
        if sourced.field == "liability_cap" and sourced.doc_type == "msa":
            bumped = type(sourced.fact.normalised)(
                raw="USD 999,999",
                canonical="USD 999999.00",
                value_type="money",
                number=sourced.fact.normalised.number,
                currency="USD",
            )
            edited.append(
                SourcedFact(
                    sourced.document,
                    sourced.doc_type,
                    sourced.entity_key,
                    type(sourced.fact)(**{**sourced.fact.__dict__, "normalised": bumped}),
                )
            )
        else:
            edited.append(sourced)

    after = compose(cfg, edited, reconcile(cfg, edited).conflicts, report.gap_pairs)
    changed = {k for k, v in after.hashes.items() if report.register.hashes[k] != v}
    assert "risk" in changed, "the section holding liability_cap must change"
    assert "parties" not in changed and "billing" not in changed


def test_a_pile_with_no_conflicts_says_so_honestly(cfg, report):
    """The rarest output in this industry, and it has to be sayable."""
    clean = [m for m in report.facts if m.field == "counterparty"]
    register = compose(cfg, clean, [], [])
    assert "No disagreements found" in register.section("disagreements").body


def test_fields_no_document_states_appear_in_the_gaps_section(cfg, report):
    body = report.register.section("gaps").body
    assert "not stated in any document" in body or "Nothing was left unestablished" in body


def test_the_run_reports_what_it_cost_by_stage(report):
    """Behaviour 10, falling out of the same record that makes stages watchable."""
    costs = report.cost_by_stage()
    assert set(costs) == {
        "ingest",
        "classify",
        "extract",
        "resolve_entity",
        "reconcile",
        "compose",
        "examine",
        "delta",
        "gate",
    }
    assert costs["classify"]["calls"] == 7
    assert costs["extract"]["tokens_in"] > 0
    # Only the two stages that talk to a model may report a cost. If a stage
    # that does pure CPU ever shows tokens, something is calling a model
    # somewhere it was not meant to and the cost report has stopped meaning
    # what it says.
    spending = {stage for stage, row in costs.items() if row["tokens_in"]}
    assert spending == {"classify", "extract"}


def test_the_run_records_which_path_each_stage_took(report):
    """Behaviour 1: not merely that a stage ran, but which branch it chose."""
    paths = report.paths_taken()
    assert paths["classify:classified"] == 7
    assert paths["extract:extracted"] == 7
    assert paths["reconcile:reconciled"] == 1


# ------------------------------------------- equal authority, later wins --


def test_a_later_amendment_supersedes_an_earlier_one(cfg, report):
    """Two amendments both outrank the agreement they amend, so doc_type alone
    cannot separate them. The documents say when they take effect, and the later
    one governs -- without this the system gives up exactly where a reviewer
    most needs an answer."""

    from app.domain.models import SourcedFact
    from app.stages.reconcile import reconcile

    earlier = next(
        m for m in report.facts if m.field == "hourly_rate" and m.doc_type == "amendment"
    )
    assert earlier.canonical == "USD 135.00"
    later = SourcedFact(
        "amendment_02.md",
        "amendment",
        earlier.entity_key,
        _valued(cfg, earlier, "hourly_rate", "USD 145", "money"),
    )
    dates = [
        SourcedFact(
            "amendment_01.md",
            "amendment",
            earlier.entity_key,
            _valued(cfg, earlier, "effective_date", "1 June 2026", "date"),
        ),
        SourcedFact(
            "amendment_02.md",
            "amendment",
            earlier.entity_key,
            _valued(cfg, earlier, "effective_date", "1 December 2026", "date"),
        ),
    ]
    conflict = next(
        c for c in reconcile(cfg, [earlier, later, *dates]).conflicts if c.field == "hourly_rate"
    )
    assert conflict.proposed is not None
    assert conflict.proposed.document == "amendment_02.md"
    assert "supersedes the earlier" in conflict.rationale


def test_equal_authority_with_no_effective_date_still_refuses_to_guess(cfg, report):
    """The tiebreak only applies when the documents actually settle it."""
    from app.stages.reconcile import reconcile

    a = next(
        m
        for m in report.facts
        if m.field == "hourly_rate" and m.doc_type == "invoice" and m.canonical == "USD 120.00"
    )
    b = next(
        m
        for m in report.facts
        if m.field == "hourly_rate" and m.doc_type == "invoice" and m.canonical == "USD 135.00"
    )
    conflict = next(c for c in reconcile(cfg, [a, b]).conflicts if c.field == "hourly_rate")
    assert conflict.proposed is None
    assert "does not state an effective date" in conflict.rationale


def _valued(cfg, template, field_name, text, value_type):
    """A fact carrying `text`, reusing a real span so provenance stays intact."""
    from app.domain.normalize import normalise
    from app.stages.extract import ExtractedFact

    return ExtractedFact(
        field_name=field_name,
        value_raw=text,
        quote=template.fact.quote,
        span=template.fact.span,
        normalised=normalise(value_type, text, cfg.normalization),
        value_type=value_type,
        unit=None,
        confidence=1.0,
    )
