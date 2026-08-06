"""Finding where the documents disagree.

Facts sharing an entity and a field form a group. A group holding more than one
distinct value is a conflict.

The rule this stage is built around: **a conflict is surfaced, never resolved.**
Precedence is real -- an amendment supersedes the agreement it amends -- and the
stage uses it to attach a *proposed* resolution with its reasoning. The proposal
is a suggestion for a reviewer, not a decision. Nothing here writes a resolved
value anywhere, and the conflict stays open until a person acts on it.

That distinction is the difference between a system that helps and one that
quietly decides. Auto-resolving by precedence would look identical on the clean
case and be invisibly wrong on the interesting one.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Iterable

from app.domain.config import DomainConfig
from app.domain.models import SourcedFact
from app.domain.normalize import values_agree


@dataclass
class Conflict:
    entity_key: str
    field: str
    members: list[SourcedFact]
    proposed: SourcedFact | None = None
    rationale: str = ""
    time_varying: bool = False

    @property
    def distinct_values(self) -> list[str]:
        seen: list[str] = []
        for member in self.members:
            if member.canonical not in seen:
                seen.append(member.canonical)
        return seen

    @property
    def key(self) -> tuple[str, str]:
        return (self.entity_key, self.field)


@dataclass
class ReconcileResult:
    conflicts: list[Conflict] = dc_field(default_factory=list)
    agreed: dict[tuple[str, str], SourcedFact] = dc_field(default_factory=dict)
    # Fields skipped because they describe the document instance rather than the
    # engagement. Recorded rather than silently dropped, so the run can show
    # what it chose not to compare.
    per_document: dict[str, int] = dc_field(default_factory=dict)
    escalate: bool = False
    note: str | None = None

    @property
    def path(self) -> str:
        return "escalate_volume" if self.escalate else "reconciled"


def reconcile(cfg: DomainConfig, facts: Iterable[SourcedFact]) -> ReconcileResult:
    groups: dict[tuple[str, str], list[SourcedFact]] = {}
    for sourced in facts:
        groups.setdefault((sourced.entity_key, sourced.field), []).append(sourced)

    result = ReconcileResult()
    time_varying = set(cfg.reconciliation.get("time_varying_fields", []))
    instance_fields = set(cfg.reconciliation.get("instance_fields", []))

    for (entity_key, field), members in sorted(groups.items()):
        if field in instance_fields:
            # Each document legitimately has its own value. Comparing them would
            # report three invoices as three disagreements. They still appear in
            # the register -- they are simply not conflicts.
            result.per_document[field] = len(members)
            continue

        clusters = _cluster(members, cfg)
        if len(clusters) <= 1:
            # Agreement. Keep one representative -- the highest-authority one,
            # so the register cites the governing document rather than whichever
            # file happened to sort first.
            result.agreed[(entity_key, field)] = max(
                members, key=lambda m: cfg.precedence_of(m.doc_type)
            )
            continue

        conflict = Conflict(
            entity_key=entity_key, field=field,
            members=sorted(members, key=lambda m: (-cfg.precedence_of(m.doc_type),
                                                   m.document)),
            time_varying=field in time_varying,
        )
        conflict.proposed, conflict.rationale = _propose(cfg, conflict)
        result.conflicts.append(conflict)

    ceiling = cfg.reconciliation.get("escalate_above")
    if ceiling is not None and len(result.conflicts) > ceiling:
        # Path change: past a certain volume, individual proposals stop being
        # reviewable and the run itself is what needs a human's attention.
        result.escalate = True
        result.note = (
            f"{len(result.conflicts)} conflicts exceeds the configured ceiling of "
            f"{ceiling}; escalating the run rather than flooding the gate"
        )
    return result


def _cluster(members: list[SourcedFact], cfg: DomainConfig) -> list[list[SourcedFact]]:
    """Group members whose values agree, respecting configured tolerance.

    Uses `values_agree` rather than string equality so that a rounded invoice
    total does not read as a disagreement, and so a mixed-currency group *does*.
    """
    clusters: list[list[SourcedFact]] = []
    for member in members:
        if member.fact.normalised is None:
            continue
        for cluster in clusters:
            if values_agree(cluster[0].fact.normalised, member.fact.normalised,
                            cfg.reconciliation):
                cluster.append(member)
                break
        else:
            clusters.append([member])
    return clusters


def _propose(cfg: DomainConfig, conflict: Conflict) -> tuple[SourcedFact | None, str]:
    """Suggest which member should govern, and say why.

    Returns no proposal when the ranking cannot separate the candidates. An
    arbitrary pick dressed as a recommendation is worse than an admission that
    the documents do not settle it.
    """
    ranked = sorted(conflict.members, key=lambda m: cfg.precedence_of(m.doc_type),
                    reverse=True)
    top = ranked[0]
    rank = cfg.precedence_of(top.doc_type)
    tied = [m for m in ranked if cfg.precedence_of(m.doc_type) == rank]

    if len({m.canonical for m in tied}) > 1:
        return None, (
            f"{len(tied)} documents of equal authority ({top.doc_type}) state different "
            f"values; the documents do not settle this and a person must choose"
        )

    others = ", ".join(sorted({f"{m.doc_type} says {m.canonical}"
                               for m in conflict.members if m.canonical != top.canonical}))
    rationale = (f"{top.doc_type} ({top.document}) carries the highest contractual "
                 f"authority here and states {top.canonical}; {others}")
    if conflict.time_varying:
        rationale += (". This field is expected to change over time, so the "
                      "disagreement may be historical rather than an error -- "
                      "check the effective dates before accepting")
    return top, rationale
