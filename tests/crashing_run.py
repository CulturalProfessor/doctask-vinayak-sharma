"""A run in a child process that can be made to die where we want it to.

Used by `tests/test_resume.py`. It lives in its own module because the point of
the test is that a *different process* picks the run up, so the first one has to
be able to stop existing.

The kill is a real `SIGKILL` the process sends to itself, not an exception and
not a simulated crash. Nothing runs afterwards: no cleanup, no rollback, no
final flush. That is the only version of this test worth having, because every
weaker one silently gives the system a chance to tidy up on the way out, and
tidying up on the way out is exactly what a killed process does not get to do.

Where it dies is chosen by patching a production function in this child, which
is the test choosing a kill point rather than the application carrying a
die-here hook it would never use in anger. Two points matter:

    --die-at-model-call N   before the Nth model call. The node in flight has
                            written nothing worth keeping; resume should redo
                            it and pay for it, once.
    --die-at-persist N      inside the Nth extract, after its model call has
                            been made and recorded but before the node's writes
                            commit. This is the case the model-call ledger
                            exists for: the answer was bought, the work that
                            asked for it was not. Resume must reuse the answer.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys


def _die() -> None:
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def _arm_model_call_kill(nth: int) -> None:
    from app.llm import fake

    original = fake.FakeProvider.complete
    calls = {"n": 0}

    def counted(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == nth:
            _die()
        return original(self, **kwargs)

    fake.FakeProvider.complete = counted


def _arm_persist_kill(nth: int) -> None:
    from app.store import repository as repo

    original = repo.persist_facts
    calls = {"n": 0}

    def counted(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == nth:
            _die()
        return original(*args, **kwargs)

    repo.persist_facts = counted
    # The node imported the module, not the name, so patching the module
    # attribute is enough -- but assert it rather than trust it, because a test
    # whose kill never fires passes for the wrong reason.
    assert repo.persist_facts is counted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pile", required=True)
    parser.add_argument("--corpus", default="pile_acme")
    parser.add_argument("--resume", default=None, metavar="RUN_ID")
    parser.add_argument("--die-at-model-call", type=int, default=0)
    parser.add_argument("--die-at-persist", type=int, default=0)
    args = parser.parse_args()

    if args.die_at_model_call:
        _arm_model_call_kill(args.die_at_model_call)
    if args.die_at_persist:
        _arm_persist_kill(args.die_at_persist)

    from app.domain.config import load_domain
    from app.graph import pipeline
    from app.llm.fake import FakeProvider
    from app.settings import REPO_ROOT

    cfg = load_domain("vendor_contracts")

    if args.resume:
        result = pipeline.resume(FakeProvider(), cfg, run_id=args.resume)
    else:
        directory = REPO_ROOT / "corpora" / args.corpus
        paths = sorted(p for p in directory.glob("*") if p.is_file())
        result = pipeline.run_understand(FakeProvider(), cfg, args.pile, paths)

    print(json.dumps({
        "run_id": result.run_id,
        "status": result.status,
        "issued": result.model_calls,
        "replayed": result.replayed_calls,
        "facts": result.fact_count,
        "proposals": len(result.gate.proposals),
        "hashes": {s.key: s.content_hash for s in (result.register.sections
                                                   if result.register else [])},
        "committed": result.committed,
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
