"""Seed the demo piles so a fresh clone has something to look at.

Idempotent by construction: it ingests the same corpora every time and, because
ingest is content-addressed, a second run reports every document as a duplicate
and changes nothing. Running this on every container start is therefore safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app.ingest.ingest import ensure_pile, ingest_directory
from app.settings import REPO_ROOT
from app.store.engine import transaction

PILES = {
    "acme": ("vendor_contracts", REPO_ROOT / "corpora" / "pile_acme"),
    "northwind": ("vendor_contracts", REPO_ROOT / "corpora" / "pile_northwind"),
}


def seed() -> int:
    ingested = 0
    with transaction() as conn:
        for name, (domain, directory) in PILES.items():
            if not directory.is_dir() or not any(directory.iterdir()):
                print(f"  {name}: no corpus at {directory}, skipping")
                continue
            pile_id = ensure_pile(conn, name, domain)
            results = ingest_directory(conn, pile_id, directory)
            fresh = [r for r in results if r.accepted]
            dupes = [r for r in results if r.duplicate]
            gaps = [r for r in results if r.status in ("unsupported", "empty")]
            ingested += len(fresh)
            print(f"  {name}: {len(fresh)} new, {len(dupes)} already present, {len(gaps)} gaps")
            for gap in gaps:
                print(f"      gap: {gap.filename} -- {gap.note}")
    return ingested


if __name__ == "__main__":
    print("seeding demo piles")
    count = seed()
    print(f"seed complete ({count} documents ingested this run)")
    sys.exit(0)
