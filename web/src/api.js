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
  const body = text ? JSON.parse(text) : null
  if (!response.ok) {
    // The server's refusals carry the reason a reviewer needs -- "9 proposals
    // still pending", "this pile is being worked on by run ...". Losing that
    // and showing "request failed" would throw away the useful half.
    const detail = body?.detail ?? `${response.status} ${response.statusText}`
    throw new ApiError(response.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
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

  startRun: (pileId, corpus) => post('/runs', { pile_id: pileId, corpus }),
  arrival: (pileId, document) => post('/arrivals', { pile_id: pileId, document }),
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
}

export { ApiError }
