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

// Backend runs extraction/evolution-precompute as a background task (see
// backend/main.py's _run_upload_pipeline) so /api/upload itself returns
// almost immediately with just an acknowledgment — this polls the single
// status slot until that background work reports done/error.
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
    seed_method_used: uploadResult?.benchmark?.seed_method_used ?? null,
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

export async function uploadDocument(file, seedMethod = 'llm', maxNodes = null, onStatus = null) {
  const formData = new FormData()
  formData.append('file', file)

  // seed_method and max_nodes are query params on the backend, not form fields
  const params = new URLSearchParams({ seed_method: seedMethod })
  if (maxNodes) params.set('max_nodes', maxNodes)
  const url = `${API_BASE_URL}/api/upload?${params.toString()}`
  await safeFetchJson(url, { method: 'POST', body: formData })

  // The request above only acknowledges receipt — extraction and evolution
  // precompute happen afterward in a background task, so wait for that to
  // actually finish before treating the upload as done.
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
  // Closest real equivalent to "generate graph" is /api/fuse, which fuses
  // everything uploaded so far. Backend has no per-document scoping or
  // per-call model override, so those args are accepted but unused.
  //
  // Entity merging + conflict resolution run synchronously inside /api/fuse
  // (no LLM calls, so no reason to background them); cross-week comparison
  // generation continues afterward in a background task on the server —
  // poll getPipelineStatus() if the caller needs to know when that's done.
  // The fuse response itself doesn't carry the full node/edge payload, so
  // /api/graph is read afterward for what actually gets rendered.
  await safeFetchJson(`${API_BASE_URL}/api/fuse`, { method: 'POST' })
  return safeFetchJson(`${API_BASE_URL}/api/graph`, { method: 'GET' })
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
      const { id, source, target, relation, label, ...rest } = rel
      return {
        id: id || `${source}_${target}`,
        source,
        target,
        type: relation || label || 'RELATED_TO',
        properties: rest,
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

export async function fetchGraphData(week = null) {
  const url = new URL(`${API_BASE_URL}/api/evolution/graph`)
  if (week) {
    url.searchParams.append('week', week)
  }
  return safeFetchJson(url.toString())
}

export async function fetchConcept(conceptName) {
  return safeFetchJson(`${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}`)
}

export async function fetchConceptSubgraph(conceptName) {
  return safeFetchJson(`${API_BASE_URL}/api/evolution/subgraph/${encodeURIComponent(conceptName)}`)
}