const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

async function handleResponse(response) {
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) {
    const err = new Error(data?.detail || data?.error || response?.statusText || 'API request failed')
    err.response = { data, status: response.status }
    throw err
  }
  return data
}

async function safeFetchJson(url, options = {}) {
  const response = await fetch(url, options)
  return handleResponse(response)
}

// Backend runs extraction as a background task (see backend/main.py's
// _run_upload_pipeline) so /api/upload itself returns almost immediately
// with just an acknowledgment — this polls the single status slot until
// that background work reports done/error. Evolution summaries are not
// part of this background work — they're generated on demand per concept
// (see fetchConceptDescription/fetchConcept below).
export async function getPipelineStatus() {
  return safeFetchJson(`${API_BASE_URL}/api/upload/status`)
}

async function pollUntilDone(pollFn, { intervalMs = 1500, timeoutMs = 10 * 60 * 1000 } = {}) {
  const start = Date.now()
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const status = await pollFn()
    if (status.stage === 'done' || status.stage === 'error') return status
    if (Date.now() - start > timeoutMs) {
      throw new Error('Timed out waiting for the backend to finish processing.')
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
}

// Backend has no document store — just one global engine that accumulates
// everything uploaded. We track upload metadata client-side so the docs
// table has real rows, built only from actual /api/upload responses.
let _documents = []
let _nextDocId = 1

function _docFromUploadResponse(file, uploadResult) {
  return {
    id: _nextDocId++,
    name: file.name,
    status: 'Completed',
    upload_status: 'Uploaded',
    size_kb: Math.round(file.size / 1024),
    source: 'upload',
    type: file.type || 'application/pdf',
    model: 'llama-3.3-70b-versatile',
    nodes: uploadResult?.total_concepts ?? 0,
    relationships: uploadResult?.total_edges ?? 0,
    raw: uploadResult,
  }
}

// Dummy — backend has no /api/auth/* routes. Exists so useAuth() doesn't crash.
export async function login(email, _password) {
  const user = { id: 'local-user', name: email.split('@')[0] || 'User', email }
  return { user, token: 'dummy-local-token', dummy: true }
}

export async function register(email, _password, name) {
  const user = { id: 'local-user', name: name || email.split('@')[0] || 'User', email }
  return { user, token: 'dummy-local-token', dummy: true }
}

// Backend hardcodes the model server-side and has no /api/models route.
export async function listModels() {
  return {
    providers: [
      {
        id: 'groq',
        name: 'Groq',
        available: true,
        env_var: 'GROQ_API_KEY',
        models: [{ id: 'llama-3.3-70b-versatile', label: 'Llama 3.3 70B (server-fixed)' }],
      },
    ],
    default: { provider: 'groq', model: 'llama-3.3-70b-versatile' },
    note: 'Backend ignores the selected model — picker is currently cosmetic.',
  }
}

export async function listDocuments() {
  return { documents: _documents }
}

export async function uploadDocument(file, seedMethod = 'llm', maxNodes = null, onStatus = null, minMentions = null) {
  const formData = new FormData()
  formData.append('file', file)

  // seed_method is a query param on the backend, not a form field
  const url = `${API_BASE_URL}/api/upload`
  // seed_method, max_nodes, and min_mentions are query params on the backend, not form fields
  const params = new URLSearchParams()
  if (maxNodes) params.set('max_nodes', maxNodes)
  if (minMentions) params.set('min_mentions', minMentions)
  const uploadResult = await safeFetchJson(url, { method: 'POST', body: formData })

  // The request above only acknowledges receipt — extraction happens
  // afterward in a background task, so wait for that to actually finish
  // before treating the upload as done.
  const finalStatus = await pollUntilDone(async () => {
    const status = await getPipelineStatus()
    if (onStatus) onStatus(status)
    return status
  })

  if (finalStatus.stage === 'error') {
    throw new Error(finalStatus.error || 'Upload processing failed')
  }

  const doc = _docFromUploadResponse(file, finalStatus)
  _documents.push(doc)
  return { document: doc, raw: finalStatus }
}

export async function deleteDocument(id) {
  // Dummy — backend has no per-document delete, this only removes the row locally
  const before = _documents.length
  _documents = _documents.filter((d) => d.id !== id)
  return { deleted: before !== _documents.length, dummy: true }
}

export async function generateGraph(_docIds, _provider, _model) {

  await safeFetchJson(`${API_BASE_URL}/api/fuse`, { method: 'POST' })
  return fetchGraph()
}

// Raw GET /api/graph, with no /api/fuse call — used to restore whatever
// graph the backend already has (e.g. on mount) without re-triggering
// fusion. Same response shape generateGraph() resolves to.
export async function fetchGraph() {
  return safeFetchJson(`${API_BASE_URL}/api/graph`, { method: 'GET' })
}

export function getDocumentFileUrl(filename) {
  return `${API_BASE_URL}/api/documents/file/${encodeURIComponent(filename)}`
}

// Clears cached summaries/comparisons for a concept so the next description/
// evolution fetch regenerates them from Groq — the way to recover a concept
// whose cached summary is the raw-excerpt fallback (see is_fallback on
// fetchConceptDescription/fetchConcept) or is just wrong for any reason.
export async function regenerateConcept(conceptName) {
  return safeFetchJson(`${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}/regenerate`, {
    method: 'POST',
  })
}

function buildGraphPayload(raw) {
  if (!raw) return { nodes: [], relationships: [], stats: {} }

  const nodes = Array.isArray(raw.nodes)
    ? raw.nodes.map((node) => {
      const { id, name, description, source, is_canonical, color, appears_in_weeks, ...rest } = node
      return {
        id,
        label: name || 'Concept',
        // Use the color from the backend, with a fallback for older data structures.
        color: color || (is_canonical ? '#a3e635' : '#8b5cf6'),
        properties: {
          ...rest,
          ...(name ? { name } : {}),
          ...(description ? { description } : {}),
          ...(source ? { source } : {}),
          ...(appears_in_weeks !== undefined ? { appears_in_weeks } : {}),
        },
      }
    })
    : []

  const relationshipsSource = raw.edges || raw.relationships || []
  const relationships = Array.isArray(relationshipsSource)
    ? relationshipsSource.map((rel) => {
      const { id, source, target, relation, label, summary, context, ...rest } = rel
      return {
        id: id || `${source}_${target}`,
        source,
        target,
        type: relation || label || 'RELATED_TO',
        properties: {
          ...rest,
          ...(summary ? { summary } : {}),
          ...(context ? { context } : {}),
        },
      }
    })
    : []

  const stats = raw.stats || {
    total_nodes: nodes.length,
    total_relationships: relationships.length,
  }

  return { nodes, relationships, stats, is_fused: !!raw.is_fused }
}

export async function getGraph() {
  const rawGraph = await safeFetchJson(`${API_BASE_URL}/api/graph`)
  return buildGraphPayload(rawGraph)
}

export async function askQuestion(question) {
  return safeFetchJson(`${API_BASE_URL}/api/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: question }),
  })
}

// Cheap tier: one grounded summary from the concept's earliest-appearance
// week only. Use this for a plain node click — it's what backs the
// "DESCRIPTION" text in both the general graph and the Evolution graph.
export async function fetchConceptDescription(conceptName) {
  return safeFetchJson(`${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}/description`)
}

// Full tier: every week's summary plus every week-to-week comparison. Costs
// one LLM call per week/transition, so only call this from an explicit
// "show concept evolution" action, not on a plain node click.
export async function fetchConcept(conceptName) {
  return safeFetchJson(`${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}`)
}

export async function fetchConceptSubgraph(conceptName) {
  return safeFetchJson(`${API_BASE_URL}/api/evolution/subgraph/${encodeURIComponent(conceptName)}`)
}

// Relationship quiz, generated fresh from the current graph on every call
// (question selection is randomized server-side, so this isn't cached like
// fetchConceptDescription/fetchConcept are).
export async function generateQuiz(count = 5) {
  return safeFetchJson(`${API_BASE_URL}/api/quiz?count=${count}`)
}

export async function submitQuizAnswers(quizId, answers) {
  return safeFetchJson(`${API_BASE_URL}/api/quiz/${encodeURIComponent(quizId)}/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ answers }),
  })
}