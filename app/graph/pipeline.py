"""Starting a run, and picking one back up.

Two entry points and one guarantee between them.

    start(...)      ingest, understand, halt at the gate. Commits nothing.
    resume(run_id)  continue, from wherever this run actually is.

`resume` deliberately does not ask the caller what it is resuming *from*. A run
killed halfway through extraction and a run waiting on a reviewer are the same
question -- "what is the last thing this run is known to have finished?" -- and
the checkpoint is the only thing that can answer it. Making the caller declare
its intent would mean trusting a caller that just crashed.

The run owns its connection for its whole life, because the checkpointer commits
it (see `checkpoint.py`) and because behaviour 9's per-pile lock has to be held
by a session, not by a statement.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable

import psycopg
from langgraph.types import Command

from app.domain.config import DomainConfig, load_domain
from app.graph.build import build_graph
from app.graph.checkpoint import DurableSaver, run_connection, thread_config
from app.graph.locking import PileBusy, hold_pile
from app.graph.nodes import Nodes
from app.graph.state import RunState, new_state, register_from_state
from app.llm.base import Provider
from app.llm.durable import DurableProvider
from app.stages.compose import Register
from app.domain.models import SourcedFact
from app.stages.reconcile import Conflict, ReconcileResult, reconcile
from app.store import repository as repo
from app.store.engine import connect, fetch_one


@dataclass
class Delta:
    changed: list[str] = dc_field(default_factory=list)
    unchanged: list[str] = dc_field(default_factory=list)
    added: list[str] = dc_field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.changed and not self.added


@dataclass
class GateView:
    """What is waiting for a person, read back from the database.

    Deliberately not a live object handed out by the run: the reviewer may be a
    different process on a different day, so the gate is whatever the proposal
    table says it is.
    """
    run_id: str
    proposals: list[dict[str, Any]]

    @property
    def pending(self) -> list[dict[str, Any]]:
        return [p for p in self.proposals if p["status"] == "pending"]

    @property
    def is_open(self) -> bool:
        return bool(self.pending)


@dataclass
class RunResult:
    run_id: str
    pile_id: str
    state: RunState
    register: Register | None
    delta: Delta
    gate: GateView
    facts: list[SourcedFact]
    reconciliation: ReconcileResult
    proposed_conflicts: list[Conflict]
    model_calls: int
    replayed_calls: int
    interrupted: bool
    stage_events: list[dict[str, Any]] = dc_field(default_factory=list)

    # -- what the run found ------------------------------------------------

    @property
    def conflicts(self) -> list[Conflict]:
        return self.reconciliation.conflicts

    @property
    def status(self) -> str:
        return self.state.get("status", "unknown")

    @property
    def note(self) -> str | None:
        return self.state.get("note")

    @property
    def documents(self) -> dict[str, str]:
        return self.state.get("documents", {})

    @property
    def duplicates(self) -> list[str]:
        return self.state.get("duplicates", [])

    @property
    def fact_count(self) -> int:
        return sum(self.state.get("fact_counts", {}).values())

    @property
    def gaps(self) -> list[dict[str, Any]]:
        return self.state.get("gaps", [])

    @property
    def gap_pairs(self) -> list[tuple[str, Any]]:
        """Gaps in the shape `compose` takes them."""
        from app.stages.extract import Gap

        return [(row["document"], Gap(row["field_name"], row["reason"], row["detail"]))
                for row in self.gaps]

    @property
    def quarantined(self) -> list[dict[str, str]]:
        return self.state.get("quarantined", [])

    @property
    def escalated(self) -> list[dict[str, str]]:
        return self.state.get("escalated", [])

    @property
    def committed(self) -> dict[str, Any] | None:
        return self.state.get("committed")

    @property
    def path(self) -> str:
        """A one-word account of what this run turned out to be."""
        if self.status == "no_change":
            return "noop_duplicate" if self.duplicates and not self.fact_count \
                else "noop_no_change"
        return self.status

    # -- what the run cost, and which way it went --------------------------
    #
    # Behaviour 10 and behaviour 1 read off the same rows, because they are the
    # same record: a stage that says what it decided also says what deciding it
    # cost.

    def cost_by_stage(self) -> dict[str, dict[str, float | int]]:
        out: dict[str, dict[str, float | int]] = {}
        for event in self.stage_events:
            row = out.setdefault(event["stage"], {"calls": 0, "ms": 0, "tokens_in": 0,
                                                  "tokens_out": 0, "cost_usd": 0.0})
            row["calls"] += 1
            row["ms"] += event["ms"] or 0
            row["tokens_in"] += event["tokens_in"]
            row["tokens_out"] += event["tokens_out"]
            row["cost_usd"] = round(row["cost_usd"] + float(event["cost_usd"]), 6)
        return out

    def paths_taken(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for event in self.stage_events:
            key = f"{event['stage']}:{event['path_taken']}"
            counts[key] = counts.get(key, 0) + 1
        return counts


# ------------------------------------------------------------------- driving --

def start(provider: Provider, cfg: DomainConfig, pile_id: str,
          sources: Iterable[Path], kind: str = "full",
          wait_seconds: float = 2.0) -> RunResult:
    """Understand a pile, or a document arriving into one, and halt at the gate.

    Raises `PileBusy` if another run holds the pile. Note the ordering: the pile
    is taken *before* the run row is written, so a refused run leaves no trace
    of a run that never happened.
    """
    paths = [str(Path(p).resolve()) for p in sorted(sources)]
    return _drive(provider, cfg, None, pile_id, kind=kind, sources=paths,
                  wait_seconds=wait_seconds)


def resume(provider: Provider, cfg: DomainConfig | None = None,
           run_id: str = "", wait_seconds: float = 2.0) -> RunResult:
    """Continue a run from its last committed checkpoint.

    Works for a run that was killed and for a run that is waiting on a reviewer;
    which one it is comes from the checkpoint, not from the caller.
    """
    with connect() as conn:
        run = repo.get_run(conn, run_id)
        if run is None:
            raise LookupError(f"no run {run_id}")
        pile = fetch_one(conn, "SELECT domain FROM pile WHERE id = %s",
                         (run["pile_id"],))
    cfg = cfg or load_domain(pile["domain"])
    return _drive(provider, cfg, run_id, str(run["pile_id"]),
                  wait_seconds=wait_seconds)


def _drive(provider: Provider, cfg: DomainConfig, run_id: str | None, pile_id: str,
           kind: str = "full", sources: list[str] | None = None,
           wait_seconds: float = 2.0) -> RunResult:
    """One working phase of one run, holding the pile for its duration.

    Starting and resuming share this because they are the same thing: take the
    pile, do as much as can be done without a person, give the pile back.
    """
    with run_connection() as conn, hold_pile(conn, pile_id, wait_seconds):
        # The run row is written only once the pile is actually held, so a
        # refused run leaves no record of a run that never happened. It is
        # committed straight away because two other things point at it from
        # elsewhere: the checkpoint thread is named after it, and the model-call
        # ledger writes on a connection of its own.
        initial: RunState | None = None
        if run_id is None:
            run_id = repo.create_run(conn, pile_id, kind=kind)
            conn.commit()
            initial = new_state(run_id, pile_id, cfg.name, kind, sources or [])

        config = thread_config(run_id)
        durable = DurableProvider(provider, run_id)
        try:
            saver = DurableSaver(conn)
            graph = build_graph(Nodes(conn=conn, provider=durable, cfg=cfg), saver)

            payload: Any = initial
            if initial is None:
                if saver.get_tuple(config) is None:
                    # Silence here would look like a successful resume of a run
                    # that never checkpointed anything.
                    raise LookupError(
                        f"run {run_id} has no checkpoint; it cannot be resumed. "
                        f"Start a new run rather than pretending this one continues."
                    )
                snapshot = graph.get_state(config)
                # An interrupt waiting to be answered is resumed with a value;
                # a run that simply stopped is resumed with nothing. Asking the
                # checkpoint which it is beats asking the caller.
                waiting = any(task.interrupts for task in snapshot.tasks)
                payload = Command(resume="proceed") if waiting else None

            # `durability="sync"` is not a tuning knob here, it is the
            # behaviour. LangGraph defaults to "async": the checkpoint is
            # written on a background thread while the next step already runs,
            # which is precisely the ordering that loses work -- a crash lands
            # between a node's committed writes and the record that it
            # finished. "sync" persists the step before the next one starts,
            # which is also what lets the checkpointer share this connection at
            # all, since two threads cannot use one psycopg connection.
            graph.invoke(payload, config, durability="sync")
            snapshot = graph.get_state(config)
            state: RunState = dict(snapshot.values)  # type: ignore[assignment]
            interrupted = any(task.interrupts for task in snapshot.tasks)

            return _result(conn, cfg, run_id, pile_id, state, durable, interrupted)
        finally:
            durable.close()


def _result(conn: psycopg.Connection, cfg: DomainConfig, run_id: str, pile_id: str,
            state: RunState, durable: DurableProvider, interrupted: bool) -> RunResult:
    facts = repo.load_sourced_facts(conn, cfg, pile_id)
    reconciliation = reconcile(cfg, facts)
    proposals = repo.list_proposals(conn, run_id)

    proposed_keys = {(p["payload"].get("entity_key"), p["payload"].get("field"))
                     for p in proposals if p["kind"] == "conflict"}
    delta = state.get("delta") or {}

    return RunResult(
        run_id=run_id,
        pile_id=pile_id,
        state=state,
        register=register_from_state(state.get("register")),
        delta=Delta(changed=list(delta.get("changed", [])),
                    unchanged=list(delta.get("unchanged", [])),
                    added=list(delta.get("added", []))),
        gate=GateView(run_id, proposals),
        facts=facts,
        reconciliation=reconciliation,
        proposed_conflicts=[c for c in reconciliation.conflicts
                            if c.key in proposed_keys],
        model_calls=durable.issued,
        replayed_calls=durable.replayed,
        interrupted=interrupted,
        stage_events=repo.stage_events(conn, run_id),
    )


# ---------------------------------------------------- the two named movements --
#
# Both call the same graph. The names survive because they are what the brief
# calls the movements, not because the code underneath differs.

def run_understand(provider: Provider, cfg: DomainConfig, pile_id: str,
                   paths: Iterable[Path]) -> RunResult:
    return start(provider, cfg, pile_id, paths, kind="full")


def run_incremental(provider: Provider, cfg: DomainConfig, pile_id: str,
                    path: Path) -> RunResult:
    return start(provider, cfg, pile_id, [path], kind="incremental")


__all__ = ["start", "resume", "run_understand", "run_incremental",
           "RunResult", "Delta", "GateView", "PileBusy"]
