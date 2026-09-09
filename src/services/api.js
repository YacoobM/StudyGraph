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
        color: color || (is_canonical ? '#4D7C0F' : '#6D28D9'),
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

// ---------------------------------------------------------------------------
// token plumbing
// ---------------------------------------------------------------------------

// Same key auth.jsx already uses, so AuthProvider and these calls stay in step.
function authToken() {
  try {
    return localStorage.getItem('sg_token')
  } catch {
    return null   // storage throws in some sandboxed/private contexts
  }
}

function authHeaders(extra = {}) {
  const token = authToken()
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra
}

/** Turns a transport failure into a message that names the real cause. */
async function apiFetch(url, options = {}) {
  let response
  try {
    response = await fetch(url, options)
  } catch {
    const err = new Error(
      `Cannot reach the backend at ${API_BASE_URL}. Start it with: python dev_server.py`
    )
    err.offline = true
    throw err
  }
  return handleResponse(response)
}

// ---------------------------------------------------------------------------
// auth
// ---------------------------------------------------------------------------

export async function login(email, password) {
  return apiFetch(`${API_BASE_URL}/api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
}

export async function register(email, password, name) {
  return apiFetch(`${API_BASE_URL}/api/auth/signup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, name: name || '' }),
  })
}

export async function logout() {
  try {
    return await apiFetch(`${API_BASE_URL}/api/auth/logout`, {
      method: 'POST',
      headers: authHeaders(),
    })
  } catch {
    return { ok: true }   // the local token is cleared regardless
  }
}

/** Confirms a restored token is still valid. Throws 401 if it isn't. */
export async function me() {
  return apiFetch(`${API_BASE_URL}/api/auth/me`, { headers: authHeaders() })
}

// ---------------------------------------------------------------------------
// row shape
// ---------------------------------------------------------------------------

/**
 * The backend returns filename/concepts/size_kb; Builder's table renders
 * name/nodes/type/upload_status. Map between them here rather than editing
 * Builder, so the existing table keeps working untouched. Backend names are
 * preserved alongside, so newer components can use either.
 */
function toRow(doc) {
  const ext = (doc.filename?.split('.').pop() || '').toUpperCase()
  const label = {
    done: 'Processed', ready: 'Processed', error: 'Failed',
    queued: 'Queued', running: 'Processing',
  }[(doc.status || '').toLowerCase()] || 'Uploaded'

  return {
    ...doc,
    name: doc.filename,
    type: ext,
    size: doc.size_kb,
    status: doc.status === 'done' ? 'Ready' : (doc.status || 'New'),
    upload_status: label,
    nodes: doc.concepts ?? 0,
    relationships: doc.relationships ?? 0,
    source: doc.week || 'upload',
    model: 'openai/gpt-oss-120b',
  }
}

// ---------------------------------------------------------------------------
// documents
// ---------------------------------------------------------------------------

export async function listDocuments() {
  const data = await apiFetch(`${API_BASE_URL}/api/documents`, { headers: authHeaders() })
  return { ...data, documents: (data.documents || []).map(toRow) }
}

export async function patchDocument(id, fields) {
  return apiFetch(`${API_BASE_URL}/api/documents/${id}`, {
    method: 'PATCH',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(fields),
  })
}

export async function deleteDocument(id) {
  return apiFetch(`${API_BASE_URL}/api/documents/${id}`, {
    method: 'DELETE',
    headers: authHeaders(),
  })
}

/**
 * Upload is deliberately two calls:
 *   1. POST /api/documents  — our row, so the table shows the file at once
 *   2. POST /api/upload     — main.py's extraction pipeline
 * Extraction runs in a background task, so the status comes from polling
 * /api/upload/status rather than from the upload response.
 *
 * onStatus receives each pipeline status object, for a progress line.
 */
