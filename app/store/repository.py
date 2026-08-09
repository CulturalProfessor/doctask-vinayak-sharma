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
from app.retrieval.embed import embed_or_none
from app.retrieval.search import vector_literal
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
               ended_at = CASE WHEN %s IN ('committed', 'failed', 'cancelled',
                                           'no_change', 'abandoned')
                               THEN now() ELSE ended_at END
        WHERE id = %s
    """, (status, status, run_id))


def abandon_run(conn: psycopg.Connection, run_id: str, abandoned_by: str,
                reason: str) -> None:
    """End a run that will never finish, keeping everything it did.

    Nothing is deleted -- see `007_abandon.sql`. The facts, the stage events and
    the proposals all stay exactly as they are, and the run gains an ending that
    says who stopped it and why. `record_stage_event` puts the same thing in the
    run's own history, so the answer is visible from the run's timeline and not
    only from a column somebody has to think to look at.
    """
    set_run_status(conn, run_id, "abandoned")
    execute(conn, "UPDATE run SET abandoned_by = %s, abandon_reason = %s WHERE id = %s",
            (abandoned_by, reason, run_id))
    record_stage_event(conn, run_id, "abandon", "abandoned",
                       detail={"by": abandoned_by, "reason": reason})


def get_run(conn: psycopg.Connection, run_id: str) -> dict[str, Any] | None:
    return fetch_one(conn, "SELECT * FROM run WHERE id = %s", (run_id,))


def runs_for_pile(conn: psycopg.Connection, pile_id: str,
                  status: str | None = None) -> list[dict[str, Any]]:
    """This pile's runs, newest first, with how many items each still holds.

    A run stopped at the gate is only findable if something can list it. Without
    this, the only handle on an open gate is whatever the caller happened to
    keep -- a variable in a browser tab, a run id in a terminal's scrollback --
    and closing either would strand approved work behind a gate nobody can
    reach.
    """
    sql = """
        SELECT r.*,
               count(p.id) FILTER (WHERE p.status = 'pending') AS pending_proposals,
               count(p.id) AS proposals
        FROM run r LEFT JOIN proposal p ON p.run_id = r.id
        WHERE r.pile_id = %s
    """
    params: tuple = (pile_id,)
    if status:
        sql += " AND r.status = %s"
        params = (pile_id, status)
    return fetch_all(conn, sql + " GROUP BY r.id ORDER BY r.started_at DESC", params)


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


def stage_events(conn: psycopg.Connection, run_id: str) -> list[dict[str, Any]]:
    """Every stage this run entered, in order, with what it decided."""
    return fetch_all(conn, """
        SELECT stage, path_taken, document_id, ms, tokens_in, tokens_out,
               cost_usd, model, detail, created_at
        FROM stage_event WHERE run_id = %s ORDER BY created_at, id
    """, (run_id,))


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

    The embedding is written in the same INSERT as the span rather than by a
    pass afterwards. A span that exists without its vector is invisible to
    similarity search, so a two-step write would leave a window in which the
    pile is searchable and wrong about what it contains -- and if the process
    died inside that window it would stay wrong. `embed_or_none` returns NULL
    for text with no alphanumeric features, which is a real answer and not a
    failure: that span is not retrievable by similarity, and the column says so.
    """
    ids: list[str] = []
    for sourced in facts:
        span = sourced.fact.span
        vector = embed_or_none(span.text)
        span_row = fetch_one(conn, """
            INSERT INTO span (document_id, page_no, char_start, char_end, text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s::vector) RETURNING id
        """, (document_id, 1, span.char_start, span.char_end, span.text,
              vector_literal(vector) if vector is not None else None))
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


def document_is_unread(conn: psycopg.Connection, document_id: str) -> bool:
    """True if this document's bytes are stored and nothing has read them.

    `'ingested'` is the state a document is in before any stage has looked at
    it. Every other status -- classified, escalated, quarantined, unsupported --
    means something decided about it. The distinction matters because "these
    bytes are already here" and "these bytes have already been understood" are
    different claims, and treating the first as the second produces an empty
    register with a successful-looking run behind it.
    """
    row = fetch_one(conn, "SELECT status FROM document WHERE id = %s", (document_id,))
    return bool(row) and row["status"] == "ingested"


def set_document_type(conn: psycopg.Connection, document_id: str, doc_type: str | None,
                      confidence: float, status: str = "classified") -> None:
    execute(conn, """
        UPDATE document SET doc_type = %s, doc_type_conf = %s, status = %s WHERE id = %s
    """, (doc_type, confidence, status, document_id))


