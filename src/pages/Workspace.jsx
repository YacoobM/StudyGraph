import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import ConceptGraph from '../components/ConceptGraph.jsx'
import QuizPane from '../components/QuizPane.jsx'
import Inspector from '../components/Inspector.jsx'
import NotesPane from '../components/NotesPane.jsx'
import ChatPanel from '../components/ChatPanel.jsx'
import SubjectSidebar from '../components/SubjectSidebar.jsx'
import UploadDialog, { UploadDrop, UploadQueue, TopicSelect } from '../components/UploadPanel.jsx'
import { useLibrary } from '../lib/library.js'

import '../styles/studygraph.css'
import './Workspace.css'

import {
  listDocuments,
  uploadDocument,
  deleteDocument,
  fetchGraph,
  getPipelineStatus,
  patchDocument,
  fetchDocumentStats,
  retryFailedUploadBatches,
} from "../services/api.js";

const TABS = ["graph", "documents", "notes", "quiz"];
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
/**
 * Display-only tidy-up of a filename: FIT3143-Topic-2-2024.pdf reads as
 * "FIT3143 Topic 2 2024" in the header. The stored filename and the backend's
 * document identifier are untouched — this never leaves the header.
 */
function prettyName(filename) {
  if (!filename) return "";
  return (
    String(filename)
      .replace(/\.[A-Za-z0-9]{1,5}$/, "")
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim() || String(filename)
  );
}

/**
 * Degree, "core" vs "support" and the week range for whatever set of nodes is
 * on screen. Recomputed per view, so a topic's graph ranks its concepts
 * against each other rather than against the whole library.
 */
function shapeGraph(nodes, edges) {
  const ids = new Set(nodes.map((n) => n.id))
  const kept = edges.filter((e) => ids.has(e.source) && ids.has(e.target))
  const degree = {}
  kept.forEach((e) => {
    degree[e.source] = (degree[e.source] || 0) + 1
    degree[e.target] = (degree[e.target] || 0) + 1
  })
  const withDeg = nodes.map((n) => ({ ...n, degree: degree[n.id] || 0 }))
  const ranked = withDeg.map((n) => n.degree).sort((a, b) => b - a)
  const cut = Math.max(1, ranked[Math.floor(ranked.length / 3)] ?? 1)
  return {
    nodes: withDeg.map((n) => ({ ...n, kind: n.degree >= cut ? 'core' : 'support' })),
    edges: kept,
    maxWeek: Math.max(1, ...withDeg.map((n) => n.week || 1)),
  }
}

const ACTIVE = ['queued', 'uploading', 'reading']
const cap = (x) => (x ? x[0].toUpperCase() + x.slice(1) : x)