export async function uploadDocument(
  file,
  seedMethod = 'llm',
  maxNodes = null,
  onStatus = null,
  minMentions = null,
) {
  // Signature kept as Builder already calls it. seedMethod / maxNodes /
  // minMentions are accepted and ignored: main.py's /api/upload takes
  // max_nodes and min_mentions as query params, which the current pipeline
  // does not act on, and inventing behaviour here would be worse than saying
  // so plainly.
  const form = new FormData()
  form.append('file', file)

  const created = await apiFetch(`${API_BASE_URL}/api/documents`, {
    method: 'POST',
    headers: authHeaders(),
    body: form,
  })
  const doc = created.document

  try {
    await patchDocument(doc.id, { status: 'running' })

    const form2 = new FormData()
    form2.append('file', file)
    const ack = await apiFetch(`${API_BASE_URL}/api/upload`, {
      method: 'POST',
      body: form2,
    })
    if (ack?.week) await patchDocument(doc.id, { week: ack.week })

    // Reuses the repo's existing pollUntilDone(pollFn, opts) helper rather
    // than defining a second polling loop.
    // main.py keeps a single global pipeline_status, so a status naming a
    // different file belongs to another upload. Treat a long run of those as
    // a stall instead of polling until the ten-minute timeout.
    let foreign = 0
    const final = await pollUntilDone(
      async () => {
        const status = await getPipelineStatus()
        if (onStatus) onStatus(status)
        if (status.filename && status.filename !== file.name) {
          foreign += 1
          if (foreign > 20) {
            return { stage: 'error', error: 'Another upload took over the pipeline. Try again once it finishes.' }
          }
          return { stage: 'running', operation: 'waiting for the pipeline' }
        }
        foreign = 0
        return status
      },
      { intervalMs: 1500 },
    )

    if (final?.stage === 'error') {
      const updated = await patchDocument(doc.id, {
        status: 'error',
        error: final.error || 'Extraction failed.',
      })
      return { document: toRow(updated.document), raw: final }
    }

    const updated = await patchDocument(doc.id, {
      status: 'done',
      // main.py reports raw_concepts; the dev stub used to say total_concepts
      concepts: final?.raw_concepts ?? final?.total_concepts ?? 0,
      relationships: final?.total_edges ?? 0,
    })
    return { document: toRow(updated.document), raw: final }
  } catch (err) {
    // The row exists and the file is stored; only extraction failed. Record
    // that on the row rather than losing the upload.
    await patchDocument(doc.id, { status: 'error', error: err.message }).catch(() => {})
    throw err
  }
}

// ---------------------------------------------------------------------------
// quiz
// ---------------------------------------------------------------------------

/** Topics and questions generated from the concepts in the uploaded documents. */
export async function fetchQuiz() {
  return apiFetch(`${API_BASE_URL}/api/quiz`, { headers: authHeaders() })
}

export async function saveQuizResult(quizId, correct, total, points) {
  return apiFetch(`${API_BASE_URL}/api/quiz/results`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ quiz_id: quizId, correct, total, points }),
  })
}

/** Per-document concept/edge counts, used to reconcile stuck rows. */
export async function fetchDocumentStats() {
  return apiFetch(`${API_BASE_URL}/api/documents/stats`, { headers: authHeaders() })
}

// ---------------------------------------------------------------------------
// revision notes
//
// Two steps, deliberately split. Planning groups the concepts into sections
// and leaves every body empty; writing fills one body in and is only called
// when a reader opens that section. Both are slow — planning runs one or more
// LLM calls, writing runs twenty to thirty seconds — so callers need a busy
// state for each.
// ---------------------------------------------------------------------------

/** Plans a note over the given documents. `days` splits it; null does not. */
export async function planNote(documentIds, days = null) {
  return apiFetch(`${API_BASE_URL}/api/notes/plan`, {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ document_ids: documentIds, days }),
  })
}

/** Notes planned in this backend process, newest first. */
export async function fetchNotes() {
  return apiFetch(`${API_BASE_URL}/api/notes`, { headers: authHeaders() })
}

export async function fetchNote(noteId) {
  return apiFetch(`${API_BASE_URL}/api/notes/${noteId}`, { headers: authHeaders() })
}

/**
 * Writes one section's prose, or returns what was written before.
 *
 * Slow on the first open and immediate afterwards — the response's
 * `generated` flag says which happened, and the section comes back with its
 * `body` filled in either way.
 */
export async function writeSection(sectionId) {
  return apiFetch(`${API_BASE_URL}/api/notes/sections/${sectionId}/write`, {
    method: 'POST',
    headers: authHeaders(),
  })
}
