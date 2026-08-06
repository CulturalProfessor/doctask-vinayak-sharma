"""Ingest, understand, persist, halt at the gate.

This is the full first movement against a real database. It deliberately stops
before committing anything: the register exists in memory and as proposals, and
a person decides what becomes the deliverable.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psycopg

from app.domain.config import DomainConfig
from app.graph.understand import RunReport, understand_pile
from app.llm.base import Provider
from app.stages.compose import Register
from app.stages.gate import GateState, open_gate
from app.ingest.ingest import IngestResult, ingest_path
from app.store import repository as repo


@dataclass
class PipelineResult:
    run_id: str
    report: RunReport
    gate: GateState
    ingested: list[IngestResult]

    @property
    def register(self) -> Register:
        return self.report.register


def run_understand(conn: psycopg.Connection, provider: Provider, cfg: DomainConfig,
                   pile_id: str, paths: Iterable[Path]) -> PipelineResult:
    run_id = repo.create_run(conn, pile_id, kind="full")

    ingested: list[IngestResult] = []
    by_filename: dict[str, str] = {}
    for path in sorted(paths):
        result = ingest_path(conn, pile_id, path)
        ingested.append(result)
        if result.document_id:
            by_filename[result.filename] = result.document_id
        repo.record_stage_event(
            conn, run_id, "ingest",
            "duplicate" if result.duplicate else result.status,
            document_id=result.document_id,
            detail={"format": result.format, "note": result.note},
        )

    report = understand_pile(provider, cfg, paths)

    for event in report.events:
        repo.record_stage_event(
            conn, run_id, event.stage, event.path_taken,
            document_id=by_filename.get(event.document or ""),
            ms=event.ms, tokens_in=event.usage.tokens_in,
            tokens_out=event.usage.tokens_out, cost_usd=event.usage.cost_usd,
            model=event.usage.model, detail={"note": event.detail} if event.detail else None,
        )

    facts_by_document: dict[str, list] = {}
    for sourced in report.facts:
        facts_by_document.setdefault(sourced.document, []).append(sourced)
    for filename, facts in facts_by_document.items():
        document_id = by_filename.get(filename)
        if document_id:
            repo.persist_facts(conn, pile_id, run_id, document_id, facts)

    for filename, note in report.quarantined:
        document_id = by_filename.get(filename)
        if document_id:
            # The document stays in the pile as evidence. It simply never
            # becomes an input to anything.
            repo.execute_quarantine(conn, document_id, note)

    conflicts = report.reconciliation.conflicts if report.reconciliation else []
    conflict_ids = repo.persist_conflicts(conn, pile_id, conflicts)

    gate = open_gate(conn, pile_id, run_id, report.register, conflicts, conflict_ids)
    return PipelineResult(run_id=run_id, report=report, gate=gate, ingested=ingested)