const fmtSize = (kb) => (kb == null ? '—' : kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb} KB`)
function fmtWhen(ts) {
  if (!ts) return "—";
  const m = Math.round((Date.now() - ts * 1000) / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 24 ? `${h} hr ago` : `${Math.round(h / 24)} d ago`;
}

export default function Workspace() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const library = useLibrary(user?.id || user?.email)

  const [tab, setTab] = useState('graph')
  const [chatOpen, setChatOpen] = useState(false)
  const [docs, setDocs] = useState([])
  const [totals, setTotals] = useState(null)
  const [graph, setGraph] = useState({ nodes: [], edges: [] })
  const [selected, setSelected] = useState(null)
  const [week, setWeek] = useState(1)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [canRetry, setCanRetry] = useState(false)
  const [sideOpen, setSideOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadDest, setUploadDest] = useState(null)
  const [queue, setQueue] = useState([])
  const [dropOver, setDropOver] = useState(false)
  const setBusyToast = (msg) => { setBusy(msg); setTimeout(() => setBusy(''), 1800) }

  // Notes are written from documents the engine has finished reading, so the
  // picker there only offers rows that have settled.
  const readyDocs = useMemo(
    () => docs.filter((d) => statusClass(d.status) === "ready"),
    [docs],
  );

  // --- current view: All documents, or one topic --------------------------

  const found = library.activeTopicId ? library.findTopic(library.activeTopicId) : null
  const activeTopicId = found ? found.topic.id : null
  const topicDocIds = found ? found.topic.docIds.map(String) : null

  const viewDocs = useMemo(
    () => (topicDocIds ? docs.filter((d) => topicDocIds.includes(String(d.id))) : docs),
    [docs, topicDocIds?.join('|')],   // eslint-disable-line react-hooks/exhaustive-deps
  )

  // A topic's graph is the concepts whose source files belong to the topic,
  // and the relationships between them. The data is the same /api/graph
  // response — it is only filtered here.
  const view = useMemo(() => {
    if (!topicDocIds) return shapeGraph(graph.nodes, graph.edges)
    const files = new Set(viewDocs.map((d) => d.name || d.filename))
    const nodes = graph.nodes.filter((n) => (n.sources || []).some((f) => files.has(f)))
    return shapeGraph(nodes, graph.edges)
  }, [graph, viewDocs, topicDocIds?.join('|')])   // eslint-disable-line react-hooks/exhaustive-deps

  const maxWeek = view.maxWeek
  const viewKey = activeTopicId || 'all'

  // Land on the newest week whenever the view or its range changes.
  useEffect(() => { setWeek(view.maxWeek) }, [viewKey, view.maxWeek])
  useEffect(() => { setSelected(null) }, [viewKey])

  // --- data ---------------------------------------------------------------

  /**
   * Loads the document list, then reconciles it against the engine.
   *
   * The upload poll runs in the browser, so refreshing the page while a
   * document is being read leaves its row on "Running" forever even though
   * the backend finished. Any row still running or queued is checked against
   * /api/documents/stats and corrected.
   */
  const loadDocs = useCallback(async () => {
    try {
      const data = await listDocuments();
      const rows = data.documents || [];
      setDocs(rows);
      setTotals(data.totals || null);
      setError("");

      const unsettled = rows.filter((d) =>
        ["running", "queued", "processing"].includes(
          (d.status || "").toLowerCase(),
        ),
      );
      if (unsettled.length === 0) return rows;

      const { stats } = await fetchDocumentStats();
      const fixes = unsettled.filter((d) => stats[d.name || d.filename]);
      if (fixes.length === 0) return rows;

      await Promise.all(
        fixes.map((d) => {
          const st = stats[d.name || d.filename];
          return patchDocument(d.id, {
            status: "done",
            concepts: st.concepts,
            relationships: st.edges,
            ...(st.week ? { week: st.week } : {}),
          }).catch(() => null);
        }),
      );

      const fresh = await listDocuments();
      setDocs(fresh.documents || []);
      setTotals(fresh.totals || null);
      return fresh.documents || [];
    } catch (e) {
      setError(e.message);
    }
  }, []);

  /**
   * Maps main.py's /api/graph into what ConceptGraph draws.
   *
   * The real contract, which differs from the dev stub's older shape:
   *   node.name            the readable label (node.id is snake_case)
   *   node.weeks           the weeks this concept appears in
   *   node.source          [{file, page...}], not a list of strings
   *   edge.source/.target  snake_case node ids, NOT edge.from/.to
   * Degree and core/support are worked out per view in shapeGraph().
   */
  const loadGraph = useCallback(async () => {
    try {
      const g = await fetchGraph()
      const rawNodes = g.nodes || []
      const rawEdges = g.edges || []
      if (!rawNodes.length) { setGraph({ nodes: [], edges: [] }); return { nodes: [], edges: [] } }

      const weekNo = (w) => {
        const n = parseInt(String(w).replace(/\D+/g, ''), 10)
        return Number.isFinite(n) ? n : 1
      }

      const nodes = rawNodes.map((n) => {
        const weeks = (n.weeks || []).map(weekNo)
        return {
          id: String(n.id),
          name: n.name || String(n.id).replace(/_/g, " "),
          week: weeks.length ? Math.min(...weeks) : 1,
          weeks,
          description: n.description || '',
          sources: (n.source || []).map((x) => (typeof x === 'string' ? x : x.file)),
        }
      })

      const ids = new Set(nodes.map((n) => n.id))
      const edges = rawEdges
        .filter((e) => ids.has(String(e.source)) && ids.has(String(e.target)))
        .map((e) => ({
          source: String(e.source),
          target: String(e.target),
          label: e.label || e.relation || "relates to",
          strength: e.is_inferred ? "low" : "medium",
        }));

      setGraph({ nodes, edges })
      return { nodes, edges }
    } catch {
      /* leave what is on screen; the Documents tab reports real errors */
    }
  }, []);

  /**
   * Repair rows the browser lost track of.
   *
   * Extraction status is polled from the page, so reloading the tab mid-run
   * leaves the row stuck on "Running" forever even though the backend
   * finished. On load, if the pipeline is idle or done but rows still say
   * running, mark them from the graph instead.
   *
   * Per-document counts come from node.source — the pipeline's own totals are
   * cumulative across every upload, so attributing them to one file would
   * overstate it.
   */
  const reconcile = useCallback(async (docs, gNodes, gEdges) => {
    const pending = docs.filter((d) =>
      [
        "running",
        "queued",
        "processing",
        "extracting",
        "seeding",
        "building graph",
      ].includes(String(d.status || "").toLowerCase()),
    );
    if (!pending.length || !gNodes.length) return false;

    let stage = "idle";
    try {
      stage = (await getPipelineStatus()).stage || "idle";
    } catch {
      return false;
    }
    if (stage === "running") return false; // genuinely still working

    let changed = false;
    for (const d of pending) {
      const file = d.name || d.filename;
      const mine = gNodes.filter((n) => (n.sources || []).includes(file));
      if (!mine.length) continue; // nothing of this file in the graph yet
      const ids = new Set(mine.map((n) => n.id));
      const links = gEdges.filter(
        (e) => ids.has(e.source) && ids.has(e.target),
      ).length;
      await patchDocument(d.id, {
        status: "done",
        concepts: mine.length,
        relationships: links,
      }).catch(() => {});
      changed = true;
    }
    return changed;
  }, []);

  useEffect(() => {
    (async () => {
      const [docsData, g] = await Promise.all([loadDocs(), loadGraph()]);
      if (docsData && g && (await reconcile(docsData, g.nodes, g.edges)))
        loadDocs();
    })();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // --- upload -------------------------------------------------------------
  //
  // Several files can be picked at once. They are uploaded one after another
  // through the existing uploadDocument() call, because the backend runs a
  // single extraction pipeline and reports one global status — two uploads in
  // flight would read each other's progress. The graph refreshes after each
  // file, so it grows while the rest are still waiting.

  const pending = useRef([])
  const draining = useRef(false)
  const patchItem = (key, patch) =>
    setQueue((q) => q.map((i) => (i.key === key ? { ...i, ...patch } : i)))

  async function processItem({ key, file, name, topicId }) {
    patchItem(key, { status: 'uploading', message: '' })
    let docId = null
    try {
      const result = await uploadDocument(file, 'llm', null, (st) => {
        if (st.stage === 'running') {
          patchItem(key, { status: 'reading', message: cap(st.operation) || 'Reading…' })
        }
      }, null)
      docId = result?.document?.id ?? null
      if (result?.raw?.stage === 'partially_processed') {
        patchItem(key, { status: 'partial', message: 'Some parts could not be read.' })
        setError(`Some batches for ${name} could not be processed.`)
        setCanRetry(true)
      } else {
        const c = result?.document?.nodes ?? result?.document?.concepts
        patchItem(key, { status: 'done', message: c != null ? `${c} concepts` : '' })
      }
    } catch (e) {
      patchItem(key, { status: 'failed', message: e.message })
      setError(`Could not process ${name}: ${e.message}`)
    }

    const rows = await loadDocs()
    // The row is created before extraction starts, so a failed file still has
    // one; the newest row with this filename is it (the list is newest first).
    if (docId == null && rows) docId = rows.find((d) => (d.name || d.filename) === name)?.id ?? null
    if (docId != null && topicId) library.assignDocs([docId], topicId)
    await loadGraph()
  }

  async function drain() {
    if (draining.current) return
    draining.current = true
    try {
      while (pending.current.length) await processItem(pending.current.shift())
    } finally {
      draining.current = false
    }
  }

  function enqueue(files, topicId) {
    const stamp = Date.now()
    const items = files.map((file, i) => {
      const ext = extOf(file.name)
      const ok = ALLOWED.includes(ext)
      return {
        key: `${stamp}-${i}-${file.name}`,
        file,
        name: file.name,
        topicId: topicId || null,
        status: ok ? 'queued' : 'failed',
        message: ok ? '' : `.${ext} files aren't supported — use PDF, DOCX, PPTX, TXT or MD.`,
      }
    })
    setError('')
    setCanRetry(false)
    setQueue((q) => [...q, ...items.map(({ file, ...rest }) => rest)])
    pending.current.push(...items.filter((i) => i.status === 'queued'))
    drain()
  }

  function openUpload() {
    setUploadDest(activeTopicId)
    setUploadOpen(true)
  }

  const activeUploads = queue.filter((i) => ACTIVE.includes(i.status)).length

  async function retryFailed() {
    setCanRetry(false);
    setError("");
    setBusy("Retrying failed extraction batches…");
    try {
      const status = await retryFailedUploadBatches();
      if (status.stage === "partially_processed") {
        setError("Some extraction batches still could not be processed.");
        setCanRetry(true);
      }
      await loadDocs();
      await loadGraph();
    } catch (e) {
      setError(`Could not retry failed batches: ${e.message}`);
      setCanRetry(true);
    } finally {
      setBusy("");
    }
  }

  async function remove(doc) {
    try {
      await deleteDocument(doc.id)
      library.assignDocs([doc.id], null)
      await loadDocs()
      await loadGraph()
    } catch (e) {
      setError(`Could not remove ${doc.name || doc.filename}: ${e.message}`);
    }
  }

  async function signOut() {
    await logout();
    navigate("/login", { replace: true });
  }

  const t = totals || { count: docs.length, concepts: 0, relationships: 0 };

  // Header: a topic names itself (with its subject above). "All documents"
  // keeps the old rule — one document names the graph, several are counted.
  const rawTitle = !found && docs.length === 1 ? (docs[0].name || docs[0].filename || '') : ''
  const title = found
    ? found.topic.name
    : docs.length === 0
      ? 'Untitled graph'
      : docs.length === 1
        ? prettyName(rawTitle)
        : 'All documents'

  const fileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')

  return (
    <div className="ws">
      <SubjectSidebar
        library={library}
        docs={docs}
        open={sideOpen}
        onClose={() => setSideOpen(false)}
        onHome={() => navigate('/')}
        user={user}
        onSignOut={signOut}
      />

      {/* ---------------- main ---------------- */}
      <main className="ws-main">
        <header className="ws-top">
          <div className="ws-top-left">
            <button type="button" className="ws-menu" aria-label="Show subjects and topics"
                    onClick={() => setSideOpen(true)}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
                <path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h16" />
              </svg>
            </button>
            <div className="ws-head">
              {found && (
                <div className="ws-crumb">
                  <span title={found.subject.name}>{found.subject.name}</span>
                  <span aria-hidden="true">›</span>
                  <span>Graph</span>
                </div>
              )}
              <div className="ws-title" title={rawTitle || title}>{title}</div>
              {view.nodes.length ? (
                <div className="ws-facts">
                  <span><b>{viewDocs.length}</b> document{viewDocs.length === 1 ? '' : 's'}</span>
                  <span><b>{view.nodes.length}</b> concepts</span>
                  <span><b>{view.edges.length}</b> relationships</span>
                  <span>Week <b>{week}</b> of {maxWeek}</span>
                </div>
              ) : (
                <div className="ws-sub">
                  {viewDocs.length
                    ? `${viewDocs.length} document${viewDocs.length === 1 ? '' : 's'} · no concepts yet`
                    : 'No sources yet'}
                </div>
              )}
            </div>
          </div>

          <div className="ws-top-right">
            <button type="button" className={`ws-upload${activeUploads ? ' is-busy' : ''}`}
                    onClick={openUpload}>
              {activeUploads ? (
                <><span className="ws-upload-dot" aria-hidden="true" />
                  <span className="ws-upload-long">Uploading · {activeUploads} left</span>
                  <span className="ws-upload-short">{activeUploads} left</span></>
              ) : (
                <><span aria-hidden="true">+</span>
                  <span className="ws-upload-long">Add documents</span>
                  <span className="ws-upload-short">Add</span></>
              )}
            </button>
            <nav className="ws-tabs">
              {TABS.map((x) => (
                <button
                  key={x}
                  className={`ws-tab${tab === x ? ' on' : ''}`}
                  onClick={() => setTab(x)}
                >
                  {x[0].toUpperCase() + x.slice(1)}
                </button>
              ))}
            <button
              type="button"
              className={`ws-tab${chatOpen ? ' on' : ''}`}
              onClick={() => setChatOpen((open) => !open)}
            >
              Ask StudyGraph
            </button>
            </nav>
          </div>
        </header>

        {busy && <div className="ws-banner">{busy}</div>}
        {error && (
          <div className="ws-banner err">
            <span>{error}</span>
            {canRetry && (
              <button onClick={retryFailed}>Retry failed batches</button>
            )}
          </div>
        )}

        <section className="ws-body">
          {tab === 'graph' && (
            <div
              className={`graph-canvas-wrap${view.nodes.length ? '' : ' is-empty'}${dropOver ? ' drop-over' : ''}`}
              onDragOver={(e) => { if (fileDrag(e)) { e.preventDefault(); setDropOver(true) } }}
              onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDropOver(false) }}
              onDrop={(e) => {
                if (!fileDrag(e)) return
                e.preventDefault()
                setDropOver(false)
                const files = Array.from(e.dataTransfer.files || [])
                if (files.length) { enqueue(files, activeTopicId); setUploadDest(activeTopicId); setUploadOpen(true) }
              }}
            >
              {view.nodes.length === 0 ? (
                <div className="empty-state">
                  {viewDocs.length ? (
                    <>
                      <h3>Still reading your documents</h3>
                      <p>Concepts appear here as soon as each file has been read.</p>
                    </>
                  ) : (
                    <>
                      <h3>{found ? `Nothing in ${found.topic.name} yet` : 'Nothing here yet'}</h3>
                      <p>Add your lecture notes or slides and StudyGraph will build the
                         graph right here.</p>
                    </>
                  )}
                  <UploadDrop
                    tone="dark"
                    title={found ? `Add documents to ${found.topic.name}` : 'Drop your documents here'}
                    onFiles={(files) => enqueue(files, activeTopicId)}
                  />
                  <UploadQueue items={queue} />
                </div>
              ) : (
                <ConceptGraph
                  key={viewKey}
                  nodes={view.nodes}
                  edges={view.edges}
                  week={week}
                  maxWeek={maxWeek}
                  onWeekChange={setWeek}
                  onSelectNode={setSelected}
                  selectedId={selected?.id}
                />
              )}
              {dropOver && view.nodes.length > 0 && (
                <div className="graph-drop-hint" aria-hidden="true">
                  Drop to add to {found ? found.topic.name : 'All documents'}
                </div>
              )}
            </div>
          )}
          {tab === 'graph' && view.nodes.length > 0 && selected && (
            <Inspector
              node={selected}
              nodes={view.nodes}
              edges={view.edges}
              maxWeek={maxWeek}
              onClose={() => setSelected(null)}
            />
          )}

          {tab === "documents" && (
            <DocumentsTab
              docs={viewDocs} totals={t} topic={found?.topic || null}
              library={library} queue={queue}
              onFiles={(files) => enqueue(files, activeTopicId)}
              onRemove={remove}
            />
          )}

          {tab === "notes" && <NotesPane readyDocs={readyDocs} />}
          {tab === "quiz" && <QuizPane onToast={setBusyToast} />}
        </section>
      </main>
      {chatOpen && <ChatPanel onClose={() => setChatOpen(false)} />}

      <UploadDialog
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        subjects={library.subjects}
        destination={uploadDest}
        onDestination={setUploadDest}
        onFiles={(files) => enqueue(files, uploadDest)}
        items={queue}
        onClearFinished={() => setQueue((q) => q.filter((i) => ACTIVE.includes(i.status)))}
      />
    </div>
  );
}

