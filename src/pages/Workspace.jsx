import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import ConceptGraph from "../components/ConceptGraph.jsx";
import QuizPane from "../components/QuizPane.jsx";
import Inspector from "../components/Inspector.jsx";
import NotesPane from "../components/NotesPane.jsx";
import {
  listGraphs,
  createGraph as createSupabaseGraph,
  listGraphVersions,
  getGraphActiveSupport,
  fetchGraphVersion,
  listGraphDocuments,
  uploadGraphDocument,
  createGraphVersion,
  deleteGraphDocument,
  askGraphQuestion,
} from "../services/api.js";
import "../styles/studygraph.css";
import "./Workspace.css";

const TABS = ["graph", "documents", "chat", "notes", "quiz"];
const ALLOWED = ["pdf", "docx", "pptx", "txt", "md"];
const extOf = (n) => (n.split(".").pop() || "").toLowerCase();

function statusClass(s) {
  const v = (s || "").toLowerCase();
  if (["done", "ready", "processed", "complete", "completed"].includes(v))
    return "ready";
  if (
    [
      "running",
      "processing",
      "extracting",
      "building graph",
      "seeding",
    ].includes(v)
  )
    return "working";
  if (["error", "failed"].includes(v)) return "failed";
  return "queued";
}
function statusLabel(s) {
  const v = (s || "").toLowerCase();
  if (["done", "ready", "processed", "complete", "completed"].includes(v))
    return "Ready";
  if (["error", "failed"].includes(v)) return "Failed";
  if (v === "queued") return "Queued";
  return s ? s[0].toUpperCase() + s.slice(1) : "Queued";
}
const fmtSize = (kb) =>
  kb == null ? "—" : kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb} KB`;
function fmtWhen(ts) {
  if (!ts) return "—";
  const m = Math.round((Date.now() - ts * 1000) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} hr ago` : `${Math.round(h / 24)} d ago`;
}

function weekNumber(value) {
  const match = String(value || "").match(/\d+/);
  return match ? Number(match[0]) : 1;
}

function graphViewFromSnapshot(snapshot, activeSupport = null, documents = []) {
  const concepts = snapshot?.concepts || [];
  const relationships = snapshot?.relationships || [];
  const conceptProvenance = snapshot?.concept_provenance || [];
  const documentsById = new Map(
    documents.map((document) => [String(document.id), document]),
  );
  const degree = {};
  for (const relationship of relationships) {
    degree[relationship.source_concept_id] =
      (degree[relationship.source_concept_id] || 0) + 1;
    degree[relationship.target_concept_id] =
      (degree[relationship.target_concept_id] || 0) + 1;
  }
  const ranked = concepts
    .map((concept) => degree[concept.id] || 0)
    .sort((a, b) => b - a);
  const cut = Math.max(1, ranked[Math.floor(ranked.length / 3)] ?? 1);
  const nodes = concepts.map((concept) => {
    const provenanceSources = conceptProvenance
      .filter((row) => String(row.concept_id) === String(concept.id))
      .map((row) => ({
        name: row.document_filename,
        week: Number(row.source_week) || weekNumber(
          documentsById.get(String(row.document_id))?.week,
        ),
        weekLabel: `Week ${Number(row.source_week) || weekNumber(documentsById.get(String(row.document_id))?.week)}`,
        documentId: row.document_id,
        chunkId: row.chunk_id,
        deleted: !documentsById.has(String(row.document_id)),
      }));
    const fallbackSources = (activeSupport?.concepts?.[concept.normalized_name] || [])
      .map((documentId) => documentsById.get(String(documentId)))
      .filter(Boolean)
      .map((document) => ({
        name: document.filename,
        week: weekNumber(document.week),
        weekLabel: document.week || `Week ${weekNumber(document.week)}`,
        documentId: document.id,
      }));
    const sources = [...new Map(
      [...provenanceSources, ...fallbackSources].map((source) => [
        String(source.documentId),
        source,
      ]),
    ).values()];
    const weeks = [...new Set(sources.map((source) => source.week))].sort(
      (a, b) => a - b,
    );

    return {
      id: String(concept.id),
      name: concept.name,
      description: "",
      status: concept.status || "active",
      week: weeks[0] || 1,
      weeks: weeks.length ? weeks : [1],
      degree: degree[concept.id] || 0,
      kind: (degree[concept.id] || 0) >= cut ? "core" : "support",
      sources,
    };
  });
  const edges = relationships.map((relationship) => ({
    source: String(relationship.source_concept_id),
    target: String(relationship.target_concept_id),
    label: relationship.relationship_type || "relates to",
    strength: "medium",
  }));
  return { nodes, edges };
}

function graphDocumentRow(document) {
  return {
    ...document,
    name: document.filename,
    size_kb: Math.max(1, Math.round((Number(document.size_bytes) || 0) / 1024)),
    uploaded_at:
      typeof document.uploaded_at === "string"
        ? Date.parse(document.uploaded_at) / 1000
        : document.uploaded_at,
    nodes: document.concepts ?? 0,
    relationships: document.relationships ?? 0,
  };
}

export default function Workspace() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const [tab, setTab] = useState("graph");
  const [graphs, setGraphs] = useState([]);
  const [selectedGraphId, setSelectedGraphId] = useState("");
  const [versions, setVersions] = useState([]);
  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [creatingGraph, setCreatingGraph] = useState(false);
  const [graphName, setGraphName] = useState("");
  const [docs, setDocs] = useState([]);
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [drag, setDrag] = useState(false);
  const setBusyToast = (msg) => {
    setBusy(msg);
    setTimeout(() => setBusy(""), 1800);
  };
  const fileInput = useRef(null);

  // Notes are written from documents the engine has finished reading, so the
  // picker there only offers rows that have settled.
  const readyDocs = useMemo(
    () => docs.filter((d) => statusClass(d.status) === "ready"),
    [docs],
  );

  const loadGraphs = useCallback(async () => {
    try {
      const result = await listGraphs();
      setGraphs(result.graphs || []);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }, []);

  const loadGraphWorkspace = useCallback(
    async (graphId, requestedVersionId = null) => {
      if (!graphId) return;
      const [{ documents = [] }, { versions: graphVersions = [] }] =
        await Promise.all([
          listGraphDocuments(graphId),
          listGraphVersions(graphId),
        ]);
      setDocs(documents.map(graphDocumentRow));
      setVersions(graphVersions);
      const version =
        graphVersions.find((item) => item.id === requestedVersionId) ||
        graphVersions[0];
      setSelectedVersionId(version?.id || "");
      if (!version) {
        setGraph({ nodes: [], edges: [] });
        setSelected(null);
        return;
      }
      const snapshot = await fetchGraphVersion(graphId, version.id);
      const activeSupport =
        graphVersions[0]?.id === version.id
          ? await getGraphActiveSupport(graphId)
          : null;
      setGraph(graphViewFromSnapshot(snapshot, activeSupport, documents));
      setSelected(null);
    },
    [],
  );

  useEffect(() => {
    loadGraphs();
  }, [loadGraphs]);

  async function selectGraph(graphId) {
    setSelectedGraphId(graphId);
    setSelectedVersionId("");
    setDocs([]);
    setVersions([]);
    setGraph({ nodes: [], edges: [] });
    setSelected(null);
    setError("");
    if (!graphId) return;
    setBusy("Loading graph…");
    try {
      await loadGraphWorkspace(graphId);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function createGraphFromForm(event) {
    event.preventDefault();
    const name = graphName.trim();
    if (!name) return;
    setBusy("Creating graph…");
    try {
      const { graph: created } = await createSupabaseGraph(name);
      setGraphs((current) => [created, ...current]);
      setGraphName("");
      setCreatingGraph(false);
      await selectGraph(created.id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function selectVersion(versionId) {
    setSelectedVersionId(versionId);
    if (!selectedGraphId || !versionId) return;
    setBusy("Loading version…");
    setTab("graph");
    try {
      await loadGraphWorkspace(selectedGraphId, versionId);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  // --- upload -------------------------------------------------------------

  async function handleFile(file) {
    if (!file) return;
    if (!selectedGraphId) {
      setError("Choose a graph before uploading documents.");
      return;
    }
    if (versions.length && selectedVersionId !== versions[0]?.id) {
      setError("Select the latest graph version before uploading documents.");
      return;
    }
    const ext = extOf(file.name);
    if (!ALLOWED.includes(ext)) {
      setError(
        `${file.name} is a .${ext} file. Use PDF, DOCX, PPTX, TXT or MD.`,
      );
      return;
    }

    setError("");
    setBusy(`Uploading ${file.name}…`);
    try {
      const { document } = await uploadGraphDocument(selectedGraphId, file);
      await createGraphVersion(selectedGraphId, [document.id]);
      await loadGraphWorkspace(selectedGraphId);
      setTab("graph");
    } catch (e) {
      setError(`Could not process ${file.name}: ${e.message}`);
      await loadGraphWorkspace(selectedGraphId).catch(() => {});
    } finally {
      setBusy("");
    }
  }

  async function remove(doc) {
    if (
      !selectedGraphId ||
      (versions.length && selectedVersionId !== versions[0]?.id)
    )
      return;
    try {
      await deleteGraphDocument(selectedGraphId, doc.id);
      await loadGraphWorkspace(selectedGraphId);
    } catch (e) {
      setError(`Could not remove ${doc.name || doc.filename}: ${e.message}`);
    }
  }

  function newGraph() {
    setCreatingGraph(true);
    setGraphName("");
    setError("");
  }

  async function signOut() {
    await logout();
    navigate("/login", { replace: true });
  }

  const t = {
    count: docs.length,
    concepts: graph.nodes.length,
    relationships: graph.edges.length,
  };
  const selectedGraph = graphs.find((item) => item.id === selectedGraphId);
  const viewingLatestVersion =
    !versions.length || selectedVersionId === versions[0]?.id;
  const evolutionMaxWeek = Math.max(1, ...docs.map((document) => weekNumber(document.week)));

  return (
    <div className="ws">
      {/* ---------------- sidebar ---------------- */}
      <aside className="ws-side">
        <div className="side-brand" onClick={() => navigate("/")}>
          StudyGraph
        </div>

        <button className="side-new" onClick={newGraph}>
          + New graph
        </button>

        {creatingGraph && (
          <form className="graph-create" onSubmit={createGraphFromForm}>
            <input
              autoFocus
              aria-label="Graph name"
              placeholder="Graph name"
              value={graphName}
              onChange={(event) => setGraphName(event.target.value)}
            />
            <div>
              <button
                type="button"
                className="row-btn"
                onClick={() => setCreatingGraph(false)}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="row-btn graph-create-submit"
                disabled={!graphName.trim() || Boolean(busy)}
              >
                Create
              </button>
            </div>
          </form>
        )}

        <label
          className="side-sec graph-picker-label"
          htmlFor="workspace-graph-picker"
        >
          Your graphs
        </label>
        <select
          id="workspace-graph-picker"
          className="graph-picker"
          value={selectedGraphId}
          onChange={(event) => selectGraph(event.target.value)}
          aria-label="Select graph"
        >
          <option value="">Select a graph</option>
          {graphs.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>

        <div className="side-sec">At a glance</div>
        <div className="side-stats">
          <div>
            <span className="mono">{t.count}</span> documents
          </div>
          <div>
            <span className="mono">{t.concepts}</span> concepts
          </div>
          <div>
            <span className="mono">{t.relationships}</span> links
          </div>
        </div>

        <div className="side-foot">
          <div className="side-user">
            <div className="su-avatar">
              {(user?.name || "··").slice(0, 2).toUpperCase()}
            </div>
            <div>
              <div className="su-name">{user?.name || "Not signed in"}</div>
              <div className="su-mail">{user?.email || ""}</div>
            </div>
          </div>
          <button className="side-signout" onClick={signOut}>
            Sign out
          </button>
        </div>
      </aside>

      {/* ---------------- main ---------------- */}
      <main className="ws-main">
        <header className="ws-top">
          <div>
            <div className="ws-title">
              {selectedGraph?.name || "Choose a graph"}
            </div>
            <div className="ws-sub">
              {!selectedGraphId
                ? "Select or create a graph to continue"
                : graph.nodes.length
                  ? `${graph.nodes.length} concepts · ${graph.edges.length} links`
                  : "No sources yet"}
            </div>
          </div>
          <nav className="ws-tabs">
            {TABS.map((x) => (
              <button
                key={x}
                className={`ws-tab${tab === x ? " on" : ""}`}
                onClick={() => setTab(x)}
                disabled={
                  (x !== "graph" && !selectedGraphId) ||
                  (["chat", "notes", "quiz"].includes(x) &&
                    !viewingLatestVersion)
                }
              >
                {x[0].toUpperCase() + x.slice(1)}
              </button>
            ))}
          </nav>
        </header>

        {busy && <div className="ws-banner">{busy}</div>}
        {error && (
          <div className="ws-banner err">
            <span>{error}</span>
          </div>
        )}

        <section className="ws-body">
          {tab === "graph" && (
            <div
              className={`graph-canvas-wrap${graph.nodes.length ? "" : " is-empty"}`}
            >
              {graph.nodes.length === 0 ? (
                <div className="empty-state">
                  <h3>
                    {selectedGraphId ? "Nothing here yet" : "Choose a graph"}
                  </h3>
                  <p>
                    {selectedGraphId
                      ? "Upload a document to create the first version of this graph."
                      : "Select a saved graph or create one to view its documents and knowledge graph."}
                  </p>
                  {selectedGraphId && viewingLatestVersion && (
                    <button
                      className="btn-primary"
                      onClick={() => setTab("documents")}
                    >
                      Upload a document
                    </button>
                  )}
                </div>
              ) : (
                <ConceptGraph
                  nodes={graph.nodes}
                  edges={graph.edges}
                  versions={versions}
                  selectedVersionId={selectedVersionId}
                  onVersionChange={selectVersion}
                  loadingVersion={busy === "Loading version…"}
                  onSelectNode={setSelected}
                  selectedId={selected?.id}
                />
              )}
            </div>
          )}
          {tab === "graph" && graph.nodes.length > 0 && (
            <Inspector
              graphId={selectedGraphId}
              node={selected}
              nodes={graph.nodes}
              edges={graph.edges}
              maxWeek={evolutionMaxWeek}
            />
          )}

          {tab === "documents" && (
            <DocumentsTab
              docs={docs}
              totals={t}
              drag={drag}
              setDrag={setDrag}
              fileInput={fileInput}
              onFile={handleFile}
              onRemove={remove}
              busy={busy}
              readOnly={!viewingLatestVersion}
            />
          )}

          {tab === "chat" && (
            <ChatTab graphId={selectedGraphId} hasDocs={docs.length > 0} />
          )}
          {tab === "notes" && <NotesPane graphId={selectedGraphId} readyDocs={readyDocs} />}
          {tab === "quiz" && <QuizPane graphId={selectedGraphId} onToast={setBusyToast} />}
        </section>
      </main>
    </div>
  );
}

/* ====================== documents ====================== */

function DocumentsTab({
  docs,
  totals,
  drag,
  setDrag,
  fileInput,
  onFile,
  onRemove,
  busy,
  readOnly,
}) {
  return (
    <div className="pane pane-scroll">
      <div className="pane-head">
        <div>
          <h2>Documents</h2>
          <p>
            Everything this graph was built from. Removing a source takes its
            concepts out of the graph.
          </p>
        </div>
        <div className="stat-row">
          <div className="stat">
            <div className="n mono">{totals.count}</div>
            <div className="l">Documents</div>
          </div>
          <div className="stat">
            <div className="n mono">{totals.concepts}</div>
            <div className="l">Concepts</div>
          </div>
          <div className="stat">
            <div className="n mono">{totals.relationships}</div>
            <div className="l">Links</div>
          </div>
        </div>
      </div>

      <div
        className={`drop${drag ? " over" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          if (!readOnly) onFile(e.dataTransfer?.files?.[0]);
        }}
      >
        <h3>Add a document</h3>
        <p>Drop a PDF, DOCX, PPTX or TXT here, or choose a file.</p>
        <button
          className="btn-accent"
          disabled={Boolean(busy) || readOnly}
          onClick={() => fileInput.current?.click()}
        >
          {readOnly ? "Read-only version" : busy ? "Working…" : "Choose a file"}
        </button>
        <input
          ref={fileInput}
          type="file"
          accept=".pdf,.docx,.pptx,.txt,.md"
          disabled={readOnly}
          onChange={(e) => {
            onFile(e.target.files?.[0]);
            e.target.value = "";
          }}
        />
      </div>

      <table className="doc-table">
        <thead>
          <tr>
            <th>File</th>
            <th>Status</th>
            <th>Week</th>
            <th className="r">Concepts</th>
            <th className="r">Links</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {docs.length === 0 ? (
            <tr>
              <td colSpan={6}>
                <div className="empty">
                  No documents yet. Add one above and StudyGraph will start
                  reading it.
                </div>
              </td>
            </tr>
          ) : (
            docs.map((d) => (
              <tr key={d.id}>
                <td>
                  <div className="fname">
                    <span className="fext">
                      {extOf(d.name || d.filename || "")
                        .toUpperCase()
                        .slice(0, 4)}
                    </span>
                    {d.name || d.filename}
                  </div>
                  <div className="fmeta mono">
                    {fmtSize(d.size_kb)} · added {fmtWhen(d.uploaded_at)}
                  </div>
                </td>
                <td>
                  <span className={`pill ${statusClass(d.status)}`}>
                    <span className="pd" />
                    {statusLabel(d.status)}
                  </span>
                </td>
                <td className="mono">{d.week || "—"}</td>
                <td className="r mono">{d.nodes ?? d.concepts ?? 0}</td>
                <td className="r mono">{d.relationships ?? 0}</td>
                <td className="r">
                  <button
                    className="row-btn danger"
                    disabled={readOnly}
                    onClick={() => onRemove(d)}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

/* ====================== chat ====================== */

function ChatTab({ graphId, hasDocs }) {
  const [messages, setMessages] = useState([
    {
      who: "ai",
      text: "Ask me anything about the documents you have uploaded.",
    },
  ]);
  const [text, setText] = useState("");
  const [thinking, setThinking] = useState(false);
  const scroll = useRef(null);

  useEffect(() => {
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages, thinking]);

  async function send() {
    const q = text.trim();
    if (!q || thinking) return;
    setMessages((m) => [...m, { who: "me", text: q }]);
    setText("");
    setThinking(true);
    try {
      const r = await askGraphQuestion(graphId, q);
      setMessages((m) => [
        ...m,
        {
          who: "ai",
          text: r.answer || "No answer came back.",
          cite: r.citation_label,
        },
      ]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        {
          who: "ai",
          text:
            e.response?.status === 400
              ? "No documents have been processed yet. Add one in the Documents tab first."
              : `Could not reach the answer service: ${e.message}`,
        },
      ]);
    } finally {
      setThinking(false);
    }
  }

  return (
    <div className="pane pane-chat">
      <div className="chat-scroll" ref={scroll}>
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.who}`}>
            {m.text}
            {m.cite && <div className="msg-cite">Source: {m.cite}</div>}
          </div>
        ))}
        {thinking && <div className="msg ai">Searching your sources…</div>}
      </div>
      <div className="chat-bar">
        <input
          value={text}
          placeholder={
            hasDocs ? "Ask about your documents…" : "Upload a document first"
          }
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
        />
        <button className="btn-accent" onClick={send} disabled={thinking}>
          Ask
        </button>
      </div>
    </div>
  );
}
