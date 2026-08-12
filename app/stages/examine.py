"""The second movement: checking the pile against a playbook.

You hand the system a contract playbook -- a YAML checklist of the rules you
care about -- and it checks the pile against them and produces findings, each
pointing at where it came from.

Three decisions shape this stage.

**A rule has three outcomes, not two.** `violated`, `satisfied`, and
`not_enough_evidence`. The third is the one that makes the other two worth
reading. A rule the pile could not answer is not a rule the pile passed, and
collapsing them is how a report comes to say a contract is clean when what
happened is that nobody could tell. Every "satisfied" here means the facts were
present and they were checked.

**Checks are arithmetic, not opinion.** No model is called. "Invoice 1043 billed
40 hours against a cap of 35" is subtraction, and asking a model to do it would
make a citable, always-correct finding into an occasionally-wrong one that has
to be trusted. It also costs nothing, which matters for a stage that runs on
every arrival. The place a model would earn its keep is a rule that needs
reading rather than counting -- that is the `judged` kind this vocabulary does
not yet have, and it is named here so its absence is a decision rather than an
oversight.

**A finding cannot exist without its evidence.** Same rule as facts: a violation
carries the facts that establish it, and there is no path that produces an
uncited one. `_finding` refuses to build a violation with no citations, so the
failure is a crash in a test rather than an unsupported claim in a deliverable.

## Configuration over code, and exactly where that stops

A rule is a block in `rules/playbook.yaml` naming one of the check kinds below
and the fields it applies to. Adding a rule of a shape that already exists is a
YAML change and nothing else. Adding a genuinely new *kind* of arithmetic is
Python, in this file. That boundary is real and worth stating plainly rather
than claiming the stronger version: six kinds cover the eight rules in the
playbook, and the seventh rule anyone writes will probably fit one of them.

    field_present           the engagement must state this field at all
    matches_governing       lower-authority documents must agree with the
                            document that actually governs
    sum_within_limit        a total across documents must not exceed a cap
    at_least                a value must meet a minimum stated elsewhere
    date_within             a date must fall inside a term
    no_quarantined_sources  no source may contain instructions aimed at us
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from decimal import Decimal
from typing import Any

from app.domain.config import DomainConfig, RuleSpec
from app.domain.models import SourcedFact
from app.domain.normalize import values_agree

VIOLATED = "violated"
SATISFIED = "satisfied"
UNJUDGED = "not_enough_evidence"


@dataclass
class Finding:
    rule_key: str
    severity: str
    outcome: str
    entity_key: str
    statement: str  # the rule, as the playbook states it
    detail: str  # what was actually found, with the numbers in it
    citations: list[SourcedFact] = dc_field(default_factory=list)

    @property
    def is_violation(self) -> bool:
        return self.outcome == VIOLATED

    @property
    def cited_documents(self) -> list[str]:
        seen: list[str] = []
        for fact in self.citations:
            if fact.document not in seen:
                seen.append(fact.document)
        return seen


@dataclass
class ExamineResult:
    findings: list[Finding] = dc_field(default_factory=list)

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == VIOLATED]

    @property
    def unjudged(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == UNJUDGED]

    @property
    def satisfied(self) -> list[Finding]:
        return [f for f in self.findings if f.outcome == SATISFIED]

    @property
    def path(self) -> str:
        return "violations_found" if self.violations else "clean"

    def summary(self) -> str:
        """What the run says about the playbook, including when it is silent.

        A clean pile has to be sayable, and it has to be distinguishable from a
        pile nobody could check.
        """
        if self.violations:
            return (
                f"{len(self.violations)} of {len(self.findings)} rules "
                f"violated; {len(self.unjudged)} could not be judged"
            )
        if self.unjudged and not self.satisfied:
            return (
                f"no rule could be judged: none of the {len(self.unjudged)} "
                f"rules had the facts it needs"
            )
        return (
            f"no violations. {len(self.satisfied)} rules checked and "
            f"satisfied, {len(self.unjudged)} could not be judged"
        )


def examine(
    cfg: DomainConfig,
    facts: Iterable[SourcedFact],
    documents: Iterable[dict[str, Any]] | None = None,
) -> ExamineResult:
    """Run every rule in the playbook over the pile, in order.

    Rules are evaluated per engagement, because a pile can hold more than one
    and a rule about "the governing agreement" means a particular one.
    """
    facts = list(facts)
    documents = list(documents or [])
    result = ExamineResult()

    entities = sorted({f.entity_key for f in facts})
    for key in sorted(cfg.rules):
        rule = cfg.rules[key]
        kind = (rule.check or {}).get("kind")
        if kind is None:
            result.findings.append(
                _unjudged(
                    rule, "-", f"rule {rule.key!r} declares no check, so nothing " f"evaluates it"
                )
            )
            continue
        evaluator = _KINDS.get(kind)
        if evaluator is None:
            # A typo in a check kind would otherwise make a rule silently stop
            # being enforced, which is worse than a rule that fails loudly.
            result.findings.append(
                _unjudged(
                    rule,
                    "-",
                    f"unknown check kind {kind!r}; known kinds are " f"{', '.join(sorted(_KINDS))}",
                )
            )
            continue

        if kind == "no_quarantined_sources":
            result.findings.append(evaluator(cfg, rule, facts, documents, "-"))
            continue

        if not entities:
            result.findings.append(
                _unjudged(rule, "-", "the pile holds no facts, so no rule can be judged")
            )
            continue
        for entity_key in entities:
            scoped = [f for f in facts if f.entity_key == entity_key]
            result.findings.append(evaluator(cfg, rule, scoped, documents, entity_key))

    return result


# ---------------------------------------------------------------- the kinds --


def _field_present(
    cfg: DomainConfig,
    rule: RuleSpec,
    facts: list[SourcedFact],
    documents: list[dict],
    entity_key: str,
) -> Finding:
    """The engagement must state this field somewhere."""
    name = rule.check["field"]

    if name not in _declared_fields(cfg):
        # Not the documents' fault. No extraction schema asks for this field, so
        # the pile could not state it even if every contract did. Reporting that
        # as a violation would blame the sources for a gap in the configuration.
        return _unjudged(
            rule,
            entity_key,
            f"no extraction schema declares {name!r}, so nothing in "
            f"the pile can answer this rule",
        )

    stated = [f for f in facts if f.field == name]
    if not stated:
        return _violation(
            rule, entity_key, f"no document in the engagement states {name}", _authority(cfg, facts)
        )
    best = max(stated, key=lambda f: cfg.precedence_of(f.doc_type))
    return _satisfied(
        rule, entity_key, f"{name} is stated as {best.display} in {best.document}", [best]
    )


def _matches_governing(
    cfg: DomainConfig,
    rule: RuleSpec,
    facts: list[SourcedFact],
    documents: list[dict],
    entity_key: str,
) -> Finding:
    """Lower-authority documents must state what the governing one states.

    This is the shape of most playbook rules that matter commercially: an
    invoice bills what the amended agreement says, not what the original did.
    It is deliberately asymmetric -- an amendment changing the rate is the
    contract working, an invoice disagreeing with it is the error.

    `must_match` names the document types that are actually obliged to comply,
    and leaving it out is almost always wrong. Without it the first version of
    this check reported the MSA as violating the amendment that amended it,
    which is not a finding -- it is the definition of an amendment. A superseded
    document stating the old value is history; only the documents that had to
    follow the new one can breach it.
    """
    name = rule.check["field"]
    must_match = set(rule.check.get("must_match") or ())
    stated = [f for f in facts if f.field == name and f.fact.normalised]
    if len(stated) < 2:
        return _unjudged(
            rule,
            entity_key,
            f"{len(stated)} document(s) state {name}; at least two "
            f"are needed to check one against the other",
        )

    top_rank = max(cfg.precedence_of(f.doc_type) for f in stated)
    governing = [f for f in stated if cfg.precedence_of(f.doc_type) == top_rank]
    if len({f.canonical for f in governing}) > 1:
        # Two documents of equal authority disagreeing is a conflict for the
        # gate, not a rule violation. Naming one of them the governing value
        # here would silently resolve exactly what must not be resolved.
        return _unjudged(
            rule,
            entity_key,
            f"{len(governing)} documents of equal authority state "
            f"different values for {name}; which one governs is "
            f"unsettled and is raised as a conflict, not a finding",
        )

    authority = governing[0]
    bound = [
        f
        for f in stated
        if cfg.precedence_of(f.doc_type) < top_rank and (not must_match or f.doc_type in must_match)
    ]
    if not bound:
        return _unjudged(
            rule,
            entity_key,
            f"only {authority.doc_type} documents state {name}; "
            f"nothing that has to follow it does, so there is "
            f"nothing to check",
        )

    offenders = [
        f
        for f in bound
        if not values_agree(authority.fact.normalised, f.fact.normalised, cfg.reconciliation)
    ]
    if not offenders:
        return _satisfied(
            rule,
            entity_key,
            f"every document that must follow {name} agrees with "
            f"{authority.doc_type} {authority.document} "
            f"({authority.display})",
            [authority, *bound],
        )

    listed = ", ".join(f"{f.document} states {f.display}" for f in offenders)
    return _violation(
        rule,
        entity_key,
        f"{authority.doc_type} {authority.document} governs "
        f"{name} at {authority.display}, but {listed}",
        [authority, *offenders],
    )


def _sum_within_limit(
    cfg: DomainConfig,
    rule: RuleSpec,
    facts: list[SourcedFact],
    documents: list[dict],
    entity_key: str,
) -> Finding:
    """A total across documents must not exceed a cap stated in another."""
    sum_field, limit_field = rule.check["sum_of"], rule.check["limit"]
    parts = [f for f in facts if f.field == sum_field and _number(f) is not None]
    limits = [f for f in facts if f.field == limit_field and _number(f) is not None]

    if not parts:
        return _unjudged(rule, entity_key, f"no document states {sum_field}")
    if not limits:
        return _unjudged(
            rule,
            entity_key,
            f"{len(parts)} document(s) state {sum_field} but none "
            f"states {limit_field}, so there is nothing to check "
            f"them against",
        )

    cap = min(limits, key=lambda f: _number(f))  # the tightest cap binds
    total = sum(_number(f) for f in parts)
    limit = _number(cap)
    if total <= limit:
        return _satisfied(
            rule,
            entity_key,
            f"{sum_field} totals {_plain(total)} across "
            f"{len(parts)} document(s), within the "
            f"{_plain(limit)} authorised by {cap.document}",
            [cap, *parts],
        )
    return _violation(
        rule,
        entity_key,
        f"{sum_field} totals {_plain(total)} across "
        f"{len(parts)} document(s), exceeding the {_plain(limit)} "
        f"authorised by {cap.document} by {_plain(total - limit)}",
        [cap, *parts],
    )


def _at_least(
    cfg: DomainConfig,
    rule: RuleSpec,
    facts: list[SourcedFact],
    documents: list[dict],
    entity_key: str,
) -> Finding:
    """A value must meet a minimum that another document sets."""
    name, minimum_field = rule.check["field"], rule.check["minimum"]
    values = [f for f in facts if f.field == name and _number(f) is not None]
    minimums = [f for f in facts if f.field == minimum_field and _number(f) is not None]

    if not values:
        return _unjudged(rule, entity_key, f"no document states {name}")
    if not minimums:
        return _unjudged(
            rule,
            entity_key,
            f"no document states {minimum_field}, so there is no " f"minimum to hold {name} to",
        )

    required = max(minimums, key=lambda f: cfg.precedence_of(f.doc_type))
    floor = _number(required)
    short = [f for f in values if _number(f) < floor]
    if not short:
        return _satisfied(
            rule,
            entity_key,
            f"{name} meets the {_plain(floor)} required by " f"{required.document}",
            [required, *values],
        )
    listed = ", ".join(f"{f.document} gives {f.display}" for f in short)
    return _violation(
        rule,
        entity_key,
        f"{required.document} requires {minimum_field} of at least "
        f"{_plain(floor)}, but {listed}",
        [required, *short],
    )


def _date_within(
    cfg: DomainConfig,
    rule: RuleSpec,
    facts: list[SourcedFact],
    documents: list[dict],
    entity_key: str,
) -> Finding:
    """A date must fall inside a term that starts somewhere and runs a length."""
    name, start_field = rule.check["value"], rule.check["start"]
    months_field = rule.check.get("plus_months")

    dates = [f for f in facts if f.field == name and _as_date(f)]
    starts = [f for f in facts if f.field == start_field and _as_date(f)]
    if not dates:
        return _unjudged(rule, entity_key, f"no document states {name}")
    if not starts:
        return _unjudged(
            rule,
            entity_key,
            f"no document states {start_field}, so the term has no " f"beginning to measure from",
        )

    start_fact = min(starts, key=lambda f: _as_date(f))
    begins = _as_date(start_fact)
    ends: date | None = None
    end_fact = start_fact
    if months_field:
        months = [f for f in facts if f.field == months_field and _number(f) is not None]
        if not months:
            return _unjudged(
                rule,
                entity_key,
                f"no document states {months_field}, so the term "
                f"has no length and no end to check against",
            )
        end_fact = max(months, key=lambda f: cfg.precedence_of(f.doc_type))
        ends = _add_months(begins, int(_number(end_fact)))

    outside = [
        f for f in dates if _as_date(f) < begins or (ends is not None and _as_date(f) > ends)
    ]
    window = f"{begins.isoformat()}" + (f" to {ends.isoformat()}" if ends else " onwards")
    if not outside:
        return _satisfied(
            rule,
            entity_key,
            f"every {name} falls within the term ({window})",
            [start_fact, end_fact, *dates],
        )
    listed = ", ".join(f"{f.document} dated {_as_date(f).isoformat()}" for f in outside)
    return _violation(
        rule, entity_key, f"the term runs {window}, but {listed}", [start_fact, end_fact, *outside]
    )


def _no_quarantined_sources(
    cfg: DomainConfig,
    rule: RuleSpec,
    facts: list[SourcedFact],
    documents: list[dict],
    entity_key: str,
) -> Finding:
    """Text inside a document that addresses the system is reported, never
    followed.

    This is where behaviour 8 stops being a branch in the pipeline and becomes
    something a reviewer reads. Quarantine already kept the document out of
    every input; the playbook is what turns that into a stated finding with the
    offending text quoted back.
    """
    quarantined = [d for d in documents if d.get("status") == "quarantined"]
    if not documents:
        return _unjudged(rule, entity_key, "the pile's documents were not available to this check")
    if not quarantined:
        return _satisfied(
            rule,
            entity_key,
            f"none of the {len(documents)} sources contains text " f"directing the system to act",
            [],
        )
    listed = "; ".join(
        f"{d['filename']}: {(d.get('ingest_note') or 'no note')[:160]}" for d in quarantined
    )
    # No fact citations, and that is correct rather than a gap: a quarantined
    # document never produced a fact, on purpose. The document itself is the
    # evidence, and the finding names it.
    return Finding(
        rule_key=rule.key,
        severity=rule.severity,
        outcome=VIOLATED,
        entity_key=entity_key,
        statement=rule.statement,
        detail=(
            f"{len(quarantined)} source(s) contain text addressed "
            f"at the analysing system. It was quarantined and "
            f"never used as an input. {listed}"
        ),
    )


_KINDS = {
    "field_present": _field_present,
    "matches_governing": _matches_governing,
    "sum_within_limit": _sum_within_limit,
    "at_least": _at_least,
    "date_within": _date_within,
    "no_quarantined_sources": _no_quarantined_sources,
}

# What each kind needs from the playbook, checked when the domain loads. A rule
# missing a parameter would otherwise fail on the run that needed it, which is
# both later and further from the person who wrote the typo.
KNOWN_CHECKS: dict[str, tuple[str, ...]] = {
    "field_present": ("field",),
    "matches_governing": ("field",),
    "sum_within_limit": ("sum_of", "limit"),
    "at_least": ("field", "minimum"),
    "date_within": ("value", "start"),
    "no_quarantined_sources": (),
}


# --------------------------------------------------------------- plumbing --


def _violation(
    rule: RuleSpec, entity_key: str, detail: str, citations: list[SourcedFact]
) -> Finding:
    citations = _dedupe(citations)
    if not citations and rule.check.get("kind") != "no_quarantined_sources":
        # There is no path that produces an uncited claim. A violation without
        # evidence is the exact thing this system is built not to emit, so it
        # fails here rather than reaching a deliverable.
        raise AssertionError(f"rule {rule.key!r} produced a violation with no citations")
    return Finding(rule.key, rule.severity, VIOLATED, entity_key, rule.statement, detail, citations)


def _satisfied(
    rule: RuleSpec, entity_key: str, detail: str, citations: list[SourcedFact]
) -> Finding:
    return Finding(
        rule.key, rule.severity, SATISFIED, entity_key, rule.statement, detail, _dedupe(citations)
    )


def _unjudged(rule: RuleSpec, entity_key: str, detail: str) -> Finding:
    """Could not be checked, and says why.

    Never carries citations, because there is nothing to cite -- that is what
    "not enough evidence" means.
    """
    return Finding(rule.key, rule.severity, UNJUDGED, entity_key, rule.statement, detail, [])


def _dedupe(facts: list[SourcedFact]) -> list[SourcedFact]:
    seen: list[SourcedFact] = []
    for fact in facts:
        if not any(f is fact or f.citation == fact.citation for f in seen):
            seen.append(fact)
    return seen


def _declared_fields(cfg: DomainConfig) -> set[str]:
    return {name for schema in cfg.extraction.values() for name in schema.get("fields", {})}


def _authority(cfg: DomainConfig, facts: list[SourcedFact]) -> list[SourcedFact]:
    """One fact from the highest-authority document, so an absence still points
    at the document that should have said it."""
    if not facts:
        return []
    return [max(facts, key=lambda f: (cfg.precedence_of(f.doc_type), f.document))]


def _number(fact: SourcedFact) -> Decimal | None:
    normalised = fact.fact.normalised
    return normalised.number if normalised else None


def _as_date(fact: SourcedFact) -> date | None:
    normalised = fact.fact.normalised
    return normalised.as_date if normalised else None


def _plain(value: Decimal) -> str:
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    # Clamp rather than overflow: a term starting on the 31st ends on the last
    # day of the month, not on the 1st of the next one.
    day = min(
        start.day,
        [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1],
    )
    return date(year, month, day)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
