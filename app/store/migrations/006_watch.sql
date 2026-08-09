-- What the folder watcher did, and what it refused to do.
--
-- The watcher is a trigger, not a pipeline: it calls the same `arrival`
-- operation the HTTP endpoint and the MCP tool call, and every consequence of a
-- dispatch is already recorded by the run it starts. This table records the two
-- things a run cannot -- what the watcher already handled, and the attempts
-- that never became runs at all.
--
-- Both matter. A finished file stays in the inbox and is "settled" on every
-- scan afterwards, so without a record of what was done with it the watcher
-- would dispatch the same bytes forever. And a file the watcher cannot process
-- otherwise disappears silently: the inbox looks handled, the register never
-- mentions the document, and nothing anywhere says why. A watcher that
-- swallows a failure is a success message that does not mean what it says.
--
-- Deliberately NOT a claim table. There is no 'dispatching' state taken before
-- the work and cleared after it, because a process killed mid-dispatch would
-- then leave a claim nobody clears and a file nobody retries. The row is
-- written after the attempt resolves, and a crash in the window before it lands
-- is covered by state that is already true: the document's content hash is
-- either in the pile or it is not. Such a crash therefore re-dispatches into an
-- ingest that is a content-addressed no-op, which is behaviour 2 holding for
-- the watcher without the watcher knowing anything about it.
CREATE TABLE IF NOT EXISTS watch_event (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pile_id           UUID REFERENCES pile(id) ON DELETE CASCADE,
    filename          TEXT NOT NULL,
    content_sha256    TEXT NOT NULL,
    -- dispatched: an arrival ran, and `run_id` names it.
    -- failed:     the arrival was refused or errored; `detail` says how, and
    --             these bytes are not tried again, because a file that fails
    --             deterministically would otherwise be retried on every tick
    --             forever.
    outcome           TEXT NOT NULL,
    run_id            UUID REFERENCES run(id) ON DELETE SET NULL,
    detail            TEXT,
    at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS watch_event_pile_idx ON watch_event (pile_id, at DESC);
CREATE INDEX IF NOT EXISTS watch_event_digest_idx ON watch_event (pile_id, content_sha256);
