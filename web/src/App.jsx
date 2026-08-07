/**
 * The review desk.
 *
 * This screen exists for one job: a person looks at what the system understood,
 * decides item by item, and only then does anything get written. Everything
 * else on the page -- the register, the findings, the audit trail, the cost
 * table -- is there so the reviewer can check the answer rather than take it.
 *
 * Three rules shaped it.
 *
 * 1. **The gate is a gate.** Commit stays disabled while anything is pending,
 *    and the reason is on the button. There is no "approve all" -- a control
 *    whose whole purpose is to let someone clear thirteen items without reading
 *    them would quietly undo behaviour 3.
 *
 * 2. **A review is one review.** Verdicts are collected locally and sent as a
 *    single POST /decide with mixed approvals and rejections, which is how the
 *    operation is shaped everywhere else. Deciding one item per request would
 *    leave a half-reviewed run behind on any failure.
 *
 * 3. **The screen never claims more than the server said.** Every panel is
 *    filled from a response; nothing is predicted locally. When the server
 *    refuses -- "9 proposal(s) still pending", "pile is held by run ..." -- its
 *    sentence is what appears, because the server's refusals are the useful
 *    ones.
 */
import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from './api'
import { Audit, Findings, Proposal, Register, Stages } from './components'

const TABS = ['review', 'findings', 'register', 'audit', 'cost']

/** Light, dark, or whatever the operating system says.
 *
 * Three states rather than two: "system" is the default and is not the same as
 * either fixed choice, because a reviewer who has their machine set to switch
 * at dusk should not have this one page stay bright. The choice is stamped on
 * the root element, which is what the `:root[data-theme=...]` rules in
 * styles.css key off; `system` removes the attribute and lets the media query
 * decide.
 */
const THEMES = ['system', 'light', 'dark']
const THEME_ICON = { system: '◐', light: '☀', dark: '☾' }

function applyTheme(theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  try { localStorage.setItem('doctask-theme', theme) } catch { /* private mode */ }
}

function storedTheme() {
  try { return THEMES.includes(localStorage.getItem('doctask-theme'))
    ? localStorage.getItem('doctask-theme') : 'system' } catch { return 'system' }
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [piles, setPiles] = useState([])
  const [pileId, setPileId] = useState('')
  const [documents, setDocuments] = useState(null)

  const [corpus, setCorpus] = useState('pile_acme')
  const [arrivalPath, setArrivalPath] = useState('arrivals/amendment_02.md')
  const [newPile, setNewPile] = useState('')

  const [run, setRun] = useState(null)
  const [runs, setRuns] = useState([])
  const [proposals, setProposals] = useState([])
  const [verdicts, setVerdicts] = useState({})
  const [decidedBy, setDecidedBy] = useState('')

  const [register, setRegister] = useState(null)
  const [findings, setFindings] = useState(null)
  const [audit, setAudit] = useState(null)
  const [report, setReport] = useState(null)

  const [tab, setTab] = useState('review')
  const [theme, setTheme] = useState(storedTheme)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  // --------------------------------------------------------------- loading --

  /** Run one call with the spinner, and surface the server's own words on
   *  failure rather than a generic apology. */
  const guard = useCallback(async (label, work) => {
    setBusy(label)
    setError(null)
    try {
      return await work()
    } catch (exc) {
      setError(exc instanceof ApiError ? `${exc.status} — ${exc.detail}` : String(exc))
      return undefined
    } finally {
      setBusy(null)
    }
  }, [])

  /** The pile's committed state. Each of these 404s until there is one, which
   *  is the honest answer and not an error worth shouting about. */
  const loadPile = useCallback(async (id) => {
    if (!id) return
    const [docs, reg, find, aud] = await Promise.all([
      api.listDocuments(id).catch(() => null),
      api.register(id).catch(() => null),
      api.findings(id).catch(() => null),
      api.audit(id).catch(() => null),
    ])
    setDocuments(docs)
    setRegister(reg)
    setFindings(find)
    setAudit(aud)
  }, [])

  const loadRun = useCallback(async (runId) => {
    const [list, rep] = await Promise.all([
      api.proposals(runId),
      api.report(runId).catch(() => null),
    ])
    setProposals(list.proposals)
    setReport(rep)
    setVerdicts({})
  }, [])

  /** Open a run that already exists, from the server's row rather than from a
   *  start response. The counts a start reports -- documents read, facts found,
   *  model calls made -- are facts about that invocation, and this screen does
   *  not have them, so it shows them as unknown instead of as zero. */
  const open = useCallback(async (row) => {
    setRun({ run_id: row.id, status: row.status, kind: row.kind,
             pending_proposals: Number(row.pending_proposals) })
    setVerdicts({})
    setNotice(null)
    setTab('review')
    await loadRun(row.id)
  }, [loadRun])

  useEffect(() => { applyTheme(theme) }, [theme])

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ status: 'unreachable' }))
    api.listPiles().then((body) => setPiles(body.piles)).catch(() => {})
  }, [])

  // Selecting a pile picks up whatever is already open on it.
  //
  // Before this existed the only handle on a run stopped at the gate was a
  // variable in this tab, so a refresh stranded the review -- thirteen items
  // waiting in the database with nothing able to name the run holding them.
  // Behaviour 2 says a stopped run can be picked back up; a UI that lost the
  // run id on reload was quietly not honouring that.
  useEffect(() => {
    let current = true
    if (!pileId) { setRuns([]); return undefined }
    loadPile(pileId)
    api.listRuns(pileId).then((body) => {
      if (!current) return
      setRuns(body.runs)
      const waiting = body.runs.find((r) => r.status === 'awaiting_approval')
      if (waiting) open(waiting)
    }).catch(() => {})
    return () => { current = false }
  }, [pileId, loadPile, open])

  // -------------------------------------------------------------- actions --

  async function createPile() {
    const name = newPile.trim()
    if (!name) return
    const made = await guard('creating pile', () => api.createPile(name))
    if (!made) return
    const body = await api.listPiles()
    setPiles(body.piles)
    setPileId(made.pile_id)
    setNewPile('')
    reset()
  }

  function reset() {
    setRun(null)
    setProposals([])
    setVerdicts({})
    setReport(null)
    setNotice(null)
    setTab('review')
  }

  const refreshRuns = useCallback(async (id) => {
    const body = await api.listRuns(id).catch(() => null)
    if (body) setRuns(body.runs)
  }, [])

  async function begin(kind) {
    reset()
    const result = await guard(
      kind === 'run' ? 'reading the pile' : 'reading the new document',
      () => (kind === 'run'
        ? api.startRun(pileId, corpus)
        : api.arrival(pileId, arrivalPath)),
    )
    if (!result) return
    setRun(result)
    setNotice(result.note)
    await Promise.all([loadRun(result.run_id), loadPile(pileId), refreshRuns(pileId)])
  }

  async function submitReview() {
    const decisions = Object.entries(verdicts)
      .filter(([, approved]) => approved !== null && approved !== undefined)
      .map(([proposal_id, approved]) => ({ proposal_id, approved }))
    if (!decisions.length) return
    const outcome = await guard('recording the review',
      () => api.decide(run.run_id, decisions, decidedBy.trim()))
    if (!outcome) return
    setNotice(`${outcome.approved} approved, ${outcome.rejected} rejected, `
      + `${outcome.pending} still to decide`)
    setRun((prev) => ({ ...prev, pending_proposals: outcome.pending }))
    await loadRun(run.run_id)
  }

  async function commit() {
    const done = await guard('writing the register', () => api.commit(run.run_id))
    if (!done) return
    setNotice(done.sections_written !== undefined
      ? `Committed at version ${done.version}: ${done.sections_written} section(s) written, `
        + `${done.sections_carried} carried unchanged, ${done.rejected} rejected and not written.`
      : `Run is ${done.status}.`)
    setRun((prev) => ({ ...prev, status: done.status, pending_proposals: 0 }))
    await Promise.all([loadRun(run.run_id), loadPile(pileId), refreshRuns(pileId)])
    setTab('register')
  }

  async function resume() {
    const result = await guard('resuming', () => api.resume(run.run_id))
    if (!result) return
    setRun(result)
    setNotice(result.note)
    await Promise.all([loadRun(result.run_id), loadPile(pileId), refreshRuns(pileId)])
  }

  // --------------------------------------------------------------- render --

  const pending = proposals.filter((p) => p.status === 'pending')
  const undecided = pending.filter(
    (p) => verdicts[p.id] === undefined || verdicts[p.id] === null).length
  const chosen = pending.length - undecided
  const canDecide = chosen > 0 && decidedBy.trim().length > 0
  const canCommit = run && pending.length === 0 && run.status !== 'committed'
      && proposals.length > 0

  return (
    <div className="app">
      <header className="top">
        <h1>doctask</h1>
        <span className="sub">a pile of vendor contracts, owned end to end</span>
        <span className={`health ${health?.status ?? ''}`}>
          {health ? `${health.status} · db ${health.database ?? 'unknown'}` : '…'}
        </span>
        <button
          className="theme"
          title={`Theme: ${theme}. Click to cycle system → light → dark.`}
          // Functional, for the same reason the verdicts are: two clicks before
          // a re-render would both advance from the same starting theme.
          onClick={() => setTheme((prev) =>
            THEMES[(THEMES.indexOf(prev) + 1) % THEMES.length])}
        >
          {THEME_ICON[theme]}
        </button>
      </header>

      <div className="columns">
        <aside>
          <section>
            <h2>Pile</h2>
            <select value={pileId} onChange={(e) => { setPileId(e.target.value); reset() }}>
              <option value="">— choose a pile —</option>
              {piles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.documents} docs{p.gaps ? `, ${p.gaps} not read` : ''})
                </option>
              ))}
            </select>
            <div className="row">
              <input
                placeholder="new pile name" value={newPile}
                onChange={(e) => setNewPile(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && createPile()}
              />
              <button onClick={createPile} disabled={!newPile.trim()}>Create</button>
            </div>
          </section>

          {pileId && (
            <>
              <section>
                <h2>Understand</h2>
                <label>corpus directory</label>
                <input value={corpus} onChange={(e) => setCorpus(e.target.value)} />
                <button className="primary" onClick={() => begin('run')} disabled={!!busy}>
                  Read the pile
                </button>
                <p className="hint">
                  Ingests every file under <code>corpora/{corpus || '…'}</code>, extracts
                  facts with their spans, checks the playbook, composes the register,
                  and stops at the gate. Nothing is written yet.
                </p>
              </section>

              <section>
                <h2>A document arrives</h2>
                <label>path under corpora/</label>
                <input value={arrivalPath} onChange={(e) => setArrivalPath(e.target.value)} />
                <button onClick={() => begin('arrival')} disabled={!!busy}>
                  Ingest and update
                </button>
                <p className="hint">
                  Re-reads only the new document, then re-composes. What did not move
                  is reported as unchanged because its hash was recomputed and came
                  out the same, not because it was skipped.
                </p>
              </section>

              {runs.length > 0 && (
                <section>
                  <h2>Runs ({runs.length})</h2>
                  <ul className="runs">
                    {runs.map((r) => (
                      <li
                        key={r.id}
                        className={`${r.status} ${run?.run_id === r.id ? 'on' : ''}`}
                        onClick={() => open(r)}
                      >
                        <span className="when">
                          {new Date(r.started_at).toLocaleString()}
                        </span>
                        <span className="pill">{r.kind}</span>
                        <span className={`state ${r.status}`}>
                          {r.status.replace(/_/g, ' ')}
                        </span>
                        {Number(r.pending_proposals) > 0 && (
                          <span className="bad">{r.pending_proposals} pending</span>
                        )}
                      </li>
                    ))}
                  </ul>
                  <p className="hint">
                    A run stopped at the gate belongs to the pile, not to this
                    tab. Closing the browser does not lose it.
                  </p>
                </section>
              )}

              {documents?.documents?.length > 0 && (
                <section>
                  <h2>Documents ({documents.documents.length})</h2>
                  <ul className="docs">
                    {documents.documents.map((d) => (
                      <li key={d.id} className={d.status}>
                        <span className="name">{d.filename}</span>
                        <span className="pill">{d.doc_type ?? d.format}</span>
                        {/* `is_gap` comes from the server. This used to test
                            `status !== 'ingested'` here, which labelled a
                            classified document as a gap -- the same mistake the
                            pile counts made, in a second place, which is why the
                            answer now lives in one. */}
                        {d.is_gap && (
                          <span className="bad" title={d.ingest_note ?? ''}>
                            {d.status}
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </aside>

        <main>
          {busy && <div className="banner busy">{busy}…</div>}
          {error && <div className="banner error">{error}</div>}
          {notice && <div className="banner notice">{notice}</div>}

          {!pileId && (
            <div className="empty">
              <p>Choose a pile on the left, or create one.</p>
              <p className="hint">
                This screen is one client of the same operations the HTTP API and
                the MCP server use. It has no privileged path into the system: it
                cannot commit anything a script could not, and it cannot skip the
                gate.
              </p>
            </div>
          )}

          {run && (
            <div className="runbar">
              <span className={`status ${run.status}`}>{run.status?.replace(/_/g, ' ')}</span>
              {/* Only what this screen was actually told. A run opened from the
                  runs list has no document or fact count here, and showing zero
                  would be a made-up number in the one place a reviewer looks to
                  see how much the system read. */}
              {run.documents !== undefined && (
                <>
                  <span className="stat"><b>{run.documents}</b> documents</span>
                  <span className="stat"><b>{run.facts}</b> facts</span>
                  <span className="stat"><b>{run.conflicts}</b> conflicts</span>
                  <span className="stat"><b>{run.gaps}</b> gaps</span>
                  <span className="stat"><b>{run.model_calls}</b> model calls
                    {run.replayed_calls ? ` (${run.replayed_calls} replayed)` : ''}</span>
                </>
              )}
              {run.quarantined?.length > 0 && (
                <span className="stat bad">{run.quarantined.length} quarantined</span>
              )}
              <span className="runid" title={run.run_id}>{run.run_id?.slice(0, 8)}</span>
              {run.status === 'running' && (
                <button onClick={resume} disabled={!!busy}>Resume</button>
              )}
            </div>
          )}

          {/* The third movement's actual claim. `unchanged` is the interesting
              half: those sections were recomposed from scratch and hashed to
              the same bytes, which is evidence. A section skipped because we
              guessed it would not move would only be an assumption. */}
          {run?.unchanged !== undefined && (
            <div className="delta">
              {run.added.length > 0 && (
                <span><b>added</b> {run.added.join(', ')}</span>
              )}
              {run.changed.length > 0 && (
                <span><b>changed</b> {run.changed.join(', ')}</span>
              )}
              {run.unchanged.length > 0 && (
                <span className="same">
                  <b>unchanged</b> {run.unchanged.join(', ')} — recomputed and
                  identical, not skipped
                </span>
              )}
            </div>
          )}

          {run && (
            <nav className="tabs">
              {TABS.map((name) => (
                <button
                  key={name}
                  className={tab === name ? 'on' : ''}
                  onClick={() => setTab(name)}
                >
                  {name === 'review' ? `review (${pending.length})` : name}
                </button>
              ))}
            </nav>
          )}

          {run && tab === 'review' && (
            <>
              {proposals.length === 0 && (
                <div className="empty">
                  <p>Nothing to review.</p>
                  <p className="hint">
                    A gate over an empty list is not a gate — it teaches a reviewer
                    to click through. The run said: {run.note ?? 'no change.'}
                  </p>
                </div>
              )}

              {proposals.map((proposal) => (
                <Proposal
                  key={proposal.id}
                  proposal={proposal}
                  verdict={verdicts[proposal.id] ?? null}
                  // Functional update, not `{...verdicts, [id]: value}`. Two
                  // verdicts set before React re-renders both read the same
                  // stale `verdicts` and the second overwrites the first, so a
                  // reviewer clicking quickly down a list of thirteen would
                  // submit one decision and believe they had submitted
                  // thirteen. Found by driving the page rather than by reading
                  // it.
                  onVerdict={(value) =>
                    setVerdicts((prev) => ({ ...prev, [proposal.id]: value }))}
                />
              ))}

              {proposals.length > 0 && (
                <div className="review-bar">
                  <div className="counts">
                    {chosen} of {pending.length} decided
                    {undecided > 0 && <span className="warn"> · {undecided} still open</span>}
                  </div>
                  <input
                    className="who" placeholder="who is deciding"
                    value={decidedBy} onChange={(e) => setDecidedBy(e.target.value)}
                  />
                  <button className="primary" onClick={submitReview} disabled={!canDecide || !!busy}>
                    Record review
                  </button>
                  <button
                    className="commit" onClick={commit} disabled={!canCommit || !!busy}
                    title={canCommit ? 'write the approved items to the register'
                      : `${pending.length} item(s) still pending; a run cannot commit while any item is undecided`}
                  >
                    Commit
                  </button>
                </div>
              )}
              {!decidedBy.trim() && chosen > 0 && (
                <p className="hint right">
                  A decision has to have a decider — the audit trail records who,
                  and through which surface.
                </p>
              )}
            </>
          )}

          {tab === 'findings' && (findings
            ? <Findings findings={findings} />
            : <div className="empty"><p>No findings yet for this pile.</p></div>)}

          {tab === 'register' && (register
            ? <Register register={register} />
            : <div className="empty">
                <p>No committed register.</p>
                <p className="hint">
                  The register exists only after a person approves something. That
                  is the point.
                </p>
              </div>)}

          {tab === 'audit' && (audit?.audit?.length
            ? <Audit audit={audit} />
            : <div className="empty"><p>Nothing committed yet, so nothing to trace.</p></div>)}

          {tab === 'cost' && (report
            ? <Stages report={report} />
            : <div className="empty"><p>No stage events for this run.</p></div>)}
        </main>
      </div>
    </div>
  )
}
