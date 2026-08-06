"""Run the UNDERSTAND movement over a pile and write the register.

    python -m scripts.run_pile --pile pile_acme --out register.md

Defaults to the recorded provider, so this runs with no key and no network.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.domain.config import load_domain
from app.graph.understand import understand_pile
from app.llm.base import get_provider
from app.settings import REPO_ROOT


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pile", default="pile_acme")
    parser.add_argument("--domain", default="vendor_contracts")
    parser.add_argument("--out", default=None, help="write the register here")
    args = parser.parse_args()

    cfg = load_domain(args.domain)
    directory = REPO_ROOT / "corpora" / args.pile
    paths = sorted(p for p in directory.glob("*") if p.is_file())
    if not paths:
        print(f"no documents in {directory}")
        return 1

    report = understand_pile(get_provider(), cfg, paths)

    print(f"pile: {args.pile}  ({len(paths)} documents)\n")
    print("stages")
    for stage, row in report.cost_by_stage().items():
        print(f"  {stage:<12} {row['calls']:>3} calls  {row['ms']:>6} ms  "
              f"{row['tokens_in']:>7} in  {row['tokens_out']:>6} out  "
              f"${row['cost_usd']:.4f}")
    print(f"  {'TOTAL':<12} {'':>3}        {report.total_ms:>6} ms  "
          f"{report.total_usage.tokens_in:>7} in  {report.total_usage.tokens_out:>6} out  "
          f"${report.total_usage.cost_usd:.4f}")

    print("\npaths taken")
    for path, count in sorted(report.paths_taken().items()):
        print(f"  {path:<28} {count}")

    result = report.reconciliation
    print(f"\nfacts {len(report.facts)}   gaps {len(report.gaps)}   "
          f"conflicts {len(result.conflicts)}   "
          f"quarantined {len(report.quarantined)}   escalated {len(report.escalated)}")

    for name, note in report.quarantined:
        print(f"  QUARANTINED {name}: {note[:120]}")
    for name, note in report.escalated:
        print(f"  ESCALATED   {name}: {note[:120]}")

    if result.conflicts:
        print("\nconflicts (all open; nothing has been resolved)")
        for conflict in result.conflicts:
            proposed = conflict.proposed.display if conflict.proposed else "none"
            print(f"  {conflict.field:<22} {conflict.distinct_values}  proposed: {proposed}")
    else:
        print("\nno disagreements found")

    if args.out:
        target = Path(args.out)
        target.write_text(report.register.render())
        print(f"\nregister written to {target}")
        for section in report.register.sections:
            print(f"  {section.key:<16} {section.content_hash[:12]}  "
                  f"{len(section.citations):>3} citations  {section.gap_count} gaps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