def load_sourced_facts(conn: psycopg.Connection, cfg, pile_id: str,
                       exclude_document_id: str | None = None) -> list[SourcedFact]:
    """Rebuild the pile's facts from storage.

    Normalised values are recomputed from `value_raw` rather than read back from
    a stored column. Normalisation is deterministic, so recomputing keeps one
    source of truth: a change to a currency rule or a tolerance takes effect on
    the next run instead of leaving old rows normalised under the old rules.
    """
    from app.domain.normalize import normalise
    from app.domain.spans import SpanMatch
    from app.stages.extract import ExtractedFact

    rows = fetch_all(conn, """
        SELECT f.*, s.char_start, s.char_end, s.text AS span_text,
               d.filename, d.doc_type, d.id AS doc_id
        FROM fact f
        JOIN span s     ON s.id = f.span_id
        JOIN document d ON d.id = f.document_id
        WHERE f.pile_id = %s AND (%s::uuid IS NULL OR d.id <> %s::uuid)
        ORDER BY d.filename, f.field, s.char_start
    """, (pile_id, exclude_document_id, exclude_document_id))

    out: list[SourcedFact] = []
    for row in rows:
        span = SpanMatch(row["char_start"], row["char_end"], row["span_text"],
                         "stored", 1.0)
        out.append(SourcedFact(
            document=row["filename"], doc_type=row["doc_type"] or "unknown",
            entity_key=row["entity_key"],
            fact=ExtractedFact(
                field_name=row["field"], value_raw=row["value_raw"], quote=row["span_text"],
                span=span,
                normalised=normalise(row["value_type"], row["value_raw"], cfg.normalization),
                value_type=row["value_type"], unit=row["unit"],
                confidence=float(row["confidence"]),
            ),
        ))
    return out


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


# -------------------------------------------------------------- findings --

def persist_findings(conn: psycopg.Connection, pile_id: str, run_id: str,
                     findings: Iterable[Any]) -> dict[tuple[str, str], str]:
    """Record what the playbook said, including where it said nothing.

    Citations are written as `finding_citation` rows pointing at the spans that
    establish the finding. The lookup from a fact back to its span is by
    (document, offsets), which is what a citation actually is -- the same fact
    re-extracted by a later run gets a new row id, and matching on that would
    lose every citation on the first re-examination.
    """
    out: dict[tuple[str, str], str] = {}
    for finding in findings:
        row = fetch_one(conn, """
            INSERT INTO finding (pile_id, run_id, rule_key, entity_key, severity,
                                 outcome, statement, detail)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (pile_id, rule_key, entity_key) DO UPDATE SET
                run_id = EXCLUDED.run_id, severity = EXCLUDED.severity,
                outcome = EXCLUDED.outcome, statement = EXCLUDED.statement,
                detail = EXCLUDED.detail, detected_at = now(),
                -- A finding whose text changed is a new question, so a decision
                -- taken on the old wording does not carry over to it.
                status = CASE WHEN finding.detail IS DISTINCT FROM EXCLUDED.detail
                              THEN 'open' ELSE finding.status END
            RETURNING id
        """, (pile_id, run_id, finding.rule_key, finding.entity_key,
              finding.severity, finding.outcome, finding.statement.strip(),
              finding.detail))
        finding_id = str(row["id"])
        out[(finding.rule_key, finding.entity_key)] = finding_id

        execute(conn, "DELETE FROM finding_citation WHERE finding_id = %s",
                (finding_id,))
        for cited in finding.citations:
            span = fetch_one(conn, """
                SELECT s.id FROM span s JOIN document d ON d.id = s.document_id
                WHERE d.pile_id = %s AND d.filename = %s
                  AND s.char_start = %s AND s.char_end = %s
                LIMIT 1
            """, (pile_id, cited.document, cited.fact.span.char_start,
                  cited.fact.span.char_end))
            if span:
                execute(conn, """
                    INSERT INTO finding_citation (finding_id, span_id)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                """, (finding_id, span["id"]))
    return out


