-- Which surface a decision came through.
--
-- Behaviour 3 says a person holds the gate. Behaviour 4 says a machine must be
-- able to drive the whole flow, and names approval as part of it. Both are
-- required, and together they mean the system will accept an approval that
-- arrived over an interface built for programs -- including one an agent is
-- driving.
--
-- The resolution is not to pick a side. It is to make the record unable to
-- blur them. `decided_by` says who was named; `decided_via` says what actually
-- carried the decision, and is set by the surface rather than by the caller, so
-- it cannot be claimed. Anyone auditing a review afterwards can tell an
-- approval a person clicked from one an agent made on their behalf, which is
-- the question that matters and the one a single free-text field cannot answer.
ALTER TABLE proposal ADD COLUMN decided_via TEXT;

COMMENT ON COLUMN proposal.decided_via IS
    'The surface that carried the decision: http | mcp | ui | direct. Set by the '
    'surface, never by the caller.';
