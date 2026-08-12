/**
 * The pieces a reviewer actually looks at.
 *
 * Four rules shape every component in this file.
 *
 * **Show the evidence next to the decision.** A reviewer asked to approve
 * "hourly_rate: 2 distinct values" is not reviewing anything. A reviewer shown
 * USD 120 quoted from invoice_1043.txt at characters 484-491, beside USD 135
 * from amendment_01.md, is.
 *
 * **Never let the screen imply something is settled when it is not.** A
 * proposed conflict resolution is drawn as a dashed card labelled "suggested,
 * not applied", under a line saying nothing has been written. A similarity
 * score is drawn as the reason a question was asked, never as its answer.
 *
 * **Write for a contracts analyst, not for the person who built this.** No
 * spans, no payloads, no hashes, no operations, no surfaces, no checkpoints.
 * "Is this the same company?" rather than "entity resolution escalation";
 * "checked again and identical, not skipped" rather than "unchanged"; "the
 * passage it came from" rather than "the span". Every word of implementation
 * vocabulary that reaches the screen is a word the reader has to translate
 * before they can do their job, and the ones that sound self-explanatory are
 * the worst of them because they get translated wrong.
 *
 * Comments in this file are for engineers and stay technical. Strings are not.
 */
import { useRef, useState } from "react";

/** One cited passage: the text, then where it came from.
 *
 * Quote first and provenance second, because the quote is what is being read
 * and the citation is what is being checked. Monospace source text (invoice
 * tables, mostly) keeps its own whitespace: a rate table reflowed as prose is
 * unreadable, and these are the documents where the columns carry the meaning.
 */
export function Span({ citation, mono, extra }) {
  const text = citation.quote ?? citation.text ?? "";
  const looksTabular = mono ?? /\n.*\s{3,}\S/.test(text);
  return (
    <div className="span">
      {looksTabular ? <pre className="quote">{text}</pre> : <div className="quote">{text}</div>}
      <div className="cite">
        <span className="doc">{citation.document}</span>
        {citation.doc_type && <span className="pill">{citation.doc_type}</span>}
        <span className="offsets">
          {citation.page_no ? `page ${citation.page_no}, ` : ""}
          characters {citation.char_start}–{citation.char_end}
        </span>
        <span className="spacer" />
        {citation.value && <span className="val">{citation.value}</span>}
        {extra}
      </div>
    </div>
  );
}

function Subhead({ children, aside }) {
  return (
    <div className="subhead">
      <span className="cap">{children}</span>
      <span className="line" />
      {aside && <span className="aside">{aside}</span>}
    </div>
  );
}

/** The two answers, as full sentences on full-width buttons.
 *
 * Deliberately not an icon pair, and deliberately not preselected. The label
 * says what approving *does* rather than repeating the word "approve", because
 * the reviewer's question is "what happens if I click this", and a screen that
 * answers it is a screen where clicking through costs the same as reading.
 */
function Choice({ verdict, onVerdict, yes, yesSub, no, noSub }) {
  return (
    <div className="decide-block">
      <Subhead>Your decision</Subhead>
      <div className="choices">
        <button
          className={verdict === true ? "approve on" : "approve"}
          onClick={() => onVerdict(verdict === true ? null : true)}
        >
          {yes}
          {yesSub && <span className="sub">{yesSub}</span>}
        </button>
        <button
          className={verdict === false ? "reject on" : "reject"}
          onClick={() => onVerdict(verdict === false ? null : false)}
        >
          {no}
          {noSub && <span className="sub">{noSub}</span>}
        </button>
      </div>
    </div>
  );
}

function Decided({ proposal }) {
  const how =
    { http: "through the web interface", mcp: "by a program", direct: "directly" }[
      proposal.decided_via
    ] ?? proposal.decided_via;
  return (
    <div className={`decided ${proposal.status}`}>
      {proposal.status === "approved" ? "Approved" : "Rejected"}
      {proposal.decided_by && <> by {proposal.decided_by}</>}
      {/* Who decided is one claim; how they reached the system is another, and
          an approval a program made must never read as one a person clicked. */}
      {how && <>, {how}</>}
      {proposal.reason && <div className="reason">“{proposal.reason}”</div>}
    </div>
  );
}

