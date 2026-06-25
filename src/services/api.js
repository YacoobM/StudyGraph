/* ==========================================================================
 *  StudyGraph — API SERVICE LAYER  (MOCK / STUB)
 * ==========================================================================
 *
 *  ⚠️  INTEGRATION POINT #1 — BACKEND / GRAPH CONSTRUCTION  ⚠️
 *
 *  This file is the ONLY place the frontend talks to a backend.
 *  Right now every function returns FAKE (mock) data so the UI runs
 *  standalone with no server. The teammate responsible for the backend /
 *  graph-construction module should replace the body of each function
 *  below with a real HTTP call.
 *
 *  HOW TO INTEGRATE
 *  ----------------
 *  1. npm install axios
 *  2. Uncomment the axios client at the bottom of this file.
 *  3. Replace each `return mock...` line with the matching real request,
 *     e.g.   return api.post('/auth/login', { email, password })
 *               .then(r => r.data)
 *  4. Keep the FUNCTION SIGNATURES and RETURN SHAPES identical to the mocks
 *     so the rest of the frontend keeps working without changes.
 *
 *  The exact data shapes the frontend expects are documented above each
 *  function. This is the CONTRACT between frontend and backend — do not
 *  change the shape, only change where the data comes from.
 *
 * ========================================================================== */

// Small helper so the mock feels like a real network call (adds a delay).
const fakeDelay = (ms = 600) => new Promise((res) => setTimeout(res, ms));

let _mockDocId = 1;

/* --------------------------------------------------------------------------
 *  AUTH
 * --------------------------------------------------------------------------
 *  login(email, password)
 *    → { token: string, user: { email, name } }
 *  register(email, password, name)
 *    → { token: string, user: { email, name } }
 * ------------------------------------------------------------------------ */
export async function login(email, password) {
  await fakeDelay();
  // MOCK: accept the demo account, reject everything else.
  // BACKEND TEAMMATE: replace with  POST /api/auth/login
  if (email === 'demo@graph.com' && password === 'demo123') {
    return { token: 'mock-token-123', user: { email, name: 'Demo User' } };
  }
  // Allow any non-empty login in mock mode so the UI is easy to demo.
  if (email && password) {
    return { token: 'mock-token-123', user: { email, name: email.split('@')[0] } };
  }
  const err = new Error('Invalid credentials');
  err.response = { data: { error: 'Invalid credentials' } };
  throw err;
}

export async function register(email, password, name) {
  await fakeDelay();
  // BACKEND TEAMMATE: replace with  POST /api/auth/register
  return { token: 'mock-token-123', user: { email, name: name || email.split('@')[0] } };
}

/* --------------------------------------------------------------------------
 *  MODELS  (for the left-sidebar model picker)
 * --------------------------------------------------------------------------
 *  listModels()
 *    → {
 *        providers: [
 *          { id, name, available: bool, env_var, models: [{id, label}] }
 *        ],
 *        default: { provider, model }
 *      }
 * ------------------------------------------------------------------------ */
export async function listModels() {
  await fakeDelay(300);
  // BACKEND TEAMMATE: replace with  GET /api/models
  return {
    providers: [
      {
        id: 'groq', name: 'Groq', available: true, env_var: 'GROQ_API_KEY',
        models: [
          { id: 'llama-3.1-8b-instant', label: 'Llama 3.1 8B (fast)' },
          { id: 'llama-3.3-70b-versatile', label: 'Llama 3.3 70B (smart)' },
          { id: 'mixtral-8x7b-32768', label: 'Mixtral 8x7B' },
        ],
      },
      {
        id: 'gemini', name: 'Google Gemini', available: false, env_var: 'GOOGLE_API_KEY',
        models: [
          { id: 'gemini-2.0-flash', label: 'Gemini 2.0 Flash' },
          { id: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash' },
          { id: 'gemini-2.5-pro', label: 'Gemini 2.5 Pro' },
        ],
      },
      {
        id: 'openai', name: 'OpenAI', available: false, env_var: 'OPENAI_API_KEY',
        models: [
          { id: 'gpt-4o-mini', label: 'GPT-4o mini' },
          { id: 'gpt-4o', label: 'GPT-4o' },
        ],
      },
      {
        id: 'local', name: 'Local', available: true, env_var: null,
        models: [{ id: 'regex', label: 'Rule-based (no API)' }],
      },
    ],
    default: { provider: 'groq', model: 'llama-3.1-8b-instant' },
  };
}

/* --------------------------------------------------------------------------
 *  DOCUMENTS
 * --------------------------------------------------------------------------
 *  listDocuments()        → { documents: [Doc] }
 *  uploadDocument(file)   → { document: Doc }
 *  deleteDocument(id)     → { ok: true }
 *
 *  Doc shape:
 *    {
 *      id, name, size_kb, type, source, model,
 *      upload_status: 'Uploaded',
 *      status: 'New' | 'Processing' | 'Completed' | 'Failed',
 *      nodes: number, relationships: number
 *    }
 * ------------------------------------------------------------------------ */
let _docs = []; // in-memory mock store

export async function listDocuments() {
  await fakeDelay(200);
  // BACKEND TEAMMATE: replace with  GET /api/documents
  return { documents: _docs };
}

export async function uploadDocument(file) {
  await fakeDelay();
  // BACKEND TEAMMATE: replace with  POST /api/upload  (multipart/form-data)
  const doc = {
    id: `mock-doc-${_mockDocId++}`,
    name: file.name,
    size_kb: Math.round(file.size / 1024 * 100) / 100,
    type: (file.name.split('.').pop() || '').toUpperCase(),
    source: 'local file',
    model: '—',
    upload_status: 'Uploaded',
    status: 'New',
    nodes: 0,
    relationships: 0,
  };
  _docs = [..._docs, doc];
  return { document: doc };
}

export async function deleteDocument(id) {
  await fakeDelay(200);
  // BACKEND TEAMMATE: replace with  DELETE /api/documents/:id
  _docs = _docs.filter((d) => d.id !== id);
  return { ok: true };
}

/* --------------------------------------------------------------------------
 *  GENERATE GRAPH  (the graph-construction trigger)
 * --------------------------------------------------------------------------
 *  generateGraph(docIds, provider, model)
 *    → { documents: [Doc] }   // each Doc now has status:'Completed',
 *                             // and nodes / relationships counts filled in
 *
 *  BACKEND TEAMMATE: this should call your extraction pipeline
 *  (POST /api/generate-graph) which reads the uploaded files, chunks them,
 *  runs entity/relationship extraction, and stores the resulting graph.
 * ------------------------------------------------------------------------ */
export async function generateGraph(docIds, provider, model) {
  await fakeDelay(1200);
  // MOCK: pretend extraction happened and fill in fake counts.
  _docs = _docs.map((d) =>
    docIds.includes(d.id)
      ? { ...d, status: 'Completed', model: `${provider}/${model}`,
          nodes: 24, relationships: 41 }
      : d
  );
  return { documents: _docs };
}

/* --------------------------------------------------------------------------
 *  GET GRAPH  (consumed by the GRAPH VISUALIZATION module)
 * --------------------------------------------------------------------------
 *  getGraph()
 *    → {
 *        nodes: [
 *          { id, label, color, properties: { name?, fileName?, ... } }
 *        ],
 *        relationships: [
 *          { id, source, target, type, properties: {} }
 *        ],
 *        stats: {
 *          total_nodes, total_relationships,
 *          label_counts: { [label]: count },
 *          rel_counts:   { [type]:  count }
 *        }
 *      }
 *
 *  ⚠️  This is the CONTRACT for INTEGRATION POINT #2 (visualization).
 *  The visualization teammate reads exactly this shape. Keep it stable.
 * ------------------------------------------------------------------------ */
export async function getGraph() {
  await fakeDelay(400);
  // BACKEND TEAMMATE: replace with  GET /api/graph
  // (returns a small hard-coded sample so the visualization teammate
  //  has real-looking data to develop against)
  return SAMPLE_GRAPH;
}

/* --------------------------------------------------------------------------
 *  CHAT  (RAG Q&A — optional, owned by backend teammate)
 * --------------------------------------------------------------------------
 *  askQuestion(question)
 *    → { answer: string, sources: [{ fileName, page_number, snippet }] }
 * ------------------------------------------------------------------------ */
export async function askQuestion(question) {
  await fakeDelay(800);
  // BACKEND TEAMMATE: replace with  POST /api/chat
  return {
    answer:
      `**(Mock answer)** The backend Q&A module is not connected yet. ` +
      `Once the RAG engine is wired up, this will return a real answer to: "${question}".`,
    sources: [],
  };
}

/* ==========================================================================
 *  SAMPLE GRAPH DATA  (for visualization development only)
 *  Delete this once the real /api/graph endpoint is connected.
 * ========================================================================== */
const SAMPLE_GRAPH = {
  nodes: [
    { id: 'd1', label: 'Document', color: '#22d3ee', properties: { fileName: 'FIT3155_week2.pptx' } },
    { id: 'e1', label: 'Algorithm', color: '#fbbf24', properties: { name: 'Boyer-Moore', mentions: 6 } },
    { id: 'e2', label: 'Algorithm', color: '#fbbf24', properties: { name: 'Z-Algorithm', mentions: 4 } },
    { id: 'e3', label: 'Algorithm', color: '#fbbf24', properties: { name: 'Knuth-Morris-Pratt', mentions: 3 } },
    { id: 'e4', label: 'Rule', color: '#f87171', properties: { name: 'Bad-Character Rule', mentions: 2 } },
    { id: 'e5', label: 'Rule', color: '#f87171', properties: { name: 'Good-Suffix Rule', mentions: 2 } },
    { id: 'e6', label: 'Course', color: '#ec4899', properties: { name: 'FIT3155', mentions: 5 } },
    { id: 'e7', label: 'Tool', color: '#d8b4fe', properties: { name: 'GREP', mentions: 2 } },
    { id: 'e8', label: 'Organization', color: '#fbcfe8', properties: { name: 'Monash University', mentions: 3 } },
  ],
  relationships: [
    { id: 'r1', source: 'e1', target: 'e4', type: 'USES', properties: {} },
    { id: 'r2', source: 'e1', target: 'e5', type: 'USES', properties: {} },
    { id: 'r3', source: 'e2', target: 'e1', type: 'RELATED_TO', properties: {} },
    { id: 'r4', source: 'e3', target: 'e1', type: 'RELATED_TO', properties: {} },
    { id: 'r5', source: 'e7', target: 'e1', type: 'IMPLEMENTS', properties: {} },
    { id: 'r6', source: 'e6', target: 'e8', type: 'PART_OF', properties: {} },
    { id: 'r7', source: 'e6', target: 'e1', type: 'COVERS', properties: {} },
    { id: 'r8', source: 'e6', target: 'e2', type: 'COVERS', properties: {} },
  ],
  stats: {
    total_nodes: 9,
    total_relationships: 8,
    label_counts: { Document: 1, Algorithm: 3, Rule: 2, Course: 1, Tool: 1, Organization: 1 },
    rel_counts: { USES: 2, RELATED_TO: 2, IMPLEMENTS: 1, PART_OF: 1, COVERS: 2 },
  },
};

/* ==========================================================================
 *  REAL AXIOS CLIENT  — uncomment when integrating the backend
 * ==========================================================================
 *
 * import axios from 'axios';
 *
 * const api = axios.create({ baseURL: '/api' });
 *
 * api.interceptors.request.use((config) => {
 *   const token = localStorage.getItem('sg_token');
 *   if (token) config.headers.Authorization = `Bearer ${token}`;
 *   return config;
 * });
 *
 * // Then rewrite the functions above, for example:
 * //
 * //   export async function login(email, password) {
 * //     const { data } = await api.post('/auth/login', { email, password });
 * //     return data;
 * //   }
 * //
 * //   export async function getGraph() {
 * //     const { data } = await api.get('/graph');
 * //     return data;
 * //   }
 *
 * ========================================================================== */