def findings_for_pile(conn: psycopg.Connection, pile_id: str,
                      outcome: str | None = None) -> list[dict[str, Any]]:
    """Every rule's current answer, with the spans that support it."""
    sql = """
        SELECT f.id, f.rule_key, f.entity_key, f.severity, f.outcome, f.status,
               f.statement, f.detail, f.detected_at,
               coalesce(json_agg(json_build_object(
                   'document', d.filename, 'quote', s.text,
                   'char_start', s.char_start, 'char_end', s.char_end
               ) ORDER BY d.filename, s.char_start)
               FILTER (WHERE s.id IS NOT NULL), '[]') AS citations
        FROM finding f
        LEFT JOIN finding_citation fc ON fc.finding_id = f.id
        LEFT JOIN span s     ON s.id = fc.span_id
        LEFT JOIN document d ON d.id = s.document_id
        WHERE f.pile_id = %s
    """
    params: tuple = (pile_id,)
    if outcome:
        sql += " AND f.outcome = %s"
        params = (pile_id, outcome)
    return fetch_all(conn, sql + """
        GROUP BY f.id ORDER BY f.outcome, f.severity DESC, f.rule_key
    """, params)


def finding_proposal_details(conn: psycopg.Connection,
                             pile_id: str) -> dict[str, list[dict[str, Any]]]:
    """Which findings have already been put to a person, and what they said.

    Same shape and same reason as `conflict_proposal_history`: a run must not
    ask again about a finding still on someone's desk, nor re-open one that was
    decided and has not changed since.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for row in fetch_all(conn, """
        SELECT payload ->> 'rule_key' AS rule_key,
               payload ->> 'entity_key' AS entity_key,
               payload ->> 'detail'   AS detail,
               status
        FROM proposal WHERE pile_id = %s AND kind = 'finding'
    """, (pile_id,)):
        key = f"{row['rule_key']}|{row['entity_key']}"
        out.setdefault(key, []).append(
            {"status": row["status"], "detail": row["detail"]}
        )
    return out


def finding_ids(conn: psycopg.Connection,
                pile_id: str) -> dict[tuple[str, str], str]:
    return {(row["rule_key"], row["entity_key"]): str(row["id"])
            for row in fetch_all(conn, """
                SELECT id, rule_key, entity_key FROM finding WHERE pile_id = %s
            """, (pile_id,))}


def documents_for_pile(conn: psycopg.Connection,
                       pile_id: str) -> list[dict[str, Any]]:
    """Every document in the pile, including the ones that never became input.

    A quarantined document has no facts by design, so a stage that only looks at
    facts cannot see it -- and the rule that reports quarantine needs to.
    """
    return fetch_all(conn, """
        SELECT id, filename, format, doc_type, status, ingest_note
        FROM document WHERE pile_id = %s ORDER BY filename
    """, (pile_id,))


def set_finding_status(conn: psycopg.Connection, finding_id: str,
                       status: str) -> None:
    execute(conn, "UPDATE finding SET status = %s WHERE id = %s",
            (status, finding_id))


# ------------------------------------------------------------- proposals --

def known_entity_keys(conn: psycopg.Connection, pile_id: str) -> list[str]:
    return [row["entity_key"] for row in fetch_all(conn, """
        SELECT DISTINCT entity_key FROM fact WHERE pile_id = %s ORDER BY entity_key
    """, (pile_id,))]


def open_conflict_keys(conn: psycopg.Connection, pile_id: str) -> list[dict[str, Any]]:
    return fetch_all(conn, """
        SELECT entity_key, field FROM conflict WHERE pile_id = %s AND status = 'open'
    """, (pile_id,))


def conflict_proposal_history(conn: psycopg.Connection,
                              pile_id: str) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Every conflict this pile has already put to a person, and what it said.

    Keyed by (entity_key, field) and carrying the values that were on the
    proposal, so the gate can tell "this disagreement was already reviewed" from
    "this disagreement now says something different". Status matters as much as
    content: a pending item is a question still on someone's desk.
    """
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in fetch_all(conn, """
        SELECT payload ->> 'entity_key' AS entity_key,
               payload ->> 'field'      AS field,
               payload -> 'values'      AS values,
               status, created_at
        FROM proposal
        WHERE pile_id = %s AND kind = 'conflict'
        ORDER BY created_at
    """, (pile_id,)):
        key = (row["entity_key"], row["field"])
        out.setdefault(key, []).append(
            {"status": row["status"], "values": row["values"] or []}
        )
    return out


