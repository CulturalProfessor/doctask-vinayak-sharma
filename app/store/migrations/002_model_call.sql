-- Every model call a run makes, recorded before it is used.
--
-- This exists for graded behaviour 2. Resuming a killed run must not redo
-- finished work, and the only genuinely expensive work here is a model call.
-- Node re-execution after a crash is unavoidable -- the node that was in flight
-- when the process died had its database writes rolled back, so it runs again.
-- Without this table it would also pay for its model call again, and the run
-- report would then say the pile cost more than it did.
--
-- Written on its own connection and committed immediately, deliberately outside
-- the run's transaction. A recorded answer is safe to keep even when the work
-- that asked for it rolls back: the question was identical, so the answer is
-- still the answer. The reverse -- losing the record when the transaction rolls
-- back -- is what costs money.
--
-- It also happens to be the audit of exactly what the model said, verbatim,
-- which nothing else in the schema holds.
CREATE TABLE model_call (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    purpose       TEXT NOT NULL,               -- classify | extract | extract_retry
    -- sha of (purpose, system, user). Identical prompts are the same call.
    call_key      TEXT NOT NULL,
    response      TEXT NOT NULL,
    tokens_in     INT NOT NULL DEFAULT 0,
    tokens_out    INT NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12, 6) NOT NULL DEFAULT 0,
    model         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The whole point: one run asking the same question twice pays once.
    UNIQUE (run_id, call_key)
);

CREATE INDEX model_call_run_idx ON model_call (run_id, created_at);