/* ------------------------------------------------------------- the items --
 *
 * Each of these returns `{ primary, evidence }` rather than one blob of JSX,
 * and the split is the layout.
 *
 * A reviewer's two questions are "what is being claimed" and "what does the
 * document actually say", and on a wide screen there is room to answer both at
 * once. Stacked in one column they were sequential: the reasoning scrolled away
 * as soon as the quotes appeared, and the right half of a wide window sat empty
 * while the reader scrolled. Side by side, the claim, the values in tension and
 * the decision stay put on the left while the evidence runs down the right.
 *
 * An item with no quotes (a register section) returns no `evidence`, and the
 * pane falls back to one wider column, because a body of monospace text wants
 * width rather than a second column of nothing.
 */

function conflictBody(payload) {
  const groups = new Map();
  for (const member of payload.members) {
    if (!groups.has(member.value)) groups.set(member.value, []);
    groups.get(member.value).push(member.document);
  }
  const letters = ["A", "B", "C", "D", "E"];

  return {
    primary: (
      <>
        <p className="lede">
          {payload.members.length} document{payload.members.length === 1 ? "" : "s"} give a value
          for <b>{payload.field}</b>. They give {groups.size} different answers, and they do not
          agree on which one applies.
        </p>

        <div className="values">
          {[...groups.entries()].map(([value, docs], i) => (
            <div className="value-card" key={value}>
              <div className="label">Value {letters[i] ?? i + 1}</div>
              <div className="value">{value}</div>
              <div className="where">{docs.join(", ")}</div>
            </div>
          ))}
          {/* Dashed, and labelled with what it is not. The ranking of document
              types proposes; the reviewer resolves. Drawing this like the
              values above would make the system look as though it had already
              chosen. */}
          <div className="value-card proposed">
            <div className="label">Suggested, not applied</div>
            <div className="value">{payload.proposed ?? "none"}</div>
            <div className="where">
              {payload.proposed
                ? `from ${payload.proposed_from}`
                : "the documents do not settle this"}
            </div>
          </div>
        </div>

        <p className="rationale">
          {payload.rationale}{" "}
          <span className="unresolved">Still open. Nothing has been decided or written.</span>
        </p>
      </>
    ),
    evidence: (
      <>
        <Subhead aside={`${payload.members.length} quotes`}>What each document says</Subhead>
        {payload.members.map((member, i) => (
          <Span key={i} citation={member} />
        ))}
      </>
    ),
  };
}

function findingBody(payload) {
  const cited = payload.citations ?? [];
  return {
    primary: (
      <>
        <p className="lede">{payload.statement}</p>
        <div className="detail-text">{payload.detail}</div>
        {cited.length === 0 && (
          <p className="rationale">
            Nothing is quoted here, and that is right rather than missing. This finding is about a
            document that was set aside and never read, so it produced no quotes to show. The
            document itself is the evidence, and the finding names it.
          </p>
        )}
      </>
    ),
    evidence:
      cited.length > 0 ? (
        <>
          <Subhead aside={`${cited.length} quotes`}>The evidence</Subhead>
          {cited.map((citation, i) => (
            <Span key={i} citation={citation} />
          ))}
        </>
      ) : null,
  };
}

function sectionBody(payload) {
  return {
    // No second column: a body of monospace text wants width, not a neighbour.
    wide: true,
    primary: (
      <>
        <p className="lede">
          This is the text that will be added to the register if you approve it, exactly as it will
          appear. It is checked again at the moment of writing, and refused if anything about it has
          moved since you read it.
        </p>
        <Subhead>Section text</Subhead>
        <pre className="section-body">{payload.body}</pre>
      </>
    ),
    evidence: null,
  };
}

/** Raised about the whole reading rather than about one item. */
function escalationBody(payload) {
  return {
    primary: (
      <>
        <p className="lede">
          This reading stopped and raised itself instead of filling the list with individual items.
          Past a certain number, disagreements stop being reviewable one at a time, and the reading
          as a whole is what needs attention.
        </p>
        <div className="values">
          <div className="value-card">
            <div className="label">Open disagreements</div>
            <div className="value">{payload.conflicts ?? "not counted"}</div>
            <div className="where">found this time</div>
          </div>
          <div className="value-card">
            <div className="label">Limit</div>
            <div className="value">{payload.ceiling ?? "not set"}</div>
            <div className="where">set in the domain settings</div>
          </div>
        </div>
        {payload.note && <p className="rationale">{payload.note}</p>}
      </>
    ),
    evidence: null,
  };
}