/* ====================== documents ====================== */

function DocumentsTab({ docs, totals, topic, library, queue, onFiles, onRemove }) {
  return (
    <div className="pane pane-scroll">
      <div className="pane-head">
        <div>
          <h2>{topic ? `Documents in ${topic.name}` : 'All documents'}</h2>
          <p>{topic
            ? 'The sources this topic’s graph is built from. Use the Topic column to move a document somewhere else.'
            : 'Everything you have uploaded. Removing a source takes its concepts out of the graph.'}</p>
        </div>
        <div className="stat-row">
          <div className="stat"><div className="n mono">{topic ? docs.length : totals.count}</div><div className="l">Documents</div></div>
          <div className="stat"><div className="n mono">{topic ? docs.reduce((a, d) => a + (d.nodes ?? d.concepts ?? 0), 0) : totals.concepts}</div><div className="l">Concepts</div></div>
          <div className="stat"><div className="n mono">{topic ? docs.reduce((a, d) => a + (d.relationships ?? 0), 0) : totals.relationships}</div><div className="l">Links</div></div>
        </div>
      </div>

      <UploadDrop
        title={topic ? `Add documents to ${topic.name}` : 'Add documents'}
        onFiles={onFiles}
      />
      <UploadQueue items={queue} />

      <table className="doc-table">
        <thead>
          <tr>
            <th>File</th><th>Status</th><th>Topic</th><th>Week</th>
            <th className="r">Concepts</th><th className="r">Links</th><th />
          </tr>
        </thead>
        <tbody>
          {docs.length === 0 ? (
            <tr><td colSpan={7}><div className="empty">
              {topic
                ? 'No documents in this topic yet. Add some above, or move one here from All documents.'
                : 'No documents yet. Add some above and StudyGraph will start reading them.'}
            </div></td></tr>
          ) : docs.map((d) => (
            <tr key={d.id}>
              <td>
                <div className="fname">
                  <span className="fext">{extOf(d.name || d.filename || '').toUpperCase().slice(0, 4)}</span>
                  {d.name || d.filename}
                </div>
                <div className="fmeta mono">{fmtSize(d.size_kb)} · added {fmtWhen(d.uploaded_at)}</div>
              </td>
              <td><span className={`pill ${statusClass(d.status)}`}>
                <span className="pd" />{statusLabel(d.status)}</span></td>
              <td>
                <TopicSelect
                  id={`doc-topic-${d.id}`}
                  label={<span className="sr-only">Topic for {d.name || d.filename}</span>}
                  subjects={library.subjects}
                  value={library.topicOfDoc(d.id)}
                  onChange={(topicId) => library.assignDocs([d.id], topicId)}
                />
              </td>
              <td className="mono">{d.week || '—'}</td>
              <td className="r mono">{d.nodes ?? d.concepts ?? 0}</td>
              <td className="r mono">{d.relationships ?? 0}</td>
              <td className="r">
                <button className="row-btn danger" onClick={() => onRemove(d)}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