def section_proposal_hashes(conn: psycopg.Connection,
                            pile_id: str) -> set[tuple[str, str]]:
    """(section_key, content_hash) pairs this pile has already put to a person.

    The counterpart of `conflict_proposal_history` for sections, and it exists
    for the same reason: a second run must not ask again for content that is
    already on someone's desk, or re-litigate content they already turned down.
    Byte-identical is the right comparison because a section's hash *is* its
    content -- which is what makes this exact rather than a heuristic.
    """
    return {(row["section_key"], row["content_hash"]) for row in fetch_all(conn, """
        SELECT payload ->> 'section_key'  AS section_key,
               payload ->> 'content_hash' AS content_hash
        FROM proposal WHERE pile_id = %s AND kind = 'section_patch'
    """, (pile_id,))}


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
                    decided_by: str, reason: str | None = None,
                    decided_via: str = "direct") -> bool:
    """Record one decision. Returns False if it was already decided.

    Decisions are final and are not silently overwritten: a second call on the
    same proposal is refused rather than allowed to flip an earlier judgement
    that something downstream may already have acted on.

    `decided_by` is who the caller says decided; `decided_via` is which surface
    actually carried it, and the surface sets that rather than the caller. See
    migrations/003 -- an approval a person clicked and one an agent made through
    the machine interface must not be indistinguishable afterwards.
    """
    return execute(conn, """
        UPDATE proposal
        SET status = %s, decided_by = %s, decided_via = %s,
            decided_at = now(), reason = %s
        WHERE id = %s AND status = 'pending'
    """, ("approved" if approved else "rejected", decided_by, decided_via, reason,
          proposal_id)) == 1


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
    touched: set[str] = set()
    for proposal in approved:
        payload = proposal["payload"]
        if proposal["kind"] == "section_patch":
            section = register.section(payload["section_key"])
            if section is None:
                continue
            # What lands must be what was reviewed. The proposal carries the
            # hash of the bytes the person saw; if the register in hand hashes
            # differently, something changed between the review and the commit
            # and writing it would make the approval a lie about content nobody
            # agreed to. Refusing is the only honest option -- and this is the
            # check that lets the register be rebuilt from a checkpoint after a
            # restart instead of having to survive in a process's memory.
            if payload.get("content_hash") and payload["content_hash"] != section.content_hash:
                raise ValueError(
                    f"section {section.key!r} was approved at "
                    f"{payload['content_hash'][:12]} but now hashes to "
                    f"{section.content_hash[:12]}; refusing to commit content "
                    f"that was never reviewed"
                )
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
            touched.add(section.key)
        elif proposal["kind"] == "conflict" and proposal["ref_id"]:
            execute(conn, "UPDATE conflict SET status = 'approved' WHERE id = %s",
                    (proposal["ref_id"],))
        elif proposal["kind"] == "finding" and proposal["ref_id"]:
            # Accepted means the reviewer agrees the rule is broken. The finding
            # stands as something to act on, not as something resolved by having
            # been read.
            set_finding_status(conn, proposal["ref_id"], "accepted")

    # Carry forward every section this run did not touch, at its existing hash.
    #
    # An incremental run only proposes what changed, so without this the new
    # version would contain the one section that moved and silently lose the
    # five that did not. Carried sections get no audit row: nothing changed
    # about them, and claiming otherwise would make the trail lie.
    carried = 0
    for row in fetch_all(conn, """
        SELECT s.section_key, s.ordinal, s.body, s.content_hash
        FROM section s JOIN deliverable d ON d.id = s.deliverable_id
        WHERE d.pile_id = %s AND d.version = %s
    """, (pile_id, version - 1)):
        if row["section_key"] in touched:
            continue
        execute(conn, """
            INSERT INTO section (deliverable_id, section_key, ordinal, body, content_hash)
            VALUES (%s, %s, %s, %s, %s)
        """, (deliverable_id, row["section_key"], row["ordinal"], row["body"],
              row["content_hash"]))
        carried += 1

    for proposal in rejected:
        if proposal["kind"] == "conflict" and proposal["ref_id"]:
            # Rejected means the proposed resolution was refused. The conflict
            # does not disappear -- it stays visible as unresolved, which is the
            # honest state.
            execute(conn, "UPDATE conflict SET status = 'rejected' WHERE id = %s",
                    (proposal["ref_id"],))
        elif proposal["kind"] == "finding" and proposal["ref_id"]:
            # Dismissed means a person looked and judged it not a problem. The
            # finding stays on the record saying so, because deleting it would
            # let the next run raise it again as though it were new.
            set_finding_status(conn, proposal["ref_id"], "dismissed")

    set_run_status(conn, run_id, "committed")
    return {
        "deliverable_id": deliverable_id, "version": version,
        "sections_written": written, "sections_carried": carried,
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
