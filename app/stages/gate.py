"""The human gate.

Graded behaviour 3, and one of the five that cannot be cut. Three properties,
each of which is a test:

  **Nothing commits until a person decides.** Between composing and committing,
  the run holds. The deliverable does not exist yet; the conflicts are open. A
  process killed here has written nothing to approve away.

  **Decisions are per item, in one review.** Approving one proposal does not
  drag its siblings along, and rejecting one does not discard the rest. This is
  the property the brief calls out by name, and the one a naive implementation
  gets wrong by treating a review as a single yes/no.

  **Every decision is respected.** Committing writes exactly what was approved
  and nothing else, and records why each section changed.

A run cannot commit while any item is still pending. Partial review is a state
worth being explicit about rather than resolving with a default, because a
default here is a decision nobody made.

The proposals themselves are raised by the graph's `propose` node, and the halt
is a graph `interrupt()` in the node after it. This module is what a reviewer --
human, HTTP client or MCP client -- acts on: read the items, decide them one by
one, and the run continues when it is resumed.
"""
from __future__ import annotations

from dataclasses import dataclass

import psycopg

from app.stages.compose import Register
from app.store import repository as repo


@dataclass
class Decision:
    proposal_id: str
    approved: bool
    reason: str | None = None


def decide(conn: psycopg.Connection, run_id: str, decisions: list[Decision],
           decided_by: str) -> dict[str, int]:
    """Apply a batch of per-item decisions in one review.

    A decision on an already-decided proposal is counted as `ignored` rather
    than silently overwriting an earlier judgement that something downstream may
    have acted on.
    """
    counts = {"approved": 0, "rejected": 0, "ignored": 0}
    for decision in decisions:
        applied = repo.decide_proposal(conn, decision.proposal_id, decision.approved,
                                       decided_by, decision.reason)
        if not applied:
            counts["ignored"] += 1
        else:
            counts["approved" if decision.approved else "rejected"] += 1
    return counts


def commit(conn: psycopg.Connection, pile_id: str, run_id: str,
           register: Register) -> dict:
    """Write what was approved. Raises while anything is still pending.

    Normally reached by resuming the run rather than called directly -- the
    graph's commit node is what runs this -- so that committing is a step the
    run took, and appears in its stage record as one.
    """
    return repo.commit_approved(conn, pile_id, run_id, register)
