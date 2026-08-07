-- What the EXAMINE stage produced, and what it could not decide.
--
-- The original `finding` table assumed two outcomes: a finding exists or it
-- does not. Checking a real playbook against a real pile makes a third
-- necessary. A rule the pile could not answer is not a rule the pile passed,
-- and a report that cannot tell those apart says a contract is clean when what
-- actually happened is that nobody could tell. So `outcome` is stored, and
-- 'satisfied' rows are kept rather than dropped -- "eight rules were checked
-- and six held" is a different claim from "two problems were found", and only
-- the first is worth trusting.
ALTER TABLE finding
    ADD COLUMN entity_key TEXT NOT NULL DEFAULT '-',
    -- violated | satisfied | not_enough_evidence
    ADD COLUMN outcome    TEXT NOT NULL DEFAULT 'violated',
    -- What was actually found, with the numbers in it. `statement` is the rule
    -- as the playbook words it; this is what happened when it met the pile.
    ADD COLUMN detail     TEXT;

-- A rule has one current answer per engagement. Re-examining after a document
-- arrives updates that answer rather than stacking a second one beside it,
-- which is what makes "the rate violation is now resolved" visible instead of
-- leaving two contradictory findings on the pile.
ALTER TABLE finding
    ADD CONSTRAINT finding_rule_per_entity UNIQUE (pile_id, rule_key, entity_key);

CREATE INDEX finding_pile_outcome_idx ON finding (pile_id, outcome);
