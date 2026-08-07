"""Run a pile through the graph and show what the run decided.

    python -m scripts.run_pile --pile pile_acme --out register.md

Defaults to the recorded provider, so this runs with no key and no network. It
needs the database, because a run that cannot be resumed is not a run this
system claims to do -- see PROGRESS.md.

    python -m scripts.run_pile --resume <run_id>

picks a stopped run back up from its checkpoint.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.domain.config import load_domain
from app.graph import pipeline
from app.ingest.ingest import ensure_pile
from app.llm.base import get_provider
from app.settings import REPO_ROOT
from app.store.engine import transaction
from app.store import repository as repo


def _print_report(run_id: str) -> None:
    with transaction() as conn:
        report = repo.run_report(conn, run_id)

    print("\nstages")
    for row in report["stages"]:
        print(f"  {row['stage']:<16} {row['calls']:>3} entries  {row['ms'] or 0:>6} ms  "
              f"{row['tokens_in']:>7} in  {row['tokens_out']:>6} out  "
              f"${float(row['cost_usd']):.4f}   {','.join(sorted(row['paths']))}")
    totals = report["totals"]
    print(f"  {'TOTAL':<16} {'':>3}          {totals['ms']:>6} ms  "
          f"{totals['tokens_in']:>7} in  {totals['tokens_out']:>6} out  "
          f"${float(totals['cost_usd']):.4f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pile", default="pile_acme", help="corpus directory name")
    parser.add_argument("--domain", default="vendor_contracts")
    parser.add_argument("--arrival", default=None,
                        help="a single document arriving into the pile")
    parser.add_argument("--resume", default=None, metavar="RUN_ID",
                        help="continue a run that stopped")
    parser.add_argument("--out", default=None, help="write the register here")
    args = parser.parse_args()

    cfg = load_domain(args.domain)
    provider = get_provider()

    if args.resume:
        result = pipeline.resume(provider, cfg, run_id=args.resume)
        print(f"resumed run {result.run_id}: {result.status}")
        if result.replayed_calls:
            print(f"  {result.replayed_calls} model call(s) replayed from the run's "
                  f"ledger rather than re-issued")
    else:
        with transaction() as conn:
            pile_id = ensure_pile(conn, args.pile, args.domain)

        if args.arrival:
            path = (REPO_ROOT / "corpora" / args.arrival).resolve()
            if not path.is_file():
                print(f"no document at {path}")
                return 1
            result = pipeline.run_incremental(provider, cfg, pile_id, path)
            print(f"arrival: {path.name}  → run {result.run_id} ({result.path})")
        else:
            directory = REPO_ROOT / "corpora" / args.pile
            paths = sorted(p for p in directory.glob("*") if p.is_file())
            if not paths:
                print(f"no documents in {directory}")
                return 1
            result = pipeline.run_understand(provider, cfg, pile_id, paths)
            print(f"pile: {args.pile}  ({len(paths)} documents)  → run {result.run_id}")

    _print_report(result.run_id)

    print(f"\nfacts {result.fact_count}   gaps {len(result.gaps)}   "
          f"conflicts {len(result.conflicts)}   "
          f"quarantined {len(result.quarantined)}   escalated {len(result.escalated)}")
    print(f"model calls issued {result.model_calls}   replayed {result.replayed_calls}")

    for row in result.quarantined:
        print(f"  QUARANTINED {row['document']}: {row['note'][:120]}")
    for row in result.escalated:
        print(f"  ESCALATED   {row['document']} at {row['stage']}: {row['note'][:120]}")

    if result.conflicts:
        print("\nconflicts (all open; nothing has been resolved)")
        for conflict in result.conflicts:
            proposed = conflict.proposed.display if conflict.proposed else "none"
            print(f"  {conflict.field:<22} {conflict.distinct_values}  proposed: {proposed}")
    else:
        print("\nno disagreements found")

    delta = result.delta
    print(f"\ndelta  changed {delta.changed}  added {delta.added}  "
          f"unchanged {delta.unchanged}")
    print(f"gate   {len(result.gate.pending)} item(s) awaiting a decision "
          f"(status: {result.status})")
    if result.note:
        print(f"       {result.note}")

    if args.out and result.register:
        target = Path(args.out)
        target.write_text(result.register.render())
        print(f"\nregister written to {target}")
        for section in result.register.sections:
            print(f"  {section.key:<16} {section.content_hash[:12]}  "
                  f"{len(section.citations):>3} citations  {section.gap_count} gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
