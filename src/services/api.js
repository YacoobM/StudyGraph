const API_BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
import { supabase } from "./supabase.js";

async function handleResponse(response) {
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const err = new Error(
      data?.detail ||
        data?.error ||
        response?.statusText ||
        "API request failed",
    );
    err.response = { data, status: response.status };
    const retryAfter = Number(response.headers.get("Retry-After"));
    if (Number.isFinite(retryAfter) && retryAfter > 0)
      err.retryAfter = retryAfter;
    throw err;
  }
  return data;
}

async function safeFetchJson(url, options = {}) {
  const response = await fetch(url, options);
  return handleResponse(response);
}

export async function listGraphs() {
  return apiFetch(`${API_BASE_URL}/api/graphs`, {
    headers: await authHeaders(),
  });
}

export async function listLibrary() {
  return apiFetch(`${API_BASE_URL}/api/library`, {
    headers: await authHeaders(),
  });
}

export async function createSubject(name) {
  return apiFetch(`${API_BASE_URL}/api/subjects`, {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ name }),
  });
}

export async function updateSubject(subjectId, name) {
  return apiFetch(`${API_BASE_URL}/api/subjects/${encodeURIComponent(subjectId)}`, {
    method: "PATCH",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ name }),
  });
}

export async function deleteSubject(subjectId) {
  return apiFetch(`${API_BASE_URL}/api/subjects/${encodeURIComponent(subjectId)}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
}

export async function createTopic(subjectId, name) {
  return apiFetch(`${API_BASE_URL}/api/topics`, {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ subject_id: subjectId, name }),
  });
}

export async function updateTopic(topicId, name) {
  return apiFetch(`${API_BASE_URL}/api/topics/${encodeURIComponent(topicId)}`, {
    method: "PATCH",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ name }),
  });
}

export async function deleteTopic(topicId) {
  return apiFetch(`${API_BASE_URL}/api/topics/${encodeURIComponent(topicId)}`, {
    method: "DELETE",
    headers: await authHeaders(),
  });
}

export async function createGraph(name, topicId = null) {
  return apiFetch(`${API_BASE_URL}/api/graphs`, {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ name, topic_id: topicId }),
  });
}

export async function updateGraph(graphId, name) {
  return apiFetch(`${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}`, {
    method: "PATCH",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ name }),
  });
}

export async function listGraphVersions(graphId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/versions`,
    {
      headers: await authHeaders(),
    },
  );
}

export async function getGraphActiveSupport(graphId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/support`,
    {
      headers: await authHeaders(),
    },
  );
}

export async function fetchGraphVersion(graphId, versionId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/versions/${encodeURIComponent(versionId)}?refresh=${Date.now()}`,
    {
      headers: await authHeaders(),
      cache: "no-store",
    },
  );
}

export async function listGraphDocuments(graphId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/documents`,
    {
      headers: await authHeaders(),
    },
  );
}

export async function uploadGraphDocument(graphId, file) {
  const form = new FormData();
  form.append("file", file);
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/documents`,
    {
      method: "POST",
      headers: await authHeaders(),
      body: form,
    },
  );
}

export async function createGraphVersion(graphId, documentIds, options = {}) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/versions`,
    {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        document_ids: documentIds,
        max_nodes_per_document: options.maxNodesPerDocument ?? null,
        min_mentions: options.minMentions ?? 1,
      }),
    },
  );
}

export async function deleteGraphDocument(graphId, documentId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/documents/${encodeURIComponent(documentId)}`,
    { method: "DELETE", headers: await authHeaders() },
  );
}

export async function askGraphQuestion(graphId, question) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/query`,
    {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ query: question }),
    },
  );
}

export async function regenerateGraph() {
  return safeFetchJson(`${API_BASE_URL}/api/graph/regenerate`, {
    method: "POST",
  });
}

export function getDocumentFileUrl(filename) {
  return `${API_BASE_URL}/api/documents/file/${encodeURIComponent(filename)}`;
}

export async function refreshDegradedConcepts(graphId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/concepts/refresh-degraded`,
    { method: 'POST', headers: await authHeaders() },
  )
}

export async function getGraphDocumentSignedUrl(graphId, documentId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/documents/${encodeURIComponent(documentId)}/file`,
    { headers: await authHeaders() },
  );
}

export async function regenerateConcept(conceptName) {
  return safeFetchJson(
    `${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}/regenerate`,
    {
      method: "POST",
    },
  );
}

function buildGraphPayload(raw) {
  if (!raw) return { nodes: [], relationships: [], stats: {} };

  const nodes = Array.isArray(raw.nodes)
    ? raw.nodes.map((node) => {
        const {
          id,
          name,
          description,
          source,
          is_canonical,
          color,
          appears_in_weeks,
          ...rest
        } = node;
        return {
          id,
          label: name || "Concept",
          // Use the color from the backend, with a fallback for older data structures.
          color: color || (is_canonical ? "#4D7C0F" : "#6D28D9"),
          properties: {
            ...rest,
            ...(name ? { name } : {}),
            ...(description ? { description } : {}),
            ...(source ? { source } : {}),
            ...(appears_in_weeks !== undefined ? { appears_in_weeks } : {}),
          },
        };
      })
    : [];

  const relationshipsSource = raw.edges || raw.relationships || [];
  const relationships = Array.isArray(relationshipsSource)
    ? relationshipsSource.map((rel) => {
        const {
          id,
          source,
          target,
          relation,
          label,
          summary,
          context,
          ...rest
        } = rel;
        return {
          id: id || `${source}_${target}`,
          source,
          target,
          type: relation || label || "RELATED_TO",
          properties: {
            ...rest,
            ...(summary ? { summary } : {}),
            ...(context ? { context } : {}),
          },
        };
      })
    : [];

  const stats = raw.stats || {
    total_nodes: nodes.length,
    total_relationships: relationships.length,
  };

  return {
    nodes,
    relationships,
    stats,
    is_fused: !!raw.is_fused,
    status: raw.status || "active",
  };
}

export async function getGraph() {
  const rawGraph = await safeFetchJson(`${API_BASE_URL}/api/graph`);
  return buildGraphPayload(rawGraph);
}

export async function askQuestion(question) {
  const response = await safeFetchJson(`${API_BASE_URL}/api/query`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: question }),
  })
  console.log(response)
  return response
}

// Cheap tier: one grounded summary from the concept's earliest-appearance
// week only. Use this for a plain node click — it's what backs the
// "DESCRIPTION" text in both the general graph and the Evolution graph.
export async function fetchConceptDescription(conceptName) {
  return safeFetchJson(
    `${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}/description`,
  );
}

// Full tier: every week's summary plus every week-to-week comparison. Costs
// one LLM call per week/transition, so only call this from an explicit
// "show concept evolution" action, not on a plain node click.
export async function fetchConcept(conceptName) {
  return safeFetchJson(
    `${API_BASE_URL}/api/evolution/concept/${encodeURIComponent(conceptName)}`,
  );
}

export async function fetchGraphConceptEvolution(graphId, conceptName) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/concepts/${encodeURIComponent(conceptName)}/evolution`,
    {
      headers: await authHeaders(),
    },
  );
}

