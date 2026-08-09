/**
 * The review desk.
 *
 * This screen exists for one job: a person looks at what the system understood,
 * decides item by item, and only then does anything get written. Everything
 * else on it (the register, the findings, the audit trail, the cost table, the
 * source search) is there so the reviewer can *check* the answer rather than
 * take it.
 *
 * ## The rules that shaped it
 *
 * **1. The gate is a gate.** Commit stays disabled while anything is pending,
 * and the reason sits beside the button rather than inside a tooltip. There is
 * no "approve all" and there will not be one: a control whose whole purpose is
 * to let someone clear thirteen items without reading them would quietly undo
 * the only behaviour this product has.
 *
 * **2. A review is one review.** Verdicts are collected locally and sent as a
 * single POST with mixed approvals and rejections, which is how the operation is
 * shaped everywhere else. Deciding one item per request would leave a
 * half-reviewed run behind on any failure.
 *
 * **3. The screen never claims more than the server said.** Every panel is
 * filled from a response; nothing is predicted locally, and there are no
 * optimistic updates. When the server refuses ("9 proposal(s) still pending",
 * "pile is held by run ...") its sentence is what appears, because the server's
 * refusals are the useful ones.
 *
 * ## Why the layout is a queue beside a detail pane
 *
 * The previous version stacked every proposal as a full card in one column, so
 * reading the eighth item's evidence meant scrolling past the seventh item's
 * decision. That makes the cheapest way through the list "stop reading", which
 * is the same failure as an approve-all button arrived at by accident. A queue
 * on the left and one item on the right means the evidence for the item being
 * decided is always the thing on screen, and the queue doubles as the record of
 * what has been decided so far.
 *
 * The tabs are grouped into **Decide** and **Evidence** for the same reason.
 * One of those changes the deliverable and the rest do not, and a flat row of
 * six equal tabs said they were all the same kind of thing.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError } from './api'
import {
  Audit, Detail, Empty, EndRun, Findings, NearMatch, Picker, Register, Sources,
  Stages, Watch,
} from './components'

const DECIDE_TABS = [{ id: 'review', label: 'Review' }]
const EVIDENCE_TABS = [
  { id: 'sources', label: 'Sources' },
  { id: 'findings', label: 'Rule checks' },
  { id: 'register', label: 'Register' },
  { id: 'audit', label: 'History' },
  { id: 'cost', label: 'Cost' },
  { id: 'watch', label: 'Folder' },
]

/** Dark, light, or whatever the operating system says.
 *
 * Three states rather than two, and dark first rather than `system` first. This
 * screen is read for long stretches beside a terminal and mostly shows quoted
 * document text, which is more comfortable on a dark field, but a reviewer
 * whose machine switches at dusk should still be able to hand the decision back,
 * so `system` stays in the cycle rather than being replaced by this file's
 * opinion.
 */
const THEMES = ['dark', 'light', 'system']
const THEME_ICON = { system: '◐', light: '☀', dark: '☾' }

function applyTheme(theme) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  try { localStorage.setItem('doctask-theme', theme) } catch { /* private mode */ }
}

function storedTheme() {
  try {
    const saved = localStorage.getItem('doctask-theme')
    return THEMES.includes(saved) ? saved : 'dark'
  } catch { return 'dark' }
}

/** Most serious first.
 *
 * Not the order the graph raised them in, which is an implementation detail and
 * puts six register sections in front of a broken rule. A reviewer working top
 * to bottom should meet the things that can be wrong before the things that are
 * merely long.
 */
const KIND_RANK = { escalation: 0, finding: 1, conflict: 2, section_patch: 3 }
const SEVERITY_RANK = { high: 0, medium: 1, low: 2, info: 3 }

function rank(proposal) {
  const payload = proposal.payload ?? {}
  return [
    KIND_RANK[proposal.kind] ?? 9,
    SEVERITY_RANK[payload.severity] ?? 4,
    payload.field ?? payload.rule_key ?? payload.section_key ?? '',
  ]
}

function bySeriousness(a, b) {
  const [ka, sa, na] = rank(a)
  const [kb, sb, nb] = rank(b)
  return ka - kb || sa - sb || String(na).localeCompare(String(nb))
}

function queueLabel(proposal) {
  const p = proposal.payload ?? {}
  if (proposal.kind === 'conflict') {
    return {
      title: p.field,
      meta: [`${p.values?.length ?? 0} values`, `${p.members?.length ?? 0} documents`],
    }
  }
  if (proposal.kind === 'finding') {
    return {
      title: p.rule_key,
      meta: [p.severity, `${p.citations?.length ?? 0} citations`],
      severity: p.severity,
    }
  }
  if (proposal.kind === 'section_patch') {
    const cited = p.citation_count ?? p.citations ?? null
    return { title: p.section_key, meta: cited != null ? [`${cited} cited`] : [] }
  }
  return { title: proposal.summary?.slice(0, 60) ?? proposal.kind, meta: [] }
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [piles, setPiles] = useState([])
  const [pileId, setPileId] = useState('')
  const [documents, setDocuments] = useState(null)

  // Nothing is preselected. These start empty and are filled by picking from
  // what `GET /corpora` says is actually on disk. A default folder name here
  // was a guess that happened to be right for the demo corpus and wrong for
  // anyone else's.
  const [corpus, setCorpus] = useState('')
  const [arrivalPath, setArrivalPath] = useState('')
  const [corpora, setCorpora] = useState(null)
  // A file from the reviewer's own machine, held until they confirm. Kept apart
  // from `arrivalPath` so the two ways of choosing cannot both be armed at once.
  const [localFile, setLocalFile] = useState(null)
  const [newPile, setNewPile] = useState('')
  const [dialog, setDialog] = useState(null)   // 'read' | 'arrive' | null
  // Ending a run is deliberately two steps. The panel is where the promise that
  // nothing is deleted gets made, and a one-click version would skip it.
  const [ending, setEnding] = useState(false)
  const [endReason, setEndReason] = useState('')

  const [run, setRun] = useState(null)
  const [runs, setRuns] = useState([])
  const [proposals, setProposals] = useState([])
  const [verdicts, setVerdicts] = useState({})
  const [decidedBy, setDecidedBy] = useState('')
  const [selected, setSelected] = useState(null)

  const [register, setRegister] = useState(null)
  const [findings, setFindings] = useState(null)
  const [audit, setAudit] = useState(null)
  const [report, setReport] = useState(null)
  const [search, setSearch] = useState(null)
  const [query, setQuery] = useState('')
  const [watch, setWatch] = useState(null)

  const [tab, setTab] = useState('review')
  const [theme, setTheme] = useState(storedTheme)
  // The pane and the queue are each one DOM node reused for every item, so
  // their scroll position outlives the thing that was in them. See the effects
  // below `applyTheme`.
  const paneRef = useRef(null)
  const queueRef = useRef(null)
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
      setError(exc instanceof ApiError ? exc.detail : String(exc))
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
    const ordered = [...list.proposals].sort(bySeriousness)
    setProposals(ordered)
    setReport(rep)
    setVerdicts({})
    setSelected(ordered.find((p) => p.status === 'pending')?.id ?? ordered[0]?.id ?? null)
  }, [])

  /** Open a run that already exists, from the server's row rather than from a
   *  start response. The counts a start reports (documents read, facts found,
   *  model calls made) are facts about that invocation, and this screen does
   *  not have them, so it shows them as unknown instead of as zero. */
  const open = useCallback(async (row) => {
    setRun({ run_id: row.id, status: row.status, kind: row.kind,
             pending_proposals: Number(row.pending_proposals),
             // Carried so that a reading someone ended says who ended it and
             // why, rather than only that it stopped. "Abandoned" with no
             // account of it is the kind of dead end this was built to remove.
             abandoned_by: row.abandoned_by, abandon_reason: row.abandon_reason })
    setVerdicts({})
    setNotice(null)
    setTab('review')
    await loadRun(row.id)
  }, [loadRun])

  useEffect(() => { applyTheme(theme) }, [theme])

  /* Start each item at its beginning.
   *
   * React keeps the same scrolling element across a selection change, so its
   * scrollTop survives into the next item. Picking a long conflict and then a
   * short one used to open the short one part way down, or past its end
   * entirely -- and the thing scrolled out of view is the claim being decided,
   * which is the one part a reviewer must not skip.
   *
   * Instantly, not smoothly: this is a different document, not a move within
   * one, and animating the arrival would run the new item's evidence past
   * somebody who has not read the top of it yet. */
  useEffect(() => {
    paneRef.current?.querySelector('.detail-scroll, .sheet')?.scrollTo({ top: 0 })
  }, [selected, tab])

  /* Keep the item being decided visible in the queue.
   *
   * `block: 'nearest'` means this does nothing at all when the item is already
   * on screen, which is the common case; it only acts when a decision or a
   * refresh has moved the selection somewhere out of sight. Anything stronger
   * would re-centre the list under a reviewer who was reading it. */
  useEffect(() => {
    queueRef.current?.querySelector('.item.on')?.scrollIntoView({ block: 'nearest' })
  }, [selected])

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth({ status: 'unreachable' }))
    api.listPiles().then((body) => setPiles(body.piles)).catch(() => {})
    api.watch().then(setWatch).catch(() => setWatch({ enabled: false }))
  }, [])

  // Re-read on every open rather than once at mount. The watched folder drops
  // files into corpora/ while this tab is sitting there, so a listing cached at
  // startup would offer a stale picture of the disk.
  useEffect(() => {
    if (!dialog) return
    api.corpora().then(setCorpora).catch(() => setCorpora({ folders: [], files: [] }))
  }, [dialog])

  // The watched folder is the one panel that changes without anybody clicking,
  // so it is the one panel that polls, and only while it is being looked at.
  useEffect(() => {
    if (tab !== 'watch') return undefined
    const tick = () => api.watch().then(setWatch).catch(() => {})
    tick()
    const timer = setInterval(tick, 5000)
    return () => clearInterval(timer)
  }, [tab])

  // Selecting a pile picks up whatever is already open on it.
  //
  // Before this existed the only handle on a run stopped at the gate was a
  // variable in this tab, so a refresh stranded the review: thirteen items
  // waiting in the database with nothing able to name the run holding them.
  // Behaviour 2 says a stopped run can be picked back up; a UI that lost the
  // run id on reload was quietly not honouring that.
  useEffect(() => {
    let current = true
    if (!pileId) { setRuns([]); return undefined }
    loadPile(pileId)
    setSearch(null)
    api.listRuns(pileId).then((body) => {
      if (!current) return
      setRuns(body.runs)
      const waiting = body.runs.find((r) => r.status === 'awaiting_approval')
      if (waiting) open(waiting)
    }).catch(() => {})
    return () => { current = false }
  }, [pileId, loadPile, open])

  // -------------------------------------------------------------- actions --

  function reset() {
    setRun(null)
    setProposals([])
    setVerdicts({})
    setReport(null)
    setNotice(null)
    setSelected(null)
    setTab('review')
  }

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

  const refreshRuns = useCallback(async (id) => {
    const body = await api.listRuns(id).catch(() => null)
    if (body) setRuns(body.runs)
  }, [])

  async function begin(kind) {
    setDialog(null)
    reset()
    const sending = kind === 'arrival' && localFile
    const result = await guard(
      kind === 'run' ? 'reading the pile'
        : sending ? `sending and reading ${localFile.name}`
          : 'reading the new document',
      () => (kind === 'run'
        ? api.startRun(pileId, corpus)
        : sending
          ? api.upload(pileId, localFile)
          : api.arrival(pileId, arrivalPath)),
    )
    setLocalFile(null)
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
    const outcome = await guard('recording your decisions',
      () => api.decide(run.run_id, decisions, decidedBy.trim()))
    if (!outcome) return
    setNotice(`${outcome.approved} approved, ${outcome.rejected} rejected, `
      + `${outcome.pending} still to decide`)
    setRun((prev) => ({ ...prev, pending_proposals: outcome.pending }))
    await loadRun(run.run_id)
  }

  async function commit() {
    const done = await guard('saving to the register', () => api.commit(run.run_id))
    if (!done) return
    setNotice(done.sections_written !== undefined
      ? `Saved as version ${done.version}. ${done.sections_written} section(s) written, `
        + `${done.sections_carried} kept as they were, ${done.rejected} rejected and not written.`
      : `Nothing to save. ${done.status}.`)
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

  async function abandon() {
    const done = await guard('ending the run',
      () => api.abandon(run.run_id, decidedBy.trim(), endReason.trim()))
    if (!done) return
    setEnding(false)
    setEndReason('')
    setNotice(`Ended by ${done.abandoned_by}. `
      + (done.left_undecided
        ? `${done.left_undecided} item(s) left undecided. `
        : '')
      + 'Everything it read and everything it cost is still on the pile.')
    setRun((prev) => ({ ...prev, status: 'abandoned',
                        abandoned_by: done.abandoned_by,
                        abandon_reason: done.reason }))
    await Promise.all([loadRun(run.run_id), refreshRuns(pileId)])
  }

  async function runSearch() {
    if (!query.trim()) return
    const body = await guard('searching the documents',
      () => api.search(pileId, query.trim(), 8))
    if (body) setSearch(body)
  }

  // --------------------------------------------------------------- derived --

  const pending = useMemo(
    () => proposals.filter((p) => p.status === 'pending'), [proposals])
  const undecided = pending.filter(
    (p) => verdicts[p.id] === undefined || verdicts[p.id] === null).length
  const chosen = pending.length - undecided
  const canDecide = chosen > 0 && decidedBy.trim().length > 0
  const canCommit = run && pending.length === 0 && run.status !== 'committed'
      && proposals.length > 0

  // Documents held back because their counterparty may already be on file.
  // Not proposals (see `NearMatch`), so they are counted and shown separately
  // rather than folded into the queue's totals.
  const heldBack = run?.escalated ?? []

  const sections = proposals.filter((p) => p.kind === 'section_patch')
  const items = proposals.filter((p) => p.kind !== 'section_patch')
  const current = proposals.find((p) => p.id === selected)
  const currentIndex = proposals.findIndex((p) => p.id === selected)

  const pileName = piles.find((p) => p.id === pileId)?.name
  const openRuns = runs.filter((r) => r.status === 'awaiting_approval').length

  function selectItem(id) { setSelected(id); setTab('review') }

  function renderQueueItem(proposal, ordinal) {
    const { title, meta, severity } = queueLabel(proposal)
    const verdict = verdicts[proposal.id]
    const settled = proposal.status !== 'pending'
    const mark = verdict === true || proposal.status === 'approved' ? 'a'
      : verdict === false || proposal.status === 'rejected' ? 'r' : null
    return (
      <li
        key={proposal.id}
        className={`item${selected === proposal.id ? ' on' : ''}`}
        onClick={() => selectItem(proposal.id)}
      >
        {/* The ordinal becomes a verdict mark once a decision is chosen, so the
            queue is both the map and the progress record. */}
        {mark
          ? <span className={`verdict-mark ${mark}`}>{mark === 'a' ? 'A' : 'R'}</span>
          : <span className="ordinal">{ordinal}</span>}
        <span>
          <div className="title">{title}</div>
          <div className="meta">
            <span className={severity ? `sev-${severity}` : ''}>
              {proposal.kind === 'section_patch' ? 'section' : proposal.kind}
            </span>
            {meta.filter(Boolean).map((m) => <span key={m}>· {m}</span>)}
            {settled && <span>· {proposal.status}</span>}
          </div>
        </span>
      </li>
    )
  }

  return (
    <div className="app">
      <header className="top">
        <h1>doctask</h1>
        <span className="rule" />
        {pileName
          ? <>
              <span className="pile-name">{pileName}</span>
              <span className="sub">
                {documents?.documents?.length ?? 0} documents
                {openRuns > 0 && ` · ${openRuns} run${openRuns === 1 ? '' : 's'} open`}
              </span>
            </>
          : <span className="sub">vendor contracts, read and checked end to end</span>}
        <span className="spacer" />
        <span className={`health ${health?.status ?? ''}`}>
          {health ? health.status : '…'}
        </span>
        <button
          className="theme"
          title={`Theme: ${theme}. Click for dark, light, then match your system.`}
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
            <div className="cap-row"><span className="cap">Pile</span></div>
            <select value={pileId} onChange={(e) => { setPileId(e.target.value); reset() }}>
              <option value="">Choose a pile</option>
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
              {runs.length > 0 && (
                <section>
                  <div className="cap-row">
                    <span className="cap">Runs</span>
                    <span className="count">{runs.length}</span>
                  </div>
                  <ul className="runs">
                    {runs.map((r) => (
                      <li
                        key={r.id}
                        className={run?.run_id === r.id ? 'on' : ''}
                        onClick={() => open(r)}
                      >
                        <span className="rid">{r.id.slice(0, 8)}</span>
                        <span className={`state ${r.status}`}>
                          {r.status.replace(/_/g, ' ')}
                        </span>
                        <span className="when">
                          {new Date(r.started_at).toLocaleString([], {
                            day: 'numeric', month: 'short',
                            hour: '2-digit', minute: '2-digit',
                          })}
                        </span>
                        {/* An ended reading keeps its undecided items, so the
                            count is still true -- but "pending" would read as
                            work waiting for someone, and nobody is ever going
                            to decide these. */}
                        {Number(r.pending_proposals) > 0 && (
                          r.status === 'abandoned'
                            ? <span className="pending done">
                                {r.pending_proposals} left undecided
                              </span>
                            : <span className="pending">{r.pending_proposals} pending</span>
                        )}
                      </li>
                    ))}
                  </ul>
                  <p className="hint">
                    Work waiting for review is saved with the pile, not with
                    this browser tab. Closing the tab does not lose it.
                  </p>
                </section>
              )}

              {documents?.documents?.length > 0 && (
                <section className="grow">
                  <div className="cap-row">
                    <span className="cap">Documents</span>
                    <span className="count">{documents.documents.length}</span>
                  </div>
                  <ul className="docs">
                    {documents.documents.map((d) => (
                      <li key={d.id}>
                        <span className="type">{d.doc_type ?? d.format}</span>
                        <span className="name" title={d.filename}>{d.filename}</span>
                        {/* `is_gap` comes from the server, so HTTP, MCP and this
                            screen cannot disagree about whether a document was
                            read. This used to test `status !== 'ingested'`
                            here, which labelled a classified document a gap. */}
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

              <section className="side-actions">
                <button onClick={() => setDialog('read')} disabled={!!busy}>
                  Read the pile
                </button>
                <button onClick={() => setDialog('arrive')} disabled={!!busy}>
                  Add a document
                </button>
              </section>
            </>
          )}
        </aside>

        <main>
          {busy && <div className="banner busy">{busy}…</div>}
          {error && <div className="banner error">{error}</div>}
          {notice && <div className="banner notice">{notice}</div>}

          {!pileId ? (
            <Empty big="Choose a pile on the left, or create one.">
              Everything on this screen can also be done by a program, and this
              screen gets no shortcuts. It cannot approve or write anything a
              program could not, and it cannot skip the review.
            </Empty>
          ) : (
            <>
              {run && (
                <div className="runbar">
                  <span className={`status ${run.status}`}>
                    {run.status?.replace(/_/g, ' ')}
                  </span>
                  <span className="runid">{run.run_id?.slice(0, 8)}</span>
                  <span className="sep" />
                  {/* Only what this screen was actually told. A run opened from
                      the runs list has no document or fact count here, and
                      showing zero would be a made-up number in the one place a
                      reviewer looks to see how much the system read. */}
                  {run.documents !== undefined ? (
                    <>
                      <span className="stat"><b>{run.documents}</b> documents</span>
                      <span className="stat"><b>{run.facts}</b> facts</span>
                      <span className="stat"><b>{run.conflicts}</b> conflicts</span>
                      <span className="stat"><b>{run.gaps}</b> gaps</span>
                      <span className="stat">
                        <b>{run.model_calls}</b> AI calls
                        {run.replayed_calls ? ` (${run.replayed_calls} from saved answers)` : ''}
                      </span>
                    </>
                  ) : (
                    <span className="stat">
                      opened again later, so the totals from the first reading
                      are not shown
                    </span>
                  )}
                  {run.quarantined?.length > 0 && (
                    <span className="stat bad">{run.quarantined.length} set aside</span>
                  )}
                  {heldBack.length > 0 && (
                    <span className="stat bad">{heldBack.length} held back</span>
                  )}

                  {/* The third movement's actual claim. `unchanged` is the
                      interesting half: those sections were recomposed from
                      scratch and hashed to the same bytes, which is evidence. A
                      section skipped because we guessed it would not move would
                      only be an assumption. */}
                  {run.unchanged !== undefined && (
                    <>
                      <span className="sep" />
                      <span className="delta added"><b>{run.added.length}</b> added</span>
                      <span className="delta changed"><b>{run.changed.length}</b> changed</span>
                      <span className="delta unchanged">
                        <b>{run.unchanged.length}</b> unchanged
                        {run.unchanged.length > 0 && (
                          <span className="why">worked out again and identical, not skipped</span>
                        )}
                      </span>
                    </>
                  )}
                  {run.status === 'running' && (
                    <button onClick={resume} disabled={!!busy}>Resume</button>
                  )}
                  {/* Offered for any reading that has not finished, not only a
                      stuck one. A reviewer who has decided a reading was a
                      mistake should not have to wait for it to break first. */}
                  {(run.status === 'running' || run.status === 'awaiting_approval') && (
                    <button onClick={() => setEnding(true)} disabled={!!busy}>
                      End this reading
                    </button>
                  )}
                  {run.status === 'abandoned' && run.abandon_reason && (
                    <span className="stat">
                      ended by {run.abandoned_by}: {run.abandon_reason}
                    </span>
                  )}
                </div>
              )}

              <nav className="tabs">
                <div className="group">
                  <span className="cap">Decide</span>
                  {DECIDE_TABS.map((t) => (
                    <button
                      key={t.id}
                      className={tab === t.id ? 'on' : ''}
                      onClick={() => setTab(t.id)}
                    >
                      {t.label}
                      {pending.length > 0 && (
                        <span className="badge urgent">{pending.length}</span>
                      )}
                    </button>
                  ))}
                </div>
                <span className="sep" />
                <div className="group">
                  <span className="cap">Evidence</span>
                  {EVIDENCE_TABS.map((t) => (
                    <button
                      key={t.id}
                      className={tab === t.id ? 'on' : ''}
                      onClick={() => setTab(t.id)}
                    >
                      {t.label}
                      {t.id === 'findings' && findings?.counts?.violated > 0 && (
                        <span className="badge urgent">{findings.counts.violated}</span>
                      )}
                      {t.id === 'watch' && watch?.events?.length > 0 && (
                        <span className="badge">{watch.events.length}</span>
                      )}
                      {t.id === 'register' && !register && (
                        <span className="badge">none yet</span>
                      )}
                    </button>
                  ))}
                </div>
              </nav>

              <div className="pane" ref={paneRef}>
                {tab === 'review' && (
                  !run ? (
                    <Empty big="Nothing is open on this pile.">
                      Use <b>Read the pile</b> to go through it from the start,
                      or <b>Add a document</b> to bring one in. Neither one
                      writes anything: both stop here and wait for you.
                    </Empty>
                  ) : proposals.length === 0 && heldBack.length === 0 ? (
                    <Empty big="Nothing to review." good>
                      An empty list with an approve button on it teaches people
                      to click without reading, so there is not one. What
                      happened: {run.note ?? 'nothing changed.'}
                    </Empty>
                  ) : (
                    <>
                      <div className="queue" ref={queueRef}>
                        <div className="queue-head">
                          <span className="cap">To decide, most serious first</span>
                          <span className="count">{chosen} / {pending.length}</span>
                        </div>
                        <ul>
                          {heldBack.length > 0 && (
                            <li
                              className={`item${selected === '__held' ? ' on' : ''}`}
                              onClick={() => selectItem('__held')}
                            >
                              <span className="verdict-mark r">!</span>
                              <span>
                                <div className="title">
                                  {heldBack.length === 1
                                    ? heldBack[0].document
                                    : `${heldBack.length} documents held back`}
                                </div>
                                <div className="meta">
                                  <span className="sev-high">same company?</span>
                                  <span>· cannot be settled here</span>
                                </div>
                              </span>
                            </li>
                          )}
                          {items.map((p, i) => renderQueueItem(p, i + 1))}
                          {sections.length > 0 && (
                            <li className="divider">
                              Register sections · {sections.length}
                            </li>
                          )}
                          {sections.map((p, i) => renderQueueItem(p, items.length + i + 1))}
                        </ul>
                      </div>

                      <div className="detail">
                        {selected === '__held' ? (
                          <div className="detail-scroll">
                            <div className="detail-inner">
                              <NearMatch escalations={heldBack} />
                            </div>
                          </div>
                        ) : current ? (
                          <Detail
                            proposal={current}
                            index={currentIndex}
                            total={proposals.length}
                            verdict={verdicts[current.id] ?? null}
                            // Functional update, not `{...verdicts, [id]: v}`.
                            // Two verdicts set before React re-renders both read
                            // the same stale `verdicts` and the second
                            // overwrites the first, so a reviewer clicking
                            // quickly down a list of thirteen would submit one
                            // decision and believe they had submitted thirteen.
                            // Found by driving the page rather than reading it.
                            onVerdict={(value) => setVerdicts((prev) => ({
                              ...prev, [current.id]: value,
                            }))}
                          />
                        ) : (
                          <Empty big="Choose an item on the left." />
                        )}

                        <div className="review-bar">
                          <div className="pips">
                            {pending.map((p) => {
                              const v = verdicts[p.id]
                              return (
                                <i
                                  key={p.id}
                                  className={v === true ? 'a' : v === false ? 'r' : ''}
                                />
                              )
                            })}
                          </div>
                          <span className="counts">
                            <b>{chosen}</b> of {pending.length} decided
                            {undecided > 0 && (
                              <span className="warn"> · {undecided} still open</span>
                            )}
                          </span>
                          <span className="spacer" />
                          <input
                            className="who" placeholder="who is deciding"
                            value={decidedBy}
                            onChange={(e) => setDecidedBy(e.target.value)}
                            title="Your name is recorded against every decision you make."
                          />
                          <button
                            className="primary"
                            onClick={submitReview}
                            disabled={!canDecide || !!busy}
                            title={decidedBy.trim() ? '' :
                              'Put your name in the box first. Every decision is recorded against someone.'}
                          >
                            {chosen > 0
                              ? `Record ${chosen} decision${chosen === 1 ? '' : 's'}`
                              : 'Record review'}
                          </button>
                          <span className="commit-wrap">
                            <button
                              className="commit" onClick={commit}
                              disabled={!canCommit || !!busy}
                            >
                              Save to register
                            </button>
                            {/* The reason the gate is shut, beside the shut
                                button rather than inside a tooltip nobody
                                hovers. */}
                            {!canCommit && (
                              <span className="why">
                                {run.status === 'committed'
                                  ? 'already saved'
                                  : pending.length > 0
                                    ? `${pending.length} item${pending.length === 1 ? '' : 's'} still undecided`
                                    : 'nothing to save'}
                              </span>
                            )}
                          </span>
                        </div>
                      </div>
                    </>
                  )
                )}

                {tab === 'sources' && (
                  <Sources
                    result={search} query={query} onQuery={setQuery}
                    onSearch={runSearch} busy={busy}
                  />
                )}

                {tab === 'findings' && (findings?.findings?.length
                  ? <Findings findings={findings} />
                  : <Empty big="Nothing checked yet.">
                      Rule checks appear once the pile has been read and your
                      rules have been put to it.
                    </Empty>)}

                {tab === 'register' && (register
                  ? <Register register={register} />
                  : <Empty big="No register yet.">
                      The register only exists after a person approves something.
                      That is the point.
                    </Empty>)}

                {tab === 'audit' && (audit?.audit?.length
                  ? <Audit audit={audit} />
                  : <Empty big="Nothing saved yet, so there is no history." />)}

                {tab === 'cost' && (report?.stages?.length
                  ? <Stages report={report} />
                  : <Empty big="Nothing has been measured yet." />)}

                {tab === 'watch' && <Watch watch={watch} />}
              </div>
            </>
          )}
        </main>
      </div>

      {ending && run && (
        <EndRun
          run={run}
          name={decidedBy}
          onName={setDecidedBy}
          reason={endReason}
          onReason={setEndReason}
          onConfirm={abandon}
          onCancel={() => setEnding(false)}
          busy={busy}
        />
      )}

      {dialog && (
        <Picker
          mode={dialog === 'read' ? 'folder' : 'file'}
          corpora={corpora}
          value={dialog === 'read' ? corpus : arrivalPath}
          onValue={dialog === 'read' ? setCorpus : setArrivalPath}
          localFile={localFile}
          onLocalFile={setLocalFile}
          limitMb={25}
          onConfirm={() => begin(dialog === 'read' ? 'run' : 'arrival')}
          onCancel={() => { setDialog(null); setLocalFile(null) }}
          busy={busy}
        />
      )}
    </div>
  )
}
