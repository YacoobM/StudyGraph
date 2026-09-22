import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth.jsx";
import {
  listDocuments,
  uploadDocument,
  deleteDocument,
  generateGraph,
  fetchGraph,
} from "../services/api";
import GraphViewer from "../components/GraphViewer.jsx";
import ChatPanel from "../components/ChatPanel.jsx";
import ModelPicker from "../components/ModelPicker.jsx";
import "./Builder.css";
import QuizPanel from "../components/QuizPanel.jsx";

/* ==========================================================================
 *  Builder — the "graph processing" workspace
 * ==========================================================================
 *  Handles: file upload UI, model selection, the documents table, and the
 *  Generate / Delete / Preview controls.
 *
 *  It talks to the backend ONLY through ../services/api (the stubbed layer).
 *  The actual graph render is delegated to <GraphViewer>, which
 *  the visualization teammate will replace.
 * ========================================================================== */

export default function Builder() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  const [modelSel, setModelSel] = useState({
    provider: "groq",
    model: "llama-3.1-8b-instant",
  });
  const [docs, setDocs] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [graphOpen, setGraphOpen] = useState(false);
  const [graphData, setGraphData] = useState({
    nodes: [],
    edges: [],
    legend: null,
  });
  const [chatOpen, setChatOpen] = useState(false);
  const [drag, setDrag] = useState(false);
  const [maxNodes, setMaxNodes] = useState("");
  const [minMentions, setMinMentions] = useState("2");
  const [processingMessage, setProcessingMessage] = useState("");
  const fileRef = useRef(null);

  const loadDocs = async () => {
    const data = await listDocuments();
    setDocs(data.documents || []);
  };

  useEffect(() => {
    loadDocs();
  }, []);

  // The backend keeps one global in-memory graph for the life of the
  // process — it doesn't disappear just because this component remounts
  // (e.g. coming back from viewing a concept's evolution used to navigate
  // to a separate page and back). Restore it here instead of leaving
  // graphData empty until the user regenerates from scratch.
  useEffect(() => {
    fetchGraph()
      .then((result) => {
        if (result?.nodes?.length) {
          const nextGraphData = {
            nodes: result.nodes,
            edges: result.edges || [],
            legend: result.legend || null,
          };
          setGraphData(nextGraphData);
          if (import.meta.env.DEV) {
            console.log("[StudyGraph] Loaded graph", nextGraphData);
          }
        }
      })
      .catch(() => {}); // no graph yet (fresh backend) — nothing to restore
  }, []);

  const onFiles = async (files) => {
    const list = Array.from(files || []);
    if (!list.length) return;
    setBusy(true);
    try {
      for (const file of list) {
        setProcessingMessage(`Processing ${file.name}…`);
        await uploadDocument(
          file,
          "llm",
          maxNodes ? Number(maxNodes) : null,
          (status) => {
            if (status.stage === "running") {
              setProcessingMessage(
                `Processing ${file.name} — extracting concepts and generating summaries…`,
              );
            }
          },
          minMentions ? Number(minMentions) : null,
        );
      }
      await loadDocs();
    } catch (e) {
      alert(e?.response?.data?.error || e?.message || "Upload failed");
    } finally {
      setBusy(false);
      setProcessingMessage("");
    }
  };

  const generate = async () => {
    if (!docs.length) return;
    setBusy(true);
    try {
      const ids = selected.size ? Array.from(selected) : docs.map((d) => d.id);
      const result = await generateGraph(
        ids,
        modelSel.provider,
        modelSel.model,
      );
      const nextGraphData = {
        nodes: result?.nodes || [],
        edges: result?.edges || [],
        legend: result?.legend || null,
      };
      setGraphData(nextGraphData);
      setGraphOpen(true);
      if (import.meta.env.DEV) {
        console.log("[StudyGraph] Generated graph", nextGraphData);
      }
      await loadDocs();
      // Keep users on the generation page so the embedded Q&A panel is immediately available.
    } catch (e) {
      alert(e?.response?.data?.error || "Graph generation failed");
    } finally {
      setBusy(false);
    }
  };

  const deleteSelected = async () => {
    if (!selected.size) return;
    if (!confirm(`Delete ${selected.size} file(s)?`)) return;
    for (const id of selected) {
      await deleteDocument(id);
    }
    setSelected(new Set());
    await loadDocs();
  };

  const toggleSelect = (id) => {
    const next = new Set(selected);
    next.has(id) ? next.delete(id) : next.add(id);
    setSelected(next);
  };

  const toggleAll = () => {
    if (selected.size === docs.length) setSelected(new Set());
    else setSelected(new Set(docs.map((d) => d.id)));
  };

  const handleLogout = () => {
    logout();
    navigate("/");
  };

  const dropHandlers = {
    onDragOver: (e) => {
      e.preventDefault();
      setDrag(true);
    },
    onDragLeave: () => setDrag(false),
    onDrop: (e) => {
      e.preventDefault();
      setDrag(false);
      onFiles(e.dataTransfer.files);
    },
  };

  return (
    <div className="builder">
      {/* ----- top bar ----- */}
      <header className="topbar">
        <div className="topbar-left">
          <button
            className="icon-btn"
            onClick={() => navigate("/")}
            title="Home"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div className="brand">
            <svg width="22" height="22" viewBox="0 0 64 64">
              <circle cx="32" cy="18" r="9" fill="#5dd4c4" />
              <circle cx="14" cy="46" r="7" fill="#f9a8d4" />
              <circle cx="50" cy="46" r="7" fill="#a3e635" />
              <line
                x1="32"
                y1="27"
                x2="14"
                y2="40"
                stroke="#5b6473"
                strokeWidth="2"
              />
              <line
                x1="32"
                y1="27"
                x2="50"
                y2="40"
                stroke="#5b6473"
                strokeWidth="2"
              />
              <line
                x1="21"
                y1="46"
                x2="43"
                y2="46"
                stroke="#5b6473"
                strokeWidth="2"
              />
            </svg>
            <span>StudyGraph</span>
          </div>
        </div>
        <div className="topbar-right">
          <button
            className="icon-btn"
            title="Ask StudyGraph"
            onClick={() =>
              document
                .querySelector(".rag-pane")
                ?.scrollIntoView({ behavior: "smooth", block: "nearest" })
            }
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
            </svg>
          </button>
          <div className="user-pill" onClick={handleLogout} title="Sign out">
            <div className="avatar">{(user?.name || "U")[0].toUpperCase()}</div>
            <div>
              <div className="user-name">{user?.name || "User"}</div>
              <div className="user-email">{user?.email}</div>
            </div>
          </div>
        </div>
      </header>

      {/* ----- main layout ----- */}
      <div className="builder-body">
        {/* sidebar */}
        <aside className="sidebar">
          <div
            {...dropHandlers}
            className={`dropzone ${drag ? "is-drag" : ""}`}
            onClick={() => fileRef.current?.click()}
          >
            <div className="dz-title">Drag &amp; Drop</div>
            <div className="dz-sub">
              or <span className="dz-link">browse</span>
            </div>
            <div className="dz-formats">
              Documents, Images,
              <br />
              Unstructured text
            </div>
            <div className="dz-info-icon">
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
              >
                <circle cx="12" cy="12" r="10" />
                <line x1="12" y1="16" x2="12" y2="12" />
                <line x1="12" y1="8" x2="12.01" y2="8" />
              </svg>
            </div>
            <input
              ref={fileRef}
              type="file"
              multiple
              accept=".pdf,.pptx,.docx,.txt,.md,.png,.jpg,.jpeg"
              onChange={(e) => onFiles(e.target.files)}
              style={{ display: "none" }}
            />
          </div>

          {processingMessage && (
            <div className="sidebar-section processing-status">
              {processingMessage}
            </div>
          )}

          {/* Model picker — choose which LLM extracts the graph */}
          <div className="sidebar-section">
            <div className="sidebar-label">Extraction Model</div>
            <ModelPicker value={modelSel} onChange={setModelSel} />
            <div className="sidebar-hint">
              Model used for entity + relationship extraction.
            </div>
          </div>

          {/* Temporary lever for LLM/rate-limit load: caps how many distinct
              concepts each uploaded document can contribute, which caps how
              many evolution summaries get generated during upload. */}
          <div className="sidebar-section">
            <div className="sidebar-label">Max Nodes / Document</div>
            <input
              type="number"
              min="1"
              placeholder="Unlimited"
              value={maxNodes}
              onChange={(e) => setMaxNodes(e.target.value)}
              className="max-nodes-input"
            />
            <div className="sidebar-hint">
              Caps concepts extracted per document. Lower this if you're hitting
              API rate limits.
            </div>
          </div>

          {/* "Key concept" threshold: drops concepts mentioned fewer than
              this many times, unless they're a curated seed concept. Cuts
              noisy/low-value nodes AND the LLM calls they'd otherwise cost
              for evolution summaries and comparisons. */}
          <div className="sidebar-section">
            <div className="sidebar-label">Min Mentions to Keep a Concept</div>
            <input
              type="number"
              min="1"
              placeholder="2"
              value={minMentions}
              onChange={(e) => setMinMentions(e.target.value)}
              className="max-nodes-input"
            />
            <div className="sidebar-hint">
              Drops concepts below this mention count. Raise it for a cleaner
              graph and fewer API calls; set to 1 to keep everything.
            </div>
          </div>
        </aside>

        {/* main content */}
        <main className="main-pane">
          <div className="connection-header">
            <div>
              <div className="conn-title">StudyGraph connection</div>
              <div className="conn-row">
                <span className="conn-dot" />
                <span className="conn-uri">studygraph://localhost / demo</span>
              </div>
              <div className="conn-row no-schema">
                <span className="diamond">◆</span>
                <span>Frontend running in standalone (mock) mode</span>
              </div>
            </div>
            <div className="conn-actions">
              <button className="btn btn-ghost">Graph Settings</button>
              <button className="btn btn-primary" onClick={handleLogout}>
                Disconnect
              </button>
            </div>
          </div>

          <div className="workspace-grid">
            <section className="docs-table-wrap">
              <table className="docs-table">
                <thead>
                  <tr>
                    <th style={{ width: 40 }}>
                      <input
                        type="checkbox"
                        checked={
                          docs.length > 0 && selected.size === docs.length
                        }
                        onChange={toggleAll}
                      />
                    </th>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Upload Status</th>
                    <th>Size (KB)</th>
                    <th>Source</th>
                    <th>Type</th>
                    <th>Model</th>
                    <th>Nodes</th>
                    <th>Relationships</th>
                  </tr>
                </thead>
                <tbody>
                  {docs.length === 0 ? (
                    <tr>
                      <td colSpan="10" className="empty-row">
                        No files yet — drag a document into the panel on the
                        left to get started.
                      </td>
                    </tr>
                  ) : (
                    docs.map((d) => (
                      <tr key={d.id}>
                        <td>
                          <input
                            type="checkbox"
                            checked={selected.has(d.id)}
                            onChange={() => toggleSelect(d.id)}
                          />
                        </td>
                        <td className="cell-name" title={d.name}>
                          {d.name}
                        </td>
                        <td>
                          <span
                            className={`status status-${(d.status || "new").toLowerCase()}`}
                          >
                            {statusDot(d.status)} {d.status || "New"}
                          </span>
                        </td>
                        <td>
                          <span className="upload-ok">
                            <span className="dot dot-success" />{" "}
                            {d.upload_status}
                          </span>
                        </td>
                        <td>{d.size_kb}</td>
                        <td>{d.source}</td>
                        <td>{d.type}</td>
                        <td className="cell-italic">{d.model}</td>
                        <td>{d.nodes}</td>
                        <td>{d.relationships}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>

              <div className="table-footer">
                <span className="ft-info">
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                  >
                    <circle cx="12" cy="12" r="10" />
                    <line x1="12" y1="16" x2="12" y2="12" />
                    <line x1="12" y1="8" x2="12.01" y2="8" />
                  </svg>
                  Running in standalone mode — uploads &amp; graphs are mocked
                  until the backend module is connected.
                </span>
              </div>
            </section>

            <aside className="rag-pane">
              <ChatPanel embedded />
            </aside>

            <aside className="quiz-pane">
              <QuizPanel embedded />
            </aside>
          </div>

          <div className="bottom-bar">
            <div className="bottom-status">
              <span className="dot dot-success" />
              Using <strong>{modelSel.provider}</strong> /{" "}
              <code>{modelSel.model}</code>
            </div>
            <div className="bottom-actions">
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={generate}
              >
                {busy ? "Working…" : "Generate Graph"}
              </button>
              <button
                className="btn btn-danger"
                disabled={!selected.size}
                onClick={deleteSelected}
              >
                Delete Files
              </button>
              <button
                className="btn btn-ghost"
                onClick={() => setGraphOpen(true)}
              >
                Preview Graph <span style={{ marginLeft: 6 }}>↑</span>
              </button>
            </div>
          </div>
        </main>
      </div>

      {/* graph viewer — INTEGRATION POINT #2 (visualization teammate) */}
      {graphOpen && (
        <GraphViewer
          nodes={graphData.nodes}
          edges={graphData.edges}
          legend={graphData.legend}
          onClose={() => setGraphOpen(false)}
        />
      )}

      {/* Q&A RAG panel now lives directly on the generation page. */}
    </div>
  );
}

function statusDot(status) {
  if (status === "Completed") return <span className="dot dot-success" />;
  if (status === "Processing") return <span className="dot dot-warn" />;
  if (status === "Failed") return <span className="dot dot-danger" />;
  return <span className="dot dot-warn" />;
}