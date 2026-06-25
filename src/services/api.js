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

export async function uploadDocument(file, seedMethod = 'llm') {
  const formData = new FormData()
  formData.append('file', file)

  // seed_method is a query param on the backend, not a form field
  const url = `${API_BASE_URL}/api/upload?seed_method=${encodeURIComponent(seedMethod)}`
  const uploadResult = await safeFetchJson(url, { method: 'POST', body: formData })

  const doc = _docFromUploadResponse(file, uploadResult)
  _documents.push(doc)
  return { document: doc, raw: uploadResult }
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
  return safeFetchJson(`${API_BASE_URL}/api/fuse`, { method: 'POST' })
}

function buildGraphPayload(raw) {
  if (!raw) return { nodes: [], relationships: [], stats: {} }

  const nodes = Array.isArray(raw.nodes)
    ? raw.nodes.map((node) => {
      const { id, name, description, source, is_canonical, ...rest } = node
      return {
        id,
        label: name || 'Concept',
        color: is_canonical ? '#a3e635' : '#8b5cf6',
        properties: {
          ...rest,
          ...(name ? { name } : {}),
          ...(description ? { description } : {}),
          ...(source ? { source } : {}),
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