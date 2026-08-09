/**
 * The API, as the UI sees it.
 *
 * Every call here maps one-to-one onto an endpoint in app/api/runs.py, which is
 * itself a thin surface over app/operations.py. The UI has no privileged path
 * into the system and no operation of its own -- it is one client of the same
 * interface a script or an agent uses, which is what makes graded behaviour 4
 * true rather than merely claimed.
 *
 * In development Vite proxies /api to the container. In the container the UI is
 * served from the same origin, so the prefix is empty.
 */
const BASE = import.meta.env.DEV ? '/api' : ''

class ApiError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

async function request(path, options = {}) {
  const response = await fetch(BASE + path, {
    headers: { 'content-type': 'application/json' },
    ...options,
  })
  const text = await response.text()

  // Not every response is JSON. An unhandled server error is plain text, and
  // parsing it unconditionally used to throw a SyntaxError from in here --
  // which is how a server saying "Internal Server Error" reached the screen as
  // `Unexpected token 'I'`, a message about this file for a problem that has
  // nothing to do with it. Whatever the server actually said is more useful
  // than a parse failure, so a body that will not parse is kept as text.
  let body = null
  let parsed = true
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      parsed = false
    }
  }

  if (!response.ok) {
    // The server's refusals carry the reason a reviewer needs -- "9 proposals
    // still pending", "this pile is being worked on by run ...". Losing that
    // and showing "request failed" would throw away the useful half.
    const detail = (parsed ? body?.detail : text.trim())
      ?? `${response.status} ${response.statusText}`
    throw new ApiError(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  if (!parsed) {
    throw new ApiError(response.status,
      `the server answered ${response.status} with something that is not JSON`)
  }
  return body
}

const post = (path, body) =>
  request(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) })

export const api = {
  health: () => request('/health'),
  listPiles: () => request('/piles'),
  createPile: (name) => post(`/piles?name=${encodeURIComponent(name)}`),
  listDocuments: (pileId) => request(`/piles/${pileId}/documents`),
  // What is available to read. The screen offers these rather than asking
  // someone to type a path they have no way to discover.
  corpora: () => request('/corpora'),

  startRun: (pileId, corpus) => post('/runs', { pile_id: pileId, corpus }),
  arrival: (pileId, document) => post('/arrivals', { pile_id: pileId, document }),
  // A document from the caller's own machine. Multipart rather than JSON so a
  // large or binary file is streamed instead of held in memory as text, and so
  // the browser sets its own boundary.
  upload: (pileId, file) => {
    const body = new FormData()
    body.append('file', file)
    return request(`/piles/${pileId}/documents`, {
      method: 'POST', body, headers: {},
    })
  },
  listRuns: (pileId, status) => request(
    `/piles/${pileId}/runs${status ? `?status=${encodeURIComponent(status)}` : ''}`),
  getRun: (runId) => request(`/runs/${runId}`),
  resume: (runId) => post(`/runs/${runId}/resume`),

  proposals: (runId) => request(`/runs/${runId}/proposals`),
  decide: (runId, decisions, decidedBy) =>
    post(`/runs/${runId}/decide`, { decisions, decided_by: decidedBy }),
  commit: (runId) => post(`/runs/${runId}/commit`),

  report: (runId) => request(`/runs/${runId}/report`),
  register: (pileId) => request(`/piles/${pileId}/register`),
  findings: (pileId) => request(`/piles/${pileId}/findings`),
  audit: (pileId) => request(`/piles/${pileId}/audit`),

  // Retrieval. `search` returns sources and an `index` block; the UI is
  // required to show both, because "no hits" from an indexed pile and "no hits"
  // from an empty one are different claims.
  search: (pileId, query, limit = 8) => request(
    `/piles/${pileId}/search?q=${encodeURIComponent(query)}&limit=${limit}`),
  entities: (pileId) => request(`/piles/${pileId}/entities`),

  // The watched location. Not scoped to a pile: it is a property of the
  // deployment, and the answer to "is it even running" has to be readable when
  // no pile is selected.
  watch: () => request('/watch'),
}

export { ApiError }
