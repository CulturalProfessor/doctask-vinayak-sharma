"""A new document arrives.

The brief is specific about what this must not be: not a rewrite, and not a
full re-run that happens to reproduce the same bytes. An update should cost like
an update, and the system should be able to *prove* that the parts the new
source did not affect stayed exactly as they were.

Two different economies are at work, and conflating them is how this gets built
wrong:

  **Model work is genuinely incremental.** Only the arriving document is
  classified and extracted. The other six are never sent anywhere. That is where
  the cost of an update actually lives.

  **Composition is recomputed in full and then diffed.** It is pure CPU over
  facts already held, and recomputing it costs nothing worth optimising. Doing
  it this way makes the unchanged sections *provably* unchanged -- their hashes
  are recomputed from scratch and still match -- rather than unchanged because
  we predicted they would be and skipped them. A predicted no-op is an
  assumption; a recomputed identical hash is evidence.

Only sections whose hash actually moved become proposals, so a reviewer sees the
update, not the document.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path

import psycopg

from app.domain.config import DomainConfig
from app.domain.models import SourcedFact
from app.ingest.formats import detect_format, extract_pages
from app.ingest.ingest import IngestResult, ingest_path
from app.llm.base import Provider
from app.stages.classify import classify_document
from app.stages.compose import Register, compose
from app.stages.entities import resolve_entity
from app.stages.extract import Gap, extract_document
from app.stages.gate import GateState
from app.stages.reconcile import Conflict, reconcile
from app.store import repository as repo


@dataclass
class Delta:
    changed: list[str] = dc_field(default_factory=list)
    unchanged: list[str] = dc_field(default_factory=list)
    added: list[str] = dc_field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.changed and not self.added


@dataclass
class IncrementalResult:
    run_id: str
    ingest: IngestResult
    register: Register | None = None
    delta: Delta = dc_field(default_factory=Delta)
    new_conflicts: list[Conflict] = dc_field(default_factory=list)
    conflicts: list[Conflict] = dc_field(default_factory=list)
    gate: GateState | None = None
    path: str = "updated"
    note: str | None = None
    model_calls: int = 0


def run_incremental(conn: psycopg.Connection, provider: Provider, cfg: DomainConfig,
                    pile_id: str, path: Path) -> IncrementalResult:
    run_id = repo.create_run(conn, pile_id, kind="incremental")
    ingest = ingest_path(conn, pile_id, path)
    result = IncrementalResult(run_id=run_id, ingest=ingest)

    repo.record_stage_event(conn, run_id, "ingest",
                            "duplicate" if ingest.duplicate else ingest.status,
                            document_id=ingest.document_id,
                            detail={"filename": ingest.filename})

    if ingest.duplicate:
        # Identical bytes already in the pile. Doing nothing is the correct
        # outcome, and saying so is what makes a re-run cost nothing.
        result.path = "noop_duplicate"
        result.note = "identical bytes already ingested; nothing to update"
        repo.set_run_status(conn, run_id, "committed")
        return result
    if ingest.status != "ingested":
        result.path = f"noop_{ingest.status}"
        result.note = ingest.note
        repo.set_run_status(conn, run_id, "committed")
        return result

    data = path.read_bytes()
    text = extract_pages(data, detect_format(path, data))[0].text

    classification = classify_document(provider, cfg, path.name, text)
    result.model_calls += 1
    repo.record_stage_event(conn, run_id, "classify", classification.path,
                            document_id=ingest.document_id,
                            tokens_in=classification.usage.tokens_in,
                            tokens_out=classification.usage.tokens_out,
                            cost_usd=classification.usage.cost_usd,
                            detail={"note": classification.note})

    if classification.quarantine:
        repo.execute_quarantine(conn, ingest.document_id, classification.note or "")
        result.path = "quarantined"
        result.note = classification.note
        repo.set_run_status(conn, run_id, "awaiting_approval")
        return result
    if classification.escalate:
        repo.set_document_type(conn, ingest.document_id, classification.doc_type,
                               classification.confidence, status="escalated")
        result.path = "escalated"
        result.note = classification.note
        repo.set_run_status(conn, run_id, "awaiting_approval")
        return result

    repo.set_document_type(conn, ingest.document_id, classification.doc_type,
                           classification.confidence)

    extraction = extract_document(provider, cfg, classification.doc_type, path.name, text)
    result.model_calls += 1
    repo.record_stage_event(conn, run_id, "extract", extraction.path,
                            document_id=ingest.document_id,
                            tokens_in=extraction.usage.tokens_in,
                            tokens_out=extraction.usage.tokens_out,
                            cost_usd=extraction.usage.cost_usd,
                            detail={"facts": len(extraction.facts),
                                    "gaps": len(extraction.gaps)})

    # Identity is resolved against the engagements the pile already holds, not
    # taken from the model's phrasing. See app/stages/entities.py.
    known_keys = repo.known_entity_keys(conn, pile_id)
    schema = cfg.extraction[cfg.doc_types[classification.doc_type].extraction]
    resolution = resolve_entity(
        extraction.counterparty or "", known_keys,
        schema.get("entity_key", "engagement:{counterparty_slug}"),
        cfg.reconciliation.get("entity"),
    )
    repo.record_stage_event(conn, run_id, "resolve_entity", resolution.method,
                            document_id=ingest.document_id,
                            detail={"entity_key": resolution.entity_key,
                                    "note": resolution.note})

    if resolution.escalate:
        # Merging two engagements corrupts the register; splitting one hides
        # every conflict. Neither is ours to choose silently.
        repo.set_document_type(conn, ingest.document_id, classification.doc_type,
                               classification.confidence, status="escalated")
        result.path = "escalated_ambiguous_entity"
        result.note = resolution.note
        repo.set_run_status(conn, run_id, "awaiting_approval")
        return result

    arriving: list[SourcedFact] = []
    if resolution.entity_key:
        arriving = [
            SourcedFact(document=path.name, doc_type=classification.doc_type,
                        entity_key=resolution.entity_key, fact=fact)
            for fact in extraction.facts
        ]
        repo.persist_facts(conn, pile_id, run_id, ingest.document_id, arriving)

    # Everything already known, plus what just arrived.
    existing = repo.load_sourced_facts(conn, cfg, pile_id,
                                       exclude_document_id=ingest.document_id)
    all_facts = existing + arriving

    previous_hashes = {row["section_key"]: row["content_hash"]
                       for row in repo.sections_for_version(conn, pile_id)}

    reconciled = reconcile(cfg, all_facts)
    result.conflicts = reconciled.conflicts
    repo.record_stage_event(conn, run_id, "reconcile", reconciled.path,
                            detail={"conflicts": len(reconciled.conflicts)})

    # A conflict this document introduced, rather than one that already existed.
    # This is the "the new source contradicts what the deliverable already says"
    # case, and it is surfaced rather than resolved.
    touched = {fact.field for fact in arriving}
    previous_conflicts = {
        (row["entity_key"], row["field"])
        for row in repo.open_conflict_keys(conn, pile_id)
    }
    result.new_conflicts = [c for c in reconciled.conflicts
                            if c.field in touched and c.key not in previous_conflicts]

    gaps: list[tuple[str, Gap]] = [(path.name, gap) for gap in extraction.gaps]
    register = compose(cfg, all_facts, reconciled.conflicts, gaps)
    result.register = register
    repo.record_stage_event(conn, run_id, "compose", "composed",
                            detail={"sections": len(register.sections)})

    result.delta = _diff(previous_hashes, register)
    repo.record_stage_event(
        conn, run_id, "delta",
        "noop" if result.delta.is_noop else "patched",
        detail={"changed": result.delta.changed, "unchanged": result.delta.unchanged,
                "added": result.delta.added},
    )

    if result.delta.is_noop:
        result.path = "noop_no_change"
        result.note = ("the document was read and its facts stored, but no section "
                       "of the register changed")

    conflict_ids = repo.persist_conflicts(conn, pile_id, reconciled.conflicts)
    result.gate = _open_partial_gate(conn, pile_id, run_id, register, result.delta,
                                     result.new_conflicts, conflict_ids,
                                     ingest.document_id)
    return result


def _diff(previous: dict[str, str], register: Register) -> Delta:
    delta = Delta()
    for section in register.sections:
        if section.key not in previous:
            delta.added.append(section.key)
        elif previous[section.key] != section.content_hash:
            delta.changed.append(section.key)
        else:
            delta.unchanged.append(section.key)
    return delta


def _open_partial_gate(conn: psycopg.Connection, pile_id: str, run_id: str,
                       register: Register, delta: Delta,
                       new_conflicts: list[Conflict],
                       conflict_ids: dict[tuple[str, str], str],
                       cause_document_id: str) -> GateState:
    """Propose only what actually changed.

    A reviewer of an update should see the update. Re-proposing six unchanged
    sections would bury the one that moved.
    """
    for conflict in new_conflicts:
        proposed = conflict.proposed
        repo.create_proposal(
            conn, pile_id, run_id, kind="conflict",
            ref_id=conflict_ids.get(conflict.key),
            summary=(f"new: {conflict.field} — the arriving document contradicts "
                     f"what the register already says"
                     + (f"; propose {proposed.display} from {proposed.document}"
                        if proposed else "; no proposal")),
            payload={
                "field": conflict.field, "entity_key": conflict.entity_key,
                "values": conflict.distinct_values,
                "proposed": proposed.canonical if proposed else None,
                "rationale": conflict.rationale,
                "members": [{"document": m.document, "doc_type": m.doc_type,
                             "value": m.canonical, "quote": m.fact.span.text,
                             "char_start": m.fact.span.char_start,
                             "char_end": m.fact.span.char_end}
                            for m in conflict.members],
            },
        )

    for key in delta.changed + delta.added:
        section = register.section(key)
        repo.create_proposal(
            conn, pile_id, run_id, kind="section_patch",
            summary=f"{key}: {'new section' if key in delta.added else 'updated'}",
            payload={"section_key": key, "content_hash": section.content_hash,
                     "cause_document_id": cause_document_id, "body": section.body},
        )

    repo.set_run_status(conn, run_id, "awaiting_approval")
    return GateState(run_id, repo.list_proposals(conn, run_id))
