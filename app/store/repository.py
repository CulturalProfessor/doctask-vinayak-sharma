"""Reading and writing the run's state.

Stages stay pure functions; this module is the only place that knows about
tables. Keeping the boundary here is what lets the stages be tested without a
database and the database be exercised without a model.

One invariant runs through everything below: a fact insert names a span, and a
span insert names a document. There is no ordering of these calls that produces
a fact without provenance, because the schema will not accept one.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

import psycopg

from app.domain.models import SourcedFact
from app.stages.compose import Register
from app.stages.reconcile import Conflict
from app.store.engine import execute, fetch_all, fetch_one


# ------------------------------------------------------------------- runs --

def create_run(conn: psycopg.Connection, pile_id: str, kind: str = "full",
               trigger_document_id: str | None = None) -> str:
    row = fetch_one(conn, """
        INSERT INTO run (pile_id, kind, trigger_document_id)
        VALUES (%s, %s, %s) RETURNING id
    """, (pile_id, kind, trigger_document_id))
    return str(row["id"])


def set_run_status(conn: psycopg.Connection, run_id: str, status: str) -> None:
    execute(conn, """
        UPDATE run SET status = %s,
               ended_at = CASE WHEN %s IN ('committed', 'failed', 'cancelled')
                               THEN now() ELSE ended_at END
        WHERE id = %s
    """, (status, status, run_id))


def get_run(conn: psycopg.Connection, run_id: str) -> dict[str, Any] | None:
    return fetch_one(conn, "SELECT * FROM run WHERE id = %s", (run_id,))


def record_stage_event(conn: psycopg.Connection, run_id: str, stage: str,
                       path_taken: str, document_id: str | None = None,
                       ms: int = 0, tokens_in: int = 0, tokens_out: int = 0,
                       cost_usd: float = 0.0, model: str | None = None,
                       detail: dict | None = None) -> None:
    execute(conn, """
        INSERT INTO stage_event (run_id, stage, path_taken, document_id, ms,
                                 tokens_in, tokens_out, cost_usd, model, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (run_id, stage, path_taken, document_id, ms, tokens_in, tokens_out,
          cost_usd, model, json.dumps(detail) if detail else None))


def run_report(conn: psycopg.Connection, run_id: str) -> dict[str, Any]:
    """Behaviour 10: what the run spent and where the time went, per stage."""
    stages = fetch_all(conn, """
        SELECT stage,
               count(*)          AS calls,
               sum(ms)           AS ms,
               sum(tokens_in)    AS tokens_in,
               sum(tokens_out)   AS tokens_out,
               sum(cost_usd)     AS cost_usd,
               array_agg(DISTINCT path_taken) AS paths
        FROM stage_event WHERE run_id = %s
        GROUP BY stage ORDER BY min(created_at)
    """, (run_id,))
    totals = fetch_one(conn, """
        SELECT coalesce(sum(ms), 0)         AS ms,
               coalesce(sum(tokens_in), 0)  AS tokens_in,
               coalesce(sum(tokens_out), 0) AS tokens_out,
               coalesce(sum(cost_usd), 0)   AS cost_usd
        FROM stage_event WHERE run_id = %s
    """, (run_id,))
    return {"run": get_run(conn, run_id), "stages": stages, "totals": totals}


# ---------------------------------------------------------- facts + spans --