export function Detail({ proposal, verdict, onVerdict, index, total }) {
  const payload = proposal.payload ?? {};
  const pending = proposal.status === "pending";

  // `build` is a function, not an already-built object. Building all four
  // eagerly ran the conflict builder over a finding's payload, which has no
  // `members`, and one thrown TypeError blanked the entire review desk. JSX
  // elements were lazy and hid this; plain calls are not.
  const KINDS = {
    conflict: {
      eyebrow: "Documents disagree",
      title: payload.field,
      mono: true,
      build: conflictBody,
      yes: "Approve the suggested value",
      yesSub: "it goes into the register when you save",
      no: "Reject, leave it open",
      noSub: "it stays on the list rather than disappearing",
    },
    finding: {
      eyebrow: "Rule check",
      title: payload.rule_key,
      mono: true,
      build: findingBody,
      yes: "Accept this finding",
      yesSub: "it is reported in the register",
      no: "Reject it",
      noSub: "it is recorded as rejected, not deleted",
    },
    section_patch: {
      eyebrow: "Register section",
      title: payload.section_key,
      mono: true,
      build: sectionBody,
      yes: "Approve this section",
      yesSub: "written exactly as shown",
      no: "Reject this section",
      noSub: "the version already saved is kept",
    },
    escalation: {
      eyebrow: "Needs your attention",
      title: "Too many disagreements to review one by one",
      build: escalationBody,
      yes: "Acknowledge and continue",
      yesSub: "the reading carries on to its review",
      no: "Reject",
      noSub: "nothing from this reading is written",
    },
  };
  const spec = KINDS[proposal.kind] ?? {
    eyebrow: proposal.kind,
    title: proposal.summary,
    build: () => ({ primary: <p className="lede">{proposal.summary}</p>, evidence: null }),
    yes: "Approve",
    no: "Reject",
  };
  // Built only for the kind actually being shown.
  const { primary, evidence, wide } = spec.build(payload);

  return (
    <div className="detail-scroll">
      <div className={`detail-inner${evidence ? " split" : wide ? " wide" : ""}`}>
        <div className="col-main">
          <div className="eyebrow">
            <span className="kind">{spec.eyebrow}</span>
            <span>
              item {index + 1} of {total}
            </span>
            {payload.severity && (
              <span className={`sev-${payload.severity}`}>{payload.severity}</span>
            )}
          </div>
          <h2 className={spec.mono ? "mono" : ""}>{spec.title}</h2>
          {primary}
          {pending ? (
            <Choice
              verdict={verdict}
              onVerdict={onVerdict}
              yes={spec.yes}
              yesSub={spec.yesSub}
              no={spec.no}
              noSub={spec.noSub}
            />
          ) : (
            <Decided proposal={proposal} />
          )}
        </div>
        {evidence && <div className="col-evidence">{evidence}</div>}
      </div>
    </div>
  );
}

/**
 * A document held back because its company name may already be on file.
 *
 * Rendered as a comparison and **not** as a decision, and the difference is not
 * cosmetic. Name matching turned what used to be a silent split into a stated
 * question, but the answer is not something this screen can record: the hold is
 * not a reviewable item, so there are no buttons here that would do anything.
 * Drawing two buttons that write nothing would be the exact failure this system
 * is built against, a control that implies a capability the code does not have.
 *
 * So it shows everything that is actually known, and then says plainly what
 * resolving it takes.
 */
export function NearMatch({ escalations }) {
  if (!escalations?.length) return null;
  return (
    <>
      <div className="eyebrow">
        <span className="kind">Held back</span>
        <span>
          {escalations.length} document{escalations.length === 1 ? "" : "s"}
        </span>
      </div>
      <h2>Is this the same company?</h2>
      <p className="lede">
        These documents name a company under a spelling this pile has not seen before. Filing one as
        a new company would split the record in two and hide any disagreement between the halves.
        Merging them is not something a close-looking name can settle. So they are held back, and
        nothing from them reaches the register until someone says which company they belong to.
      </p>
      {escalations.map((row, i) => (
        <div className="span" key={i}>
          <div className="quote">{row.note || "held back, with no reason recorded"}</div>
          <div className="cite">
            <span className="doc">{row.document}</span>
            {row.stage && <span className="pill">{row.stage}</span>}
          </div>
        </div>
      ))}
      <Subhead>How to resolve it</Subhead>
      <p className="rationale">
        The answer is recorded in the domain settings rather than by a click, so that it holds for
        the next document and the next reading too. Add the spelling to the list of known
        alternative names for that company in{" "}
        <code>config/domains/vendor_contracts/reconciliation.yaml</code>, then read the pile again.
        A name written there is treated as a decision a person made and beats anything the system
        works out on its own. If the companies really are different, nothing needs doing.
      </p>
      <p className="rationale unresolved">
        There is no button here on purpose. Nothing on this screen can settle this, and a control
        that wrote nothing would be worse than none.
      </p>
    </>
  );
}