export async function fetchConceptSubgraph(conceptName) {
  return safeFetchJson(
    `${API_BASE_URL}/api/evolution/subgraph/${encodeURIComponent(conceptName)}`,
  );
}

// Supabase owns token storage and refreshes expired sessions for us.
async function authHeaders(extra = {}) {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  const token = session?.access_token;
  return token ? { ...extra, Authorization: `Bearer ${token}` } : extra;
}

/** Turns a transport failure into a message that names the real cause. */
async function apiFetch(url, options = {}) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (e) {
    // A caller that aborted on purpose gets its own error back. Reported as
    // offline it would tell the reader to go and start a backend that is
    // already running.
    if (e?.name === "AbortError") throw e;
    const err = new Error(
      `Cannot reach the backend at ${API_BASE_URL}. Make sure the backend is running!`,
    );
    err.offline = true;
    throw err;
  }
  return handleResponse(response);
}

// ---------------------------------------------------------------------------
// quiz
// ---------------------------------------------------------------------------

/** Topics and questions generated from the concepts in the uploaded documents. */
export async function fetchQuiz(graphId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/quiz`,
    { headers: await authHeaders() },
  );
}

export async function regenerateGraphQuiz(graphId) {
  return apiFetch(`${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/quiz/regenerate`, {
    method: "POST",
    headers: await authHeaders(),
  });
}

// Compatibility endpoint for the older per-document quiz view. The current
// Supabase graph flow uses fetchQuiz(graphId), which loads or creates the
// persisted quiz for the latest graph version.
export async function generateDocumentQuiz(_graphId, documentId) {
  return apiFetch(
    `${API_BASE_URL}/api/quiz/documents/${encodeURIComponent(documentId)}/generate`,
    {
      method: "POST",
      headers: await authHeaders(),
    },
  );
}

export async function saveQuizResult(graphId, quizId, correct, total, points) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/quiz/results`,
    {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        quiz_id: quizId,
        correct,
        total,
        points,
        completed: true,
      }),
    },
  );
}

export async function saveQuizProgress(
  graphId,
  quizId,
  currentIndex,
  answers,
  correct,
  total,
  points,
  completed = false,
) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/quiz/results`,
    {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        quiz_id: quizId,
        current_index: currentIndex,
        answers,
        correct,
        total,
        points,
        completed,
      }),
    },
  );
}

export async function saveNoteQuizProgress(graphId, sectionId, currentIndex, answers, correct, total, points, completed = false) {
  return apiFetch(`${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/notes/sections/${encodeURIComponent(sectionId)}/quiz-progress`, {
    method: "POST",
    headers: await authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ section_id: sectionId, current_index: currentIndex, answers, correct, total, points, completed }),
  });
}

/**
 * Every section quiz the user has been given, grouped by note title. Read
 * from the server's stored copy, so it is there after a restart even though
 * the notes themselves are not.
 */
export async function fetchSectionQuizzes() {
  return apiFetch(`${API_BASE_URL}/api/quiz/sections`, {
    headers: await authHeaders(),
  });
}

/** Every stored attempt, summarised per quiz id. */
export async function fetchQuizResults() {
  return apiFetch(`${API_BASE_URL}/api/quiz/results`, {
    headers: await authHeaders(),
  });
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
export async function planNote(graphId, documentIds, days = null) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/notes/plan`,
    {
      method: "POST",
      headers: await authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ document_ids: documentIds, days }),
    },
  );
}

/** Notes planned in this backend process, newest first. */
export async function fetchNotes(graphId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/notes`,
    {
      headers: await authHeaders(),
    },
  );
}

export async function fetchNote(graphId, noteId) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/notes/${noteId}`,
    {
      headers: await authHeaders(),
    },
  );
}

/**
 * Writes one section's prose, or returns what was written before.
 *
 * Slow on the first open and immediate afterwards — the response's
 * `generated` flag says which happened, and the section comes back with its
 * `body` filled in either way.
 */
export async function writeSection(
  graphId,
  noteId,
  sectionId,
  { signal } = {},
) {
  return apiFetch(
    `${API_BASE_URL}/api/graphs/${encodeURIComponent(graphId)}/notes/${noteId}/sections/${sectionId}/write`,
    {
      method: "POST",
      headers: await authHeaders(),
      signal,
    },
  );
}