def persist_facts(conn: psycopg.Connection, pile_id: str, run_id: str,
                  document_id: str, facts: Iterable[SourcedFact]) -> list[str]:
    """Write spans then the facts that cite them.

    Ordered this way because `fact.span_id` is NOT NULL -- the database refuses
    the alternative, which is the point of putting the invariant in the schema
    rather than in a code review comment.
    """
    ids: list[str] = []
    for sourced in facts:
        span = sourced.fact.span
        span_row = fetch_one(conn, """
            INSERT INTO span (document_id, page_no, char_start, char_end, text)
            VALUES (%s, %s, %s, %s, %s) RETURNING id
        """, (document_id, 1, span.char_start, span.char_end, span.text))
        normalised = sourced.fact.normalised
        fact_row = fetch_one(conn, """
            INSERT INTO fact (pile_id, document_id, span_id, run_id, entity_key, field,
                              value_raw, value_norm, value_type, unit, confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (pile_id, document_id, span_row["id"], run_id, sourced.entity_key,
              sourced.field, sourced.fact.value_raw,
              normalised.canonical if normalised else None,
              sourced.fact.value_type, sourced.fact.unit, sourced.fact.confidence))
        ids.append(str(fact_row["id"]))
    return ids


def execute_quarantine(conn: psycopg.Connection, document_id: str, note: str) -> None:
    """Mark a document as carrying instructions aimed at the system.

    It stays in the pile as evidence and its text is preserved verbatim so a
    finding can cite it. It simply never becomes an input to anything.
    """
    execute(conn, """
        UPDATE document SET status = 'quarantined', ingest_note = %s WHERE id = %s
    """, (note[:2000], document_id))


def facts_for_pile(conn: psycopg.Connection, pile_id: str) -> list[dict[str, Any]]:
    return fetch_all(conn, """
        SELECT f.*, s.char_start, s.char_end, s.text AS span_text, d.filename, d.doc_type
        FROM fact f
        JOIN span s     ON s.id = f.span_id
        JOIN document d ON d.id = f.document_id
        WHERE f.pile_id = %s
        ORDER BY d.filename, f.field, s.char_start
    """, (pile_id,))


# ------------------------------------------------------------- conflicts --

def persist_conflicts(conn: psycopg.Connection, pile_id: str,
                      conflicts: list[Conflict]) -> dict[tuple[str, str], str]:
    """Record conflicts as open, with their proposal attached but not applied."""
    out: dict[tuple[str, str], str] = {}
    for conflict in conflicts:
        row = fetch_one(conn, """
            INSERT INTO conflict (pile_id, entity_key, field, proposed_rationale)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (pile_id, entity_key, field)
              DO UPDATE SET proposed_rationale = EXCLUDED.proposed_rationale
            RETURNING id
        """, (pile_id, conflict.entity_key, conflict.field, conflict.rationale))
        out[conflict.key] = str(row["id"])
    return out


# ------------------------------------------------------------- proposals --

def create_proposal(conn: psycopg.Connection, pile_id: str, run_id: str, kind: str,
                    summary: str, payload: dict, ref_id: str | None = None) -> str:
    row = fetch_one(conn, """
        INSERT INTO proposal (pile_id, run_id, kind, ref_id, summary, payload)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
    """, (pile_id, run_id, kind, ref_id, summary, json.dumps(payload)))
    return str(row["id"])


def list_proposals(conn: psycopg.Connection, run_id: str,
                   status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM proposal WHERE run_id = %s"
    params: tuple = (run_id,)
    if status:
        sql += " AND status = %s"
        params = (run_id, status)
    return fetch_all(conn, sql + " ORDER BY kind, created_at", params)


def decide_proposal(conn: psycopg.Connection, proposal_id: str, approved: bool,
                    decided_by: str, reason: str | None = None) -> bool:
    """Record one decision. Returns False if it was already decided.

    Decisions are final and are not silently overwritten: a second call on the
    same proposal is refused rather than allowed to flip an earlier judgement
    that something downstream may already have acted on.
    """
    return execute(conn, """
        UPDATE proposal
        SET status = %s, decided_by = %s, decided_at = now(), reason = %s
        WHERE id = %s AND status = 'pending'
    """, ("approved" if approved else "rejected", decided_by, reason, proposal_id)) == 1


# --------------------------------------------------------------- commit --

def commit_approved(conn: psycopg.Connection, pile_id: str, run_id: str,
                    register: Register) -> dict[str, Any]:
    """Write only what a person approved, and record why each section changed.

    Rejected proposals are left exactly as they were. This is the whole point of
    per-item review: rejecting one finding must not discard the rest, and
    approving one must not drag its siblings along.
    """
    approved = list_proposals(conn, run_id, status="approved")
    rejected = list_proposals(conn, run_id, status="rejected")
    pending = list_proposals(conn, run_id, status="pending")
    if pending:
        raise ValueError(
            f"{len(pending)} proposal(s) still pending; a run cannot commit "
            f"while any item is undecided"
        )

    version_row = fetch_one(conn, """
        SELECT coalesce(max(version), 0) + 1 AS next FROM deliverable WHERE pile_id = %s
    """, (pile_id,))
    version = version_row["next"]
    deliverable = fetch_one(conn, """
        INSERT INTO deliverable (pile_id, version) VALUES (%s, %s) RETURNING id
    """, (pile_id, version))
    deliverable_id = str(deliverable["id"])

    previous = {
        row["section_key"]: row["content_hash"]
        for row in fetch_all(conn, """
            SELECT s.section_key, s.content_hash FROM section s
            JOIN deliverable d ON d.id = s.deliverable_id
            WHERE d.pile_id = %s AND d.version = %s
        """, (pile_id, version - 1))
    }

    written = 0
    for proposal in approved:
        payload = proposal["payload"]
        if proposal["kind"] == "section_patch":
            section = register.section(payload["section_key"])
            if section is None:
                continue
            row = fetch_one(conn, """
                INSERT INTO section (deliverable_id, section_key, ordinal, body, content_hash)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (deliverable_id, section.key, section.ordinal, section.body,
                  section.content_hash))
            execute(conn, """
                INSERT INTO audit (pile_id, section_id, section_key, from_hash, to_hash,
                                   run_id, cause_document_id, proposal_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (pile_id, row["id"], section.key, previous.get(section.key),
                  section.content_hash, run_id, payload.get("cause_document_id"),
                  proposal["id"]))
            written += 1
        elif proposal["kind"] == "conflict" and proposal["ref_id"]:
            execute(conn, "UPDATE conflict SET status = 'approved' WHERE id = %s",
                    (proposal["ref_id"],))

    for proposal in rejected:
        if proposal["kind"] == "conflict" and proposal["ref_id"]:
            # Rejected means the proposed resolution was refused. The conflict
            # does not disappear -- it stays visible as unresolved, which is the
            # honest state.
            execute(conn, "UPDATE conflict SET status = 'rejected' WHERE id = %s",
                    (proposal["ref_id"],))

    set_run_status(conn, run_id, "committed")
    return {
        "deliverable_id": deliverable_id, "version": version,
        "sections_written": written,
        "approved": len(approved), "rejected": len(rejected),
    }


def sections_for_version(conn: psycopg.Connection, pile_id: str,
                         version: int | None = None) -> list[dict[str, Any]]:
    if version is None:
        row = fetch_one(conn, "SELECT max(version) AS v FROM deliverable WHERE pile_id = %s",
                        (pile_id,))
        version = row["v"]
    if version is None:
        return []
    return fetch_all(conn, """
        SELECT s.section_key, s.ordinal, s.body, s.content_hash
        FROM section s JOIN deliverable d ON d.id = s.deliverable_id
        WHERE d.pile_id = %s AND d.version = %s ORDER BY s.ordinal
    """, (pile_id, version))


def audit_trail(conn: psycopg.Connection, pile_id: str) -> list[dict[str, Any]]:
    """What changed, when, and because of which source."""
    return fetch_all(conn, """
        SELECT a.section_key, a.from_hash, a.to_hash, a.committed_at,
               d.filename AS cause_document, a.run_id
        FROM audit a LEFT JOIN document d ON d.id = a.cause_document_id
        WHERE a.pile_id = %s ORDER BY a.committed_at DESC, a.section_key
    """, (pile_id,))