// ------------------------------------------------------------------ sources --

/* Above this a hit is worth reading as a match; below it, it cleared the
 * server's noise floor and not much else. Two lines rather than one, because a
 * single threshold has to choose between hiding real-but-weak matches and
 * presenting weak ones as answers, and neither is acceptable. Everything above
 * the server's floor is shown; anything under this one is labelled. */
const CONFIDENT_MATCH = 0.35;

export function Sources({ result, query, onQuery, onSearch, busy }) {
  const hits = result?.results ?? [];
  const index = result?.index;
  const best = hits.length ? Math.max(...hits.map((h) => Number(h.similarity))) : 0;
  const weak = result && (hits.length === 0 || best < CONFIDENT_MATCH);
  return (
    <div className="sheet">
      <div className="sheet-inner">
        <div className="eyebrow">
          <span className="kind">Sources</span>
        </div>
        <h2>Check what the documents say</h2>
        <p className="lede">
          Looks through the documents that have been read and returns the passages that match, each
          one showing the document, page and exact position it came from. It does not answer the
          question, and nothing found here goes into the register by itself.
        </p>

        <form
          className="searchbar"
          onSubmit={(e) => {
            e.preventDefault();
            onSearch();
          }}
        >
          <input
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder="a phrase you expect the contracts to use"
          />
          <button type="submit" disabled={!query.trim() || !!busy}>
            Search
          </button>
        </form>

        {index && (
          <div className="index-grid">
            <div>
              <div className="k">Passages read</div>
              <div className="v">{index.spans}</div>
            </div>
            <div>
              <div className="k">Searchable</div>
              <div className="v">
                {index.spans_embedded}
                {index.spans - index.spans_embedded > 0 && (
                  <small> ({index.spans - index.spans_embedded} cannot be searched)</small>
                )}
              </div>
            </div>
            <div>
              <div className="k">Companies known</div>
              <div className="v">{index.engagements_indexed}</div>
            </div>
            <div>
              <div className="k">Matches on</div>
              <div className="v" style={{ fontSize: 12.5 }}>
                wording, not meaning
              </div>
            </div>
          </div>
        )}

        {/* The honest empty state, and the nearly-empty one.
            Amber rather than red, and worded as a limit of the search rather
            than a fact about the contracts: "we found nothing" and "there is
            nothing" are different claims and only the first is true. The second
            branch matters as much, because a list of barely-matching passages
            under the heading "passages found" reads as an answer. */}
        {weak && (
          <div className="weak">
            <div className="head">
              {hits.length === 0
                ? `Nothing in these documents is worded like “${result.query}”.`
                : `Nothing here closely matches “${result.query}”.`}
            </div>
            <p>
              {hits.length > 0 && (
                <>
                  The closest passage scores {best.toFixed(2)}, which is a loose overlap of words
                  rather than a match worth relying on.{" "}
                </>
              )}
              {hits.length > 0 ? "Either way that is" : "That is"} weak evidence, not a finding. The
              search matches wording rather than meaning, so a clause that says this in other words
              would not be found. Nothing here supports the claim that these contracts do not cover
              it.
            </p>
          </div>
        )}

        {hits.length > 0 && (
          <>
            <Subhead aside={`${hits.length} passages, not an answer`}>
              {weak ? "Loose overlaps" : "Passages found"}
            </Subhead>
            <div className="hit-grid">
              {hits.map((hit) => (
                <Span
                  key={hit.id}
                  citation={hit}
                  extra={<span className="sim">match {Number(hit.similarity).toFixed(2)}</span>}
                />
              ))}
            </div>
          </>
        )}

        {result && (
          <p className="disclaimer">
            These are sources, not an answer. The search matches wording rather than meaning, so
            finding nothing is weak evidence that there is nothing to find.
          </p>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- watch --

export function Watch({ watch }) {
  if (!watch) return null;
  const events = watch.events ?? [];
  const pending = watch.pending_files ?? [];
  // A file sitting in the folder with no event is the failure worth noticing:
  // the watcher has not seen it, has not failed it, and has not deferred it.
  // Everything else in this panel is history; this is the alarm.
  const unaccounted = pending.filter((name) => !events.some((e) => e.filename === name));
  const WORDS = {
    dispatched: "read",
    failed: "could not be read",
    busy: "waiting its turn",
    no_pile: "waiting for a pile",
  };

  return (
    <div className="sheet">
      <div className="sheet-inner">
        <div className="eyebrow">
          <span className="kind">Watched folder</span>
        </div>
        <h2>
          {!watch.enabled
            ? "Nothing is watching this folder"
            : unaccounted.length
              ? `${unaccounted.length} file${unaccounted.length === 1 ? "" : "s"} waiting with no result`
              : "Everything that arrived has an answer"}
        </h2>
        <p className="lede">
          A document dropped into this folder is picked up and read on its own, and stops for your
          review like every other document. A file that sits here and never gets an answer is the
          thing worth noticing. A file that could not be read is not tried again.
        </p>

        <div className="watch-strip">
          <span className={watch.enabled ? "on-dot" : "off-dot"}>
            {watch.enabled ? "watching" : "off"}
          </span>
          <span>
            folder <b>{watch.directory}</b>
          </span>
          <span>
            into pile <b>{watch.pile}</b>
          </span>
          <span>
            checked every <b>{watch.interval_seconds}s</b>
          </span>
          {!watch.directory_exists && (
            <span style={{ color: "var(--red)" }}>the folder does not exist</span>
          )}
        </div>

        {watch.note && <p className="rationale">{watch.note}</p>}

        {unaccounted.length > 0 && (
          <div className="weak">
            <div className="head">Waiting in the folder</div>
            <p>
              {unaccounted.join(", ")}. Nothing has happened to{" "}
              {unaccounted.length === 1 ? "it" : "them"} yet.
              {watch.enabled
                ? " Give it a moment: a file is left alone until it has stopped changing, so a document still being copied in is never read half-written."
                : " Nothing is watching this folder, so nothing will pick it up."}
            </p>
          </div>
        )}

        <Subhead aside="most recent first">What has arrived</Subhead>
        {events.length === 0 ? (
          <p className="rationale">Nothing has come through this folder yet.</p>
        ) : (
          <div className="rows">
            <div className="r head">
              <span>Result</span>
              <span>File and reason</span>
              <span>Reading</span>
              <span>At</span>
            </div>
            {events.map((event, i) => (
              <div className="r" key={i}>
                <span className={`outcome ${event.outcome}`}>
                  {WORDS[event.outcome] ?? event.outcome}
                </span>
                <span>
                  <span className="file">{event.filename}</span>
                  {event.detail && <div className="why">{event.detail}</div>}
                </span>
                <span className="rid">
                  {event.run_id ? String(event.run_id).slice(0, 8) : "none"}
                </span>
                <span className="at">
                  {new Date(event.at).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// -------------------------------------------------------------- the sheets --

export function Findings({ findings }) {
  const order = { violated: 0, not_enough_evidence: 1, satisfied: 2 };
  const rows = [...(findings?.findings ?? [])].sort(
    (a, b) => order[a.outcome] - order[b.outcome] || a.rule_key.localeCompare(b.rule_key),
  );
  const counts = findings?.counts ?? {};
  const violations = counts.violated ?? 0;
  const WORDS = {
    violated: "broken",
    satisfied: "holds",
    not_enough_evidence: "cannot be answered",
  };

  return (
    <div className="sheet">
      <div className="sheet-inner">
        <div className="eyebrow">
          <span className="kind">Rule checks</span>
        </div>
        <h2>
          {violations > 0
            ? `${violations} rule${violations === 1 ? "" : "s"} broken`
            : rows.length
              ? "No rules broken"
              : "Nothing checked yet"}
        </h2>
        <p className="lede">
          Every rule's answer, not only the broken ones. A rule that <em>cannot be answered</em> is
          not a rule that <em>holds</em>. A rule the documents could not answer is not a rule they
          passed, and treating the two the same is how a report comes to call a contract clean when
          what happened is that nobody could tell.
        </p>

        {rows.length > 0 && (
          <div className="tally">
            {["violated", "not_enough_evidence", "satisfied"].map((key) => (
              <span key={key} className={key}>
                <b>{counts[key] ?? 0}</b> {WORDS[key]}
              </span>
            ))}
          </div>
        )}

        <div className="finding-grid">
          {rows.map((finding) => (
            <div key={finding.id} className={`finding ${finding.outcome}`}>
              <div className="finding-head">
                <span className="outcome-text">{WORDS[finding.outcome]}</span>
                <span className="rule">{finding.rule_key}</span>
                <span className="pill">{finding.severity}</span>
              </div>
              <div className="detail">{finding.detail}</div>
              {finding.citations?.length > 0 &&
                finding.citations.map((citation, i) => <Span key={i} citation={citation} />)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function Register({ register }) {
  return (
    <div className="sheet">
      <div className="sheet-inner">
        <div className="eyebrow">
          <span className="kind">Register</span>
        </div>
        <h2>The finished register</h2>
        <p className="lede">
          Every value here can be traced back to the passage in the document it came from, and
          anything that could not be established is written down as a gap rather than left blank.
          This is the version a person approved, not the version the system suggested.
        </p>
        {register.sections.map((section) => (
          <div key={section.section_key} className="section">
            <div className="section-head">
              <span className="key">{section.section_key}</span>
            </div>
            <pre className="section-body">{section.body}</pre>
          </div>
        ))}
      </div>
    </div>
  );
}

export function Audit({ audit }) {
  return (
    <div className="sheet">
      <div className="sheet-inner">
        <div className="eyebrow">
          <span className="kind">History</span>
        </div>
        <h2>What changed, and because of which document</h2>
        <p className="lede">
          One row for each section that actually changed. Sections that stayed the same get no row,
          because nothing happened to them and a row would suggest otherwise.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Section</th>
                <th>Because of</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {audit.audit.map((row, i) => (
                <tr key={i}>
                  <td className="mono">{row.section_key}</td>
                  <td>{row.cause_document ?? "a full reading of the pile"}</td>
                  <td>{new Date(row.committed_at).toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export function Stages({ report }) {
  const total = report.totals;
  return (
    <div className="sheet">
      <div className="sheet-inner">
        <div className="eyebrow">
          <span className="kind">Cost</span>
        </div>
        <h2>What this reading spent, step by step</h2>
        <p className="lede">
          Added up from what the AI provider reported. The saved answers this demo replays came from
          a model priced at zero, so a demo reading costs nothing. The measurement is real; the
          amounts are not evidence of anything. <b>Which way it went</b> records the choice each
          step made, which is what makes a reading checkable rather than merely timed.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Step</th>
                <th>Times</th>
                <th>ms</th>
                <th>Sent</th>
                <th>Received</th>
                <th>Cost</th>
                <th>Which way it went</th>
              </tr>
            </thead>
            <tbody>
              {report.stages.map((row) => (
                <tr key={row.stage}>
                  <td className="stage">{row.stage}</td>
                  <td className="num">{row.calls}</td>
                  <td className="num">{row.ms ?? 0}</td>
                  <td className="num">{row.tokens_in}</td>
                  <td className="num">{row.tokens_out}</td>
                  <td className="num">${Number(row.cost_usd).toFixed(4)}</td>
                  <td>
                    {row.paths.map((path) => (
                      <span key={path} className="path">
                        {path}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td>total</td>
                <td />
                <td className="num">{total.ms}</td>
                <td className="num">{total.tokens_in}</td>
                <td className="num">{total.tokens_out}</td>
                <td className="num">${Number(total.cost_usd).toFixed(4)}</td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ picking --

function bytes(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const READABLE = [".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx", ".text"];

/**
 * Choose a folder to read, or a document to add.
 *
 * Two ways in, because there are genuinely two situations. A document already
 * sitting on the server gets picked from a list; a document on the reviewer's
 * own machine gets sent. Offering only the first made the tool useless for the
 * thing anyone actually wants to do with it, which is read a contract they have
 * just been emailed.
 *
 * This replaced a text box the reviewer typed a path into, which asked them to
 * already know an answer the screen was in a position to give. Someone guessing
 * at a filename from memory eventually gets an error and cannot tell whether
 * they mistyped it or the document is gone.
 *
 * Unreadable files are listed and disabled rather than hidden. A list that
 * silently omits a file leaves someone hunting for a document that is right
 * there; one that shows it greyed out with the reason has already answered the
 * question.
 */
export function Picker({
  mode,
  corpora,
  value,
  onValue,
  localFile,
  onLocalFile,
  onConfirm,
  onCancel,
  busy,
  limitMb,
}) {
  const isFolder = mode === "folder";
  const [over, setOver] = useState(false);
  const [rejected, setRejected] = useState(null);
  const fileInput = useRef(null);

  const folders = corpora?.folders ?? [];
  const files = corpora?.files ?? [];
  const grouped = files.reduce((acc, f) => {
    (acc[f.folder] ||= []).push(f);
    return acc;
  }, {});

  const chosen = isFolder
    ? folders.find((f) => f.name === value)
    : files.find((f) => f.path === value);
  const ready = localFile
    ? true
    : Boolean(chosen) && (isFolder ? chosen.readable > 0 : chosen.supported);

  /* Checked here as well as on the server, and the reason is the wait rather
     than the security. Sending a 40 MB video and being told two minutes later
     that the format is refused is a worse experience than being told
     immediately; the server still refuses it, because a check in a browser is
     not a check. */
  function take(file) {
    if (!file) return;
    const dot = file.name.lastIndexOf(".");
    const ext = dot < 0 ? "" : file.name.slice(dot).toLowerCase();
    if (!READABLE.includes(ext)) {
      setRejected(
        `${file.name} is not a kind of document this reads. ` +
          `It handles ${READABLE.filter((e) => e !== ".text" && e !== ".markdown" && e !== ".htm")
            .map((e) => e.slice(1))
            .join(", ")}.`,
      );
      return;
    }
    if (limitMb && file.size > limitMb * 1024 * 1024) {
      setRejected(`${file.name} is ${bytes(file.size)}. The limit is ${limitMb} MB.`);
      return;
    }
    setRejected(null);
    onValue("");
    onLocalFile(file);
  }

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div className="dialog wide" role="dialog" aria-modal="true">
        <div className="head">
          <h3>{isFolder ? "Read the pile" : "Add a document"}</h3>
          <p>
            {isFolder
              ? "Reads every document in the folder, pulls out the facts with the exact place each one came from, checks them against your rules, and builds the register. It stops before writing anything and waits for you."
              : "Reads one new document and builds the register again. Sections that come out the same are reported as unchanged, meaning they were worked out again and matched, not that they were skipped."}
          </p>
        </div>

        {!isFolder && (
          <div
            className={`drop${over ? " over" : ""}${localFile ? " has" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setOver(false);
              take(e.dataTransfer.files?.[0]);
            }}
          >
            <input
              ref={fileInput}
              type="file"
              accept={READABLE.join(",")}
              hidden
              onChange={(e) => {
                take(e.target.files?.[0]);
                e.target.value = "";
              }}
            />
            {localFile ? (
              <>
                <span className="drop-name">{localFile.name}</span>
                <span className="drop-meta">{bytes(localFile.size)} from your computer</span>
                <button type="button" className="link" onClick={() => onLocalFile(null)}>
                  Remove
                </button>
              </>
            ) : (
              <>
                <span className="drop-name">Drop a document here</span>
                <span className="drop-meta">or</span>
                <button type="button" className="link" onClick={() => fileInput.current?.click()}>
                  choose one from your computer
                </button>
              </>
            )}
          </div>
        )}
        {rejected && <div className="picker-error">{rejected}</div>}

        <div className="picker">
          {!isFolder && <div className="cap picker-divider">or one already here</div>}
          {!corpora && <p className="picker-note">Looking…</p>}
          {corpora && (isFolder ? folders : Object.entries(grouped)).length === 0 && (
            <p className="picker-note">
              {isFolder
                ? "No folders of documents are set up yet."
                : "Nothing is stored here yet. Send a document from your computer above."}
            </p>
          )}

          {isFolder &&
            folders.map((folder) => (
              <button
                key={folder.name}
                type="button"
                className={`pick${value === folder.name ? " on" : ""}`}
                disabled={folder.readable === 0}
                onClick={() => onValue(folder.name)}
              >
                <span className="pick-name">{folder.name}</span>
                <span className="pick-meta">
                  {folder.files === 0
                    ? "empty"
                    : folder.readable === folder.files
                      ? `${folder.files} document${folder.files === 1 ? "" : "s"}`
                      : `${folder.readable} of ${folder.files} can be read`}
                </span>
              </button>
            ))}

          {!isFolder &&
            Object.entries(grouped).map(([folder, entries]) => (
              <div className="pick-group" key={folder}>
                <div className="cap">{folder}</div>
                {entries.map((file) => (
                  <button
                    key={file.path}
                    type="button"
                    className={`pick${value === file.path ? " on" : ""}`}
                    disabled={!file.supported}
                    title={file.note ?? ""}
                    onClick={() => {
                      onLocalFile(null);
                      onValue(file.path);
                    }}
                  >
                    <span className="pick-name">{file.name}</span>
                    <span className="pick-meta">
                      {file.supported ? (
                        <>
                          {file.format}, {bytes(file.bytes)}
                        </>
                      ) : (
                        <span className="bad">cannot be read</span>
                      )}
                    </span>
                  </button>
                ))}
              </div>
            ))}
        </div>

        <div className="foot">
          <span className="chosen">
            {localFile ? (
              <>
                sending <b>{localFile.name}</b>
              </>
            ) : chosen ? (
              <code>{isFolder ? `${value}/` : value}</code>
            ) : (
              <span className="hint">nothing chosen</span>
            )}
          </span>
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={!ready || !!busy} onClick={onConfirm}>
            {isFolder ? "Read the pile" : localFile ? "Send and read" : "Read this document"}
          </button>
        </div>
      </div>
    </div>
  );
}

/* Ending a run that will never finish.
 *
 * Written to be un-scary, because it is not a destructive action and reading it
 * as one would be the wrong lesson. The panel says plainly what stays, since
 * every other button of this shape in every other tool deletes something, and
 * someone who has been trained by those buttons will assume this one does too.
 *
 * A name and a reason are both required, matching the review gate. This is the
 * one action whose whole justification is that the pile's history is kept, so
 * it does not get to write an anonymous, unexplained line into that history. */
export function EndRun({ run, name, onName, reason, onReason, onConfirm, onCancel, busy }) {
  const ready = name.trim().length > 0 && reason.trim().length > 0;
  const undecided = run?.pending_proposals ?? 0;

  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div className="dialog" role="dialog" aria-modal="true">
        <div className="head">
          <h3>End this reading</h3>
          <p>
            For a reading that can never be finished: the document it was part way through is gone
            for good, or it should not have been started.
          </p>
        </div>

        <div className="keeps">
          <p className="keeps-lede">Nothing is deleted.</p>
          <ul>
            <li>
              The facts it pulled out stay on the pile, still pointing at the documents they came
              from.
            </li>
            <li>What it cost stays in the record, so the totals keep adding up.</li>
            <li>The documents it read stay read.</li>
            <li>
              {undecided > 0 ? (
                <>
                  Its {undecided} undecided item{undecided === 1 ? "" : "s"} stay undecided, because
                  nobody decided them. They are not marked rejected on your behalf.
                </>
              ) : (
                <>Its items keep whatever you decided about them.</>
              )}
            </li>
          </ul>
          <p className="keeps-foot">
            What changes is that it stops being work in progress, and it can no longer be continued.
          </p>
        </div>

        <label className="field">
          <span>Your name</span>
          <input
            value={name}
            onChange={(e) => onName(e.target.value)}
            placeholder="who is ending it"
          />
        </label>
        <label className="field">
          <span>Why</span>
          <input
            value={reason}
            onChange={(e) => onReason(e.target.value)}
            placeholder="so that whoever reads this pile later knows"
          />
        </label>

        <div className="foot">
          <span className="chosen">
            <span className="hint">{ready ? "recorded against your name" : "both are needed"}</span>
          </span>
          <button type="button" onClick={onCancel}>
            Keep it
          </button>
          <button type="button" className="primary" disabled={!ready || !!busy} onClick={onConfirm}>
            End it
          </button>
        </div>
      </div>
    </div>
  );
}

export function Empty({ big, children, good }) {
  return (
    <div className={`empty${good ? " good" : ""}`}>
      <p className="big">{big}</p>
      {children && <p className="hint">{children}</p>}
    </div>
  );
}

export { Subhead };
