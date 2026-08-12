"""The graph.

One graph serves both movements. A full run over a pile and an update triggered
by a single arriving document differ only in what is in the queue and what is
already committed -- so they are the same nodes and the same edges, not two
pipelines that have to be kept in agreement. That symmetry is not cosmetic: the
entity-resolution bug in PROGRESS.md existed because a fix landed on one path
and not the other.

    ingest ─→ select_document ─┬─(a document)→ classify ─┬─→ extract ─→ select_document
                               │                          ├─(quarantine)→ select_document
                               │                          └─(low confidence)→ select_document
                               └─(queue empty)→ reconcile ─┬─(too many)→ escalate_volume ─┐
                                                           └─────────────────→ compose ←──┘
                                                                                  ↓
                                          commit ←── gate ←── propose ←── delta ←── examine
                                                                ↑
                                                          interrupt(): halts here.

Four branches change the path, and each is a decision rather than a
configuration flag: quarantine when a document tries to give orders, escalate
when classification is not confident enough to guess, escalate when a name
matches two known engagements, escalate the whole run when the conflicts stop
being individually reviewable. Retry-then-skip on malformed extraction is a
branch too; it lives inside the extract stage because the retry has to reuse the
same prompt state.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import Nodes
from app.graph.state import RunState


def build_graph(nodes: Nodes, checkpointer):
    graph = StateGraph(RunState)

    graph.add_node("ingest", nodes.ingest)
    graph.add_node("select_document", nodes.select_document)
    graph.add_node("classify", nodes.classify)
    graph.add_node("extract", nodes.extract)
    graph.add_node("reconcile", nodes.reconcile)
    graph.add_node("escalate_volume", nodes.escalate_volume)
    graph.add_node("compose", nodes.compose)
    graph.add_node("examine", nodes.examine)
    graph.add_node("delta", nodes.delta)
    graph.add_node("propose", nodes.propose)
    graph.add_node("gate", nodes.gate)
    graph.add_node("commit", nodes.commit)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "select_document")

    graph.add_conditional_edges(
        "select_document", nodes.after_select, {"classify": "classify", "reconcile": "reconcile"}
    )
    graph.add_conditional_edges(
        "classify",
        nodes.after_classify,
        {"extract": "extract", "select_document": "select_document"},
    )
    graph.add_edge("extract", "select_document")

    graph.add_conditional_edges(
        "reconcile",
        nodes.after_reconcile,
        {"escalate_volume": "escalate_volume", "compose": "compose"},
    )
    graph.add_edge("escalate_volume", "compose")
    graph.add_edge("compose", "examine")
    graph.add_edge("examine", "delta")
    graph.add_edge("delta", "propose")
    graph.add_conditional_edges("propose", nodes.after_propose, {"gate": "gate", "done": END})
    graph.add_edge("gate", "commit")
    graph.add_edge("commit", END)

    return graph.compile(checkpointer=checkpointer)
