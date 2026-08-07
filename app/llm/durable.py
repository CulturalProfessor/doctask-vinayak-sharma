"""A provider that remembers, so a resumed run never pays twice.

Graded behaviour 2 says a killed run continues from where it stopped, with no
finished work redone. Checkpointing gives that for the run's *position*. It does
not give it for the run's *spending*, and spending is the part that matters:

  A node that was in flight when the process died had its database writes rolled
  back, so on resume it runs again from the top. That is correct -- a half-run
  node is not finished work. But if it also re-issues its model call, the run has
  paid twice for one answer, and the cost report (behaviour 10) then overstates
  what the pile actually cost. A number that is wrong in the expensive direction
  is not a rounding error; it is the number the whole behaviour is judged on.

So every completion is written to `model_call` and committed **immediately, on
its own connection**, outside the run's transaction. That ordering is
deliberate and it is the only interesting decision in this file:

  - Commit the answer before the work that asked for it. If the run rolls back,
    the record survives, and the re-executed node is served from it.
  - The reverse ordering loses the record exactly when it is needed.

Keeping a recorded answer through a rollback is safe because the key is the
prompt itself. The same question gets the same answer; that is what `call_key`
already means for the recorded-fixture provider.

What this is *not*: a cache across runs. The key is `(run_id, call_key)`, so a
fresh run over the same documents genuinely re-asks. Resumption is a promise
about one run, and pretending a new run is free would be a different claim
entirely.
"""
from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app.llm.base import Completion, Provider, Usage, call_key
from app.settings import settings


class DurableProvider:
    """Wraps a provider and records every completion against a run.

    `issued` and `replayed` are the evidence for the resume test: after a kill
    and a restart, the two processes together must have issued exactly as many
    calls as one uninterrupted run.
    """

    def __init__(self, inner: Provider, run_id: str,
                 conn: psycopg.Connection | None = None) -> None:
        self.inner = inner
        self.name = f"durable({getattr(inner, 'name', 'provider')})"
        self.run_id = run_id
        self.issued = 0
        self.replayed = 0
        self._own_conn = conn is None
        self._conn = conn or psycopg.connect(
            settings.database_url, row_factory=dict_row, autocommit=True
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._own_conn and not self._conn.closed:
            self._conn.close()

    def __enter__(self) -> "DurableProvider":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the boundary ------------------------------------------------------

    def complete(self, *, purpose: str, system: str, user: str,
                 max_tokens: int = 2048) -> Completion:
        key = call_key(purpose, system, user)

        recorded = self._recall(key)
        if recorded is not None:
            self.replayed += 1
            return recorded

        completion = self.inner.complete(
            purpose=purpose, system=system, user=user, max_tokens=max_tokens
        )
        self.issued += 1
        self._record(purpose, key, completion)
        return completion

    # -- the ledger --------------------------------------------------------

    def _recall(self, key: str) -> Completion | None:
        with self._conn.cursor() as cur:
            cur.execute("""
                SELECT response, tokens_in, tokens_out, cost_usd, model
                FROM model_call WHERE run_id = %s AND call_key = %s
            """, (self.run_id, key))
            row = cur.fetchone()
        if row is None:
            return None
        # The recorded usage is reported as-is rather than as zero. The call was
        # made once and cost what it cost; the run should say so exactly once,
        # and the stage event that would have double-counted it was rolled back
        # with the node that wrote it.
        return Completion(
            text=row["response"],
            usage=Usage(row["tokens_in"], row["tokens_out"],
                        float(row["cost_usd"]), row["model"] or "recorded"),
        )

    def _record(self, purpose: str, key: str, completion: Completion) -> None:
        usage = completion.usage
        with self._conn.cursor() as cur:
            cur.execute("""
                INSERT INTO model_call (run_id, purpose, call_key, response,
                                        tokens_in, tokens_out, cost_usd, model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, call_key) DO NOTHING
            """, (self.run_id, purpose, key, completion.text, usage.tokens_in,
                  usage.tokens_out, usage.cost_usd, usage.model))
