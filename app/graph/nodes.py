"""The stages, as graph nodes.

Every node here is a thin wrapper: it calls a stage that is still a pure
function, writes what the stage decided, and returns the part of the state that
moved. The stages themselves know nothing about graphs, checkpoints or the
database, which is why this port was a wrap rather than a rewrite.

Three rules hold across all of them, and each one is load-bearing.

**A node's writes and the record that it finished commit together.** Nodes never
call `commit()`. The checkpointer does, once, when it persists the step. See
`app/graph/checkpoint.py` for why that is the whole of behaviour 2.

**A node may run twice.** The one that was in flight when a process died runs
again from the top on resume, with its earlier writes rolled back. So a node
must be safe to re-enter: no appending to a list that survived, no counting
something that was already counted. Where a node cannot be made re-entrant --
the gate, which creates proposals -- the interrupt lives in a node of its own
that writes nothing.

**Facts live in the database, not in the state.** Nodes that need the pile's
facts load them. See `app/graph/state.py` for the reasoning.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from langgraph.types import interrupt

from app.domain.config import DomainConfig
from app.domain.models import SourcedFact
from app.graph.state import RunState, register_from_state, register_to_state
from app.ingest.formats import detect_format, extract_pages
from app.ingest.ingest import ingest_path
from app.llm.base import Provider
from app.stages.classify import classify_document
from app.stages.compose import Register, compose
from app.stages.entities import resolve_entity
from app.stages.extract import Gap, extract_document
from app.stages.examine import Finding, examine
from app.stages.reconcile import Conflict, ReconcileResult, reconcile
from app.store import repository as repo


class _Timer:
    def __enter__(self) -> "_Timer":
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc) -> None:
        self.ms = int((time.perf_counter() - self.start) * 1000)


@dataclass
class Nodes:
    """The graph's dependencies, bound once per run.

    `conn` is the run's own connection, shared with the checkpointer. Everything
    a node writes goes through it, which is what puts the work and the position
    in one transaction.
    """

    conn: psycopg.Connection
    provider: Provider
    cfg: DomainConfig

    # ------------------------------------------------------------ ingest --

    def ingest(self, state: RunState) -> RunState:
        """Bytes into the pile. Duplicates are recorded and then left alone."""
        run_id, pile_id = state["run_id"], state["pile_id"]
        documents: dict[str, str] = {}
        duplicates: list[str] = []
        queue: list[str] = []

        for source in state["sources"]:
            path = Path(source)
            result = ingest_path(self.conn, pile_id, path)
            repo.record_stage_event(
                self.conn, run_id, "ingest",
                "duplicate" if result.duplicate else result.status,
                document_id=result.document_id,
                detail={"filename": result.filename, "format": result.format,
                        "note": result.note},
            )
            if result.document_id:
                documents[result.filename] = result.document_id
            if result.duplicate:
                duplicates.append(result.filename)
                continue
            if result.status == "ingested":
                queue.append(source)

        return {"documents": documents, "duplicates": duplicates, "queue": queue,
                "current": None}

    # -------------------------------------------------- the document loop --

    def select_document(self, state: RunState) -> RunState:
        """Take the next document, or signal that the pile is understood.

        Splitting this out is what makes the resume boundary a document rather
        than a pile: a run killed on document five restarts on document five.
        """
        queue = list(state.get("queue") or [])
        if not queue:
            return {"current": None, "queue": []}
        return {"current": queue[0], "queue": queue[1:]}

    def classify(self, state: RunState) -> RunState:
        path = Path(state["current"])
        text = self._page_text(path)

        with _Timer() as timer:
            result = classify_document(self.provider, self.cfg, path.name, text)

        repo.record_stage_event(
            self.conn, state["run_id"], "classify", result.path,
            document_id=state["documents"].get(path.name),
            ms=timer.ms, tokens_in=result.usage.tokens_in,
            tokens_out=result.usage.tokens_out, cost_usd=result.usage.cost_usd,
            model=result.usage.model, detail={"note": result.note},
        )

        document_id = state["documents"].get(path.name)

        if result.quarantine:
            # Leaves the extraction path entirely. What the document says
            # becomes something to report on, never something to act on.
            if document_id:
                repo.execute_quarantine(self.conn, document_id, result.note or "")
            return {"quarantined": [*state.get("quarantined", []),
                                    {"document": path.name, "note": result.note or ""}]}

        if result.escalate:
            if document_id:
                repo.set_document_type(self.conn, document_id, result.doc_type,
                                       result.confidence, status="escalated")
            return {"escalated": [*state.get("escalated", []),
                                  {"document": path.name, "stage": "classify",
                                   "note": result.note or ""}]}

        if document_id:
            repo.set_document_type(self.conn, document_id, result.doc_type,
                                   result.confidence)
        return {"doc_types": {**state.get("doc_types", {}),
                              path.name: result.doc_type}}

    def extract(self, state: RunState) -> RunState:
        """Facts out of one document, each bound to the span it came from.

        Entity resolution happens here rather than in a node of its own because
        it is not a separate decision: a set of facts and the engagement they
        belong to are written together or not at all. Splitting them would allow
        a checkpoint boundary at which facts exist with no engagement, which is
        the state that silently splits a pile in two.
        """
        path = Path(state["current"])
        run_id, pile_id = state["run_id"], state["pile_id"]
        document_id = state["documents"].get(path.name)
        doc_type = state["doc_types"][path.name]
        text = self._page_text(path)

        with _Timer() as timer:
            extraction = extract_document(self.provider, self.cfg, doc_type,
                                          path.name, text)
        repo.record_stage_event(
            self.conn, run_id, "extract", extraction.path, document_id=document_id,
            ms=timer.ms, tokens_in=extraction.usage.tokens_in,
            tokens_out=extraction.usage.tokens_out, cost_usd=extraction.usage.cost_usd,
            model=extraction.usage.model,
            detail={"facts": len(extraction.facts), "gaps": len(extraction.gaps),
                    "attempts": extraction.attempts},
        )

        gaps = [*state.get("gaps", []), *(self._gap_row(path.name, g)
                                          for g in extraction.gaps)]

        # Identity is settled against the engagements the pile already knows,
        # never taken from the model's phrasing. This used to live only in the
        # incremental path; a full run resolving identity differently from an
        # update is the same bug with a longer fuse.
        known = repo.known_entity_keys(self.conn, pile_id)
        schema = self.cfg.extraction[self.cfg.doc_types[doc_type].extraction]
        resolution = resolve_entity(
            extraction.counterparty or "", known,
            schema.get("entity_key", "engagement:{counterparty_slug}"),
            self.cfg.reconciliation.get("entity"),
        )
        repo.record_stage_event(
            self.conn, run_id, "resolve_entity", resolution.method,
            document_id=document_id,
            detail={"entity_key": resolution.entity_key, "note": resolution.note},
        )

        if resolution.escalate:
            # Merging two engagements corrupts the register; splitting one hides
            # every conflict. Neither is ours to choose silently.
            if document_id:
                repo.set_document_type(self.conn, document_id, doc_type,
                                       1.0, status="escalated")
            return {"gaps": gaps,
                    "escalated": [*state.get("escalated", []),
                                  {"document": path.name, "stage": "resolve_entity",
                                   "note": resolution.note or ""}]}

        if resolution.entity_key is None or not extraction.facts:
            if extraction.facts:
                # Without a key these facts can never join a group, so they
                # could never be compared with anything. An explicit gap, not a
                # silent drop.
                gaps.append({"document": path.name, "field_name": "*",
                             "reason": "no entity key could be built",
                             "detail": f"{len(extraction.facts)} facts could not be "
                                       f"attributed to an engagement and are "
                                       f"excluded from the register"})
            return {"gaps": gaps,
                    "fact_counts": {**state.get("fact_counts", {}), path.name: 0}}

        sourced = [SourcedFact(document=path.name, doc_type=doc_type,
                               entity_key=resolution.entity_key, fact=fact)
                   for fact in extraction.facts]
        repo.persist_facts(self.conn, pile_id, run_id, document_id, sourced)

        return {
            "gaps": gaps,
            "entity_keys": {**state.get("entity_keys", {}),
                            path.name: resolution.entity_key},
            "fact_counts": {**state.get("fact_counts", {}), path.name: len(sourced)},
        }

    # ----------------------------------------------------- the whole pile --

    def reconcile(self, state: RunState) -> RunState:
        """Group the pile's facts and find where they disagree.

        Conflict rows are written by `propose`, not here. The gate needs to know
        which conflicts were already put to a person, and writing them at two
        different points would make that answer depend on node ordering.
        """
        with _Timer() as timer:
            _, result = self._facts_and_conflicts(state)
        repo.record_stage_event(
            self.conn, state["run_id"], "reconcile", result.path, ms=timer.ms,
            detail={"conflicts": len(result.conflicts), "note": result.note},
        )
        return {"conflict_count": len(result.conflicts),
                "reconcile_path": result.path,
                "note": result.note if result.escalate else state.get("note")}

    def escalate_volume(self, state: RunState) -> RunState:
        """Past a certain number of conflicts, the run is what needs attention.

        Individual proposals stop being reviewable somewhere above a couple of
        dozen, so the run raises one item about itself rather than burying a
        person in items. It still composes and still goes to the gate -- the
        reviewer needs to see the register to judge the escalation.
        """
        repo.create_proposal(
            self.conn, state["pile_id"], state["run_id"], kind="escalation",
            summary=(f"this run found {state['conflict_count']} conflicts, above the "
                     f"configured ceiling; review the pile as a whole before "
                     f"deciding item by item"),
            payload={"conflicts": state["conflict_count"],
                     "ceiling": self.cfg.reconciliation.get("escalate_above"),
                     "note": state.get("note")},
        )
        return {"status": "escalated"}

    def compose(self, state: RunState) -> RunState:
        facts, result = self._facts_and_conflicts(state)
        with _Timer() as timer:
            register = compose(self.cfg, facts, result.conflicts, self._gaps(state))
        repo.record_stage_event(
            self.conn, state["run_id"], "compose", "composed", ms=timer.ms,
            detail={"sections": len(register.sections)},
        )
        return {"register": register_to_state(register)}

    def examine(self, state: RunState) -> RunState:
        """The second movement: check the pile against the playbook.

        Runs after `compose` so that a rule which needs to look at the
        deliverable can, and so the register is never shaped by whether a rule
        passed. Findings are their own output, not a section -- the register is
        what the documents say, and a finding is a judgement about it.

        No model is called here. See `app/stages/examine.py`: these are
        arithmetic, and a finding a reviewer has to trust is worth more than one
        a reviewer has to check.
        """
        pile_id, run_id = state["pile_id"], state["run_id"]
        facts = repo.load_sourced_facts(self.conn, self.cfg, pile_id)
        documents = repo.documents_for_pile(self.conn, pile_id)

        with _Timer() as timer:
            result = examine(self.cfg, facts, documents)
        repo.persist_findings(self.conn, pile_id, run_id, result.findings)

        repo.record_stage_event(
            self.conn, run_id, "examine", result.path, ms=timer.ms,
            detail={"rules": len(result.findings),
                    "violated": len(result.violations),
                    "satisfied": len(result.satisfied),
                    "not_enough_evidence": len(result.unjudged),
                    "summary": result.summary()},
        )
        return {"findings": [_finding_row(f) for f in result.findings],
                "examine_summary": result.summary()}

    def delta(self, state: RunState) -> RunState:
        """What this run actually changed, against the committed version.

        The unchanged list is the interesting half: those hashes were recomputed
        from scratch and came out identical. That is evidence, where a section
        skipped because we predicted it would not move is only an assumption.
        """
        register = register_from_state(state["register"])
        previous = {row["section_key"]: row["content_hash"]
                    for row in repo.sections_for_version(self.conn, state["pile_id"])}

        changed, unchanged, added = [], [], []
        for section in register.sections:
            if section.key not in previous:
                added.append(section.key)
            elif previous[section.key] != section.content_hash:
                changed.append(section.key)
            else:
                unchanged.append(section.key)

        delta = {"changed": changed, "unchanged": unchanged, "added": added}
        repo.record_stage_event(
            self.conn, state["run_id"], "delta",
            "noop" if not changed and not added else "patched", detail=delta,
        )
        return {"delta": delta}

    def propose(self, state: RunState) -> RunState:
        """Turn what changed into items a person can decide on, one at a time.

        Only what moved is proposed. A reviewer of an update should see the
        update; re-proposing six unchanged sections would bury the one that
        matters. On a fresh pile nothing has been decided yet, so everything is
        new and everything is proposed -- the full run and the update are the
        same code seeing a different starting point, not two implementations
        that have to be kept in agreement.

        For conflicts that means: put a conflict to a person unless the same
        conflict, with the same values, has already been put to one. This
        replaced a rule that skipped conflicts on fields the arriving document
        did not mention. That rule was a guess about which conflicts could have
        changed; this one compares what they actually say.
        """
        pile_id, run_id = state["pile_id"], state["run_id"]
        register = register_from_state(state["register"])
        _, result = self._facts_and_conflicts(state)

        seen = repo.conflict_proposal_history(self.conn, pile_id)
        conflict_ids = repo.persist_conflicts(self.conn, pile_id, result.conflicts)
        for conflict in result.conflicts:
            if self._already_reviewed(seen.get(conflict.key, []), conflict):
                continue
            repo.create_proposal(
                self.conn, pile_id, run_id, kind="conflict",
                ref_id=conflict_ids.get(conflict.key),
                summary=self._conflict_summary(conflict),
                payload=self._conflict_payload(conflict),
            )

        # Findings the playbook says are broken. Only violations are decisions;
        # a rule that passed and a rule nobody could judge are things to read in
        # the report, and putting them in front of a reviewer as items to
        # approve would bury the four that matter under the four that do not.
        seen_findings = repo.finding_proposal_details(self.conn, pile_id)
        finding_ids = repo.finding_ids(self.conn, pile_id)
        for row in state.get("findings", []):
            if row["outcome"] != "violated":
                continue
            key = f"{row['rule_key']}|{row['entity_key']}"
            if any(prior["status"] == "pending" or prior["detail"] == row["detail"]
                   for prior in seen_findings.get(key, [])):
                continue
            repo.create_proposal(
                self.conn, pile_id, run_id, kind="finding",
                ref_id=finding_ids.get((row["rule_key"], row["entity_key"])),
                summary=f"{row['rule_key']} ({row['severity']}): {row['detail']}",
                payload=row,
            )

        delta = state["delta"]
        asked = repo.section_proposal_hashes(self.conn, pile_id)
        for key in delta["added"] + delta["changed"]:
            section = register.section(key)
            if (key, section.content_hash) in asked:
                # These exact bytes are already in front of a reviewer, or were
                # already turned down. Asking a second time would put the same
                # question on the desk twice, which is how a run that arrives
                # while another is still at the gate turns one review into two.
                continue
            repo.create_proposal(
                self.conn, pile_id, run_id, kind="section_patch",
                summary=(f"{key}: {'new section' if key in delta['added'] else 'updated'}"
                         f" — {len(section.citations)} citations, "
                         f"{section.gap_count} gaps"),
                payload={"section_key": key, "content_hash": section.content_hash,
                         "cause_document_id": self._cause_document_id(state),
                         "body": section.body},
            )

        pending = repo.list_proposals(self.conn, run_id, status="pending")
        if not pending:
            # Nothing moved, so there is nothing for a person to decide. Opening
            # a gate over an empty list would train a reviewer to click through
            # it, which is how a gate stops being one.
            note = ("identical bytes already ingested; nothing to update"
                    if state.get("duplicates") and not state.get("fact_counts")
                    else "the documents were read and their facts stored, but no "
                         "section of the register changed")
            repo.set_run_status(self.conn, run_id, "no_change")
            repo.record_stage_event(self.conn, run_id, "gate", "nothing_to_review",
                                    detail={"note": note})
            return {"proposal_count": 0, "status": "no_change", "note": note}

        repo.set_run_status(self.conn, run_id, "awaiting_approval")
        repo.record_stage_event(self.conn, run_id, "gate", "opened",
                                detail={"proposals": len(pending)})
        return {"proposal_count": len(pending), "status": "awaiting_approval"}

    def gate(self, state: RunState) -> RunState:
        """Halt. Nothing beyond this point runs until a person has decided.

        This node writes nothing, and that is not an oversight. A node that
        interrupts re-executes from the top when the run resumes, so anything it
        wrote would be written twice. The proposals are created in `propose`,
        which runs once and stays run.
        """
        interrupt({
            "run_id": state["run_id"],
            "awaiting": "human review",
            "proposals": state.get("proposal_count", 0),
            "conflicts": state.get("conflict_count", 0),
        })
        return {}

    def commit(self, state: RunState) -> RunState:
        """Write exactly what was approved, and nothing else.

        The register comes out of the checkpoint rather than being recomposed,
        because commit must write the bytes a person actually reviewed.
        `commit_approved` checks each approved section against the hash that was
        on the proposal and refuses if they differ.
        """
        register = register_from_state(state["register"])
        result = repo.commit_approved(self.conn, state["pile_id"], state["run_id"],
                                      register)
        repo.record_stage_event(
            self.conn, state["run_id"], "commit", "committed",
            detail={k: v for k, v in result.items() if k != "deliverable_id"},
        )
        return {"status": "committed", "committed": result}

    # ------------------------------------------------------------ routing --

    def after_select(self, state: RunState) -> str:
        return "classify" if state.get("current") else "reconcile"

    def after_classify(self, state: RunState) -> str:
        """Quarantine and low-confidence both leave the extraction path."""
        path = Path(state["current"])
        return "extract" if path.name in state.get("doc_types", {}) else "select_document"

    def after_reconcile(self, state: RunState) -> str:
        return "escalate_volume" if state.get("reconcile_path") == "escalate_volume" \
            else "compose"

    def after_propose(self, state: RunState) -> str:
        """A run with nothing to propose does not open a gate."""
        return "gate" if state.get("proposal_count") else "done"

    # ------------------------------------------------------------ helpers --

    def _page_text(self, path: Path) -> str:
        data = path.read_bytes()
        return extract_pages(data, detect_format(path, data))[0].text

    def _gaps(self, state: RunState) -> list[tuple[str, Gap]]:
        return [(row["document"], Gap(row["field_name"], row["reason"], row["detail"]))
                for row in state.get("gaps", [])]

    @staticmethod
    def _gap_row(document: str, gap: Gap) -> dict[str, Any]:
        return {"document": document, "field_name": gap.field_name,
                "reason": gap.reason, "detail": gap.detail}

    def _facts_and_conflicts(self, state: RunState) -> tuple[list[SourcedFact],
                                                             ReconcileResult]:
        """The pile as it stands, reconciled.

        Reloaded and recomputed rather than carried between nodes. Both are pure
        and cheap; see `app/graph/state.py` for why that trade is the right way
        round for a checkpoint that a different process has to read back.
        """
        facts = repo.load_sourced_facts(self.conn, self.cfg, state["pile_id"])
        return facts, reconcile(self.cfg, facts)

    def _cause_document_id(self, state: RunState) -> str | None:
        """Which arriving document explains this change.

        Only meaningful when one document arrived. A full run over a pile has no
        single cause, and naming one would put a claim in the audit trail that
        the run cannot support.
        """
        queue = state.get("sources") or []
        if len(queue) != 1:
            return None
        return state.get("documents", {}).get(Path(queue[0]).name)

    @staticmethod
    def _already_reviewed(history: list[dict[str, Any]], conflict: Conflict) -> bool:
        """Has this exact disagreement already been put to a person?

        Two cases count. One is still waiting on a decision -- asking again
        would put the same question in front of the reviewer twice. One was
        decided and says exactly what it said then -- asking again would reopen
        a settled judgement for no reason. Anything else is a changed conflict
        and deserves a fresh look.
        """
        values = conflict.distinct_values
        return any(row["status"] == "pending" or row["values"] == values
                   for row in history)

    @staticmethod
    def _conflict_summary(conflict: Conflict) -> str:
        proposed = conflict.proposed
        return (
            f"{conflict.field}: {len(conflict.distinct_values)} distinct values across "
            f"{len(conflict.members)} documents"
            + (f" — propose {proposed.display} from {proposed.document}" if proposed
               else " — no proposal; the documents do not settle this")
        )

    @staticmethod
    def _conflict_payload(conflict: Conflict) -> dict[str, Any]:
        proposed = conflict.proposed
        return {
            "field": conflict.field,
            "entity_key": conflict.entity_key,
            "values": conflict.distinct_values,
            "proposed": proposed.canonical if proposed else None,
            "proposed_from": proposed.document if proposed else None,
            "rationale": conflict.rationale,
            "members": [
                {"document": m.document, "doc_type": m.doc_type, "value": m.canonical,
                 "quote": m.fact.span.text, "char_start": m.fact.span.char_start,
                 "char_end": m.fact.span.char_end}
                for m in conflict.members
            ],
        }


def _finding_row(finding: Finding) -> dict[str, Any]:
    """A finding as plain JSON, so it can live in the checkpoint and be the
    proposal payload without being reshaped twice."""
    return {
        "rule_key": finding.rule_key,
        "entity_key": finding.entity_key,
        "severity": finding.severity,
        "outcome": finding.outcome,
        "statement": " ".join(finding.statement.split()),
        "detail": finding.detail,
        "citations": [
            {"document": c.document, "doc_type": c.doc_type, "value": c.display,
             "quote": c.fact.span.text, "char_start": c.fact.span.char_start,
             "char_end": c.fact.span.char_end}
            for c in finding.citations
        ],
    }


__all__ = ["Nodes", "Register"]
