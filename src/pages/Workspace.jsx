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
  listGraphs,
  createGraph as createSupabaseGraph,
  listGraphVersions,
  getGraphActiveSupport,
  fetchGraphVersion,
  listGraphDocuments,
  uploadGraphDocument,
  createGraphVersion,
  deleteGraphDocument,
} from '../services/api.js'

const TABS = ['graph', 'documents', 'notes', 'quiz']
const ALLOWED = ['pdf', 'docx', 'pptx', 'txt', 'md']
const extOf = (n) => (n.split('.').pop() || '').toLowerCase()

function statusClass(s) {
  const v = (s || '').toLowerCase()
  if (['done', 'ready', 'processed', 'complete', 'completed'].includes(v)) return 'ready'
  if (['running', 'processing', 'extracting', 'building graph', 'seeding'].includes(v)) return 'working'
  if (['error', 'failed'].includes(v)) return 'failed'
  return 'queued'
}
function statusLabel(s) {
  const v = (s || '').toLowerCase()
  if (['done', 'ready', 'processed', 'complete', 'completed'].includes(v)) return 'Ready'
  if (['error', 'failed'].includes(v)) return 'Failed'
  if (v === 'queued') return 'Queued'
  return s ? s[0].toUpperCase() + s.slice(1) : 'Queued'
}

const ACTIVE = ['queued', 'uploading', 'reading']

const fmtSize = (kb) => (kb == null ? '—' : kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb} KB`)
function fmtWhen(ts) {
  if (!ts) return '—'
  const m = Math.round((Date.now() - ts * 1000) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  return h < 24 ? `${h} hr ago` : `${Math.round(h / 24)} d ago`
}

function weekNumber(value) {
  const match = String(value || '').match(/\d+/)
  return match ? Number(match[0]) : 1
}

/* ---------- persisted snapshot -> what ConceptGraph draws ---------- */

/**
 * Turns a Supabase graph-version snapshot into nodes/edges.
 * Each node.sources entry is { name, week, weekLabel, documentId, chunkId?, deleted? }
 * so topic filtering can match on documentId rather than filename.
 */
function graphViewFromSnapshot(snapshot, activeSupport = null, documents = []) {
  const concepts = snapshot?.concepts || []
  const relationships = snapshot?.relationships || []
  const conceptProvenance = snapshot?.concept_provenance || []
  const documentsById = new Map(documents.map((d) => [String(d.id), d]))

  const nodes = concepts.map((concept) => {
    const provenanceSources = conceptProvenance
      .filter((row) => String(row.concept_id) === String(concept.id))
      .map((row) => {
        const week =
          Number(row.source_week) || weekNumber(documentsById.get(String(row.document_id))?.week)
        return {
          name: row.document_filename,
          week,
          weekLabel: `Week ${week}`,
          documentId: row.document_id,
          chunkId: row.chunk_id,
          deleted: !documentsById.has(String(row.document_id)),
        }
      })
    const fallbackSources = (activeSupport?.concepts?.[concept.normalized_name] || [])
      .map((documentId) => documentsById.get(String(documentId)))
      .filter(Boolean)
      .map((document) => ({
        name: document.filename,
        week: weekNumber(document.week),
        weekLabel: document.week || `Week ${weekNumber(document.week)}`,
        documentId: document.id,
      }))
    const sources = [
      ...new Map(
        [...provenanceSources, ...fallbackSources].map((s) => [String(s.documentId), s]),
      ).values(),
    ]
    const weeks = [...new Set(sources.map((s) => s.week))].sort((a, b) => a - b)

    return {
      id: String(concept.id),
      name: concept.name,
      description: '',
      status: concept.status || 'active',
      week: weeks[0] || 1,
      weeks: weeks.length ? weeks : [1],
      sources,
    }
  })

  const edges = relationships.map((r) => ({
    source: String(r.source_concept_id),
    target: String(r.target_concept_id),
    label: r.relationship_type || 'relates to',
    strength: 'medium',
  }))
  return { nodes, edges }
}

function graphDocumentRow(document) {
  return {
    ...document,
    name: document.filename,
    size_kb: Math.max(1, Math.round((Number(document.size_bytes) || 0) / 1024)),
    uploaded_at:
      typeof document.uploaded_at === 'string'
        ? Date.parse(document.uploaded_at) / 1000
        : document.uploaded_at,
    nodes: document.concepts ?? 0,
    relationships: document.relationships ?? 0,
  }
}

/**
 * Degree and "core" vs "support" for whatever set of nodes is on screen.
 * Recomputed per view, so a topic's graph ranks its concepts against each
 * other rather than against the whole graph.
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
  }
}

export default function Workspace() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const library = useLibrary(user?.id || user?.email)

  // --- UI state (teammate's shell) ---
  const [tab, setTab] = useState('graph')
  const [chatOpen, setChatOpen] = useState(false)
  const [sideOpen, setSideOpen] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadDest, setUploadDest] = useState(null)
  const [queue, setQueue] = useState([])
  const [dropOver, setDropOver] = useState(false)
  const [selected, setSelected] = useState(null)
  const [week, setWeek] = useState(1)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const setBusyToast = (msg) => { setBusy(msg); setTimeout(() => setBusy(''), 1800) }

  // --- persisted state (Supabase graphs / versions) ---
  const [graphs, setGraphs] = useState([])
  const [selectedGraphId, setSelectedGraphId] = useState('')
  const [versions, setVersions] = useState([])
  const [selectedVersionId, setSelectedVersionId] = useState('')
  const [creatingGraph, setCreatingGraph] = useState(false)
  const [graphName, setGraphName] = useState('')
  const [docs, setDocs] = useState([])
  const [graph, setGraph] = useState({ nodes: [], edges: [] })

  // The upload queue outlives a graph switch, so loops check this ref before
  // refreshing the screen for a graph the user has already left.
  const activeGraphRef = useRef('')
  useEffect(() => { activeGraphRef.current = selectedGraphId }, [selectedGraphId])

  // --- derived ------------------------------------------------------------

  const selectedGraph = graphs.find((g) => String(g.id) === String(selectedGraphId))
  const viewingLatestVersion = !versions.length || selectedVersionId === versions[0]?.id

  // Notes are written from documents the engine has finished reading.
  const readyDocs = useMemo(
    () => docs.filter((d) => statusClass(d.status) === 'ready'),
    [docs],
  )

  // Current view: the whole graph, or one topic inside it.
  const found = library.activeTopicId ? library.findTopic(library.activeTopicId) : null
  const activeTopicId = found ? found.topic.id : null
  const topicDocIds = found ? found.topic.docIds.map(String) : null

  const viewDocs = useMemo(
    () => (topicDocIds ? docs.filter((d) => topicDocIds.includes(String(d.id))) : docs),
    [docs, topicDocIds?.join('|')],   // eslint-disable-line react-hooks/exhaustive-deps
  )

  // A topic's graph is the concepts that cite at least one of the topic's
  // documents, plus the relationships between them. Same snapshot, filtered.
  const view = useMemo(() => {
    if (!topicDocIds) return shapeGraph(graph.nodes, graph.edges)
    const ids = new Set(viewDocs.map((d) => String(d.id)))
    const nodes = graph.nodes.filter((n) =>
      (n.sources || []).some((s) => ids.has(String(s.documentId))),
    )
    return shapeGraph(nodes, graph.edges)
  }, [graph, viewDocs, topicDocIds?.join('|')])   // eslint-disable-line react-hooks/exhaustive-deps

  const evolutionMaxWeek = Math.max(1, ...viewDocs.map((d) => weekNumber(d.week)))
  const maxWeek = Math.max(
    evolutionMaxWeek,
    ...view.nodes.flatMap((n) => n.weeks || [n.week || 1]),
  )
  const viewKey = `${selectedGraphId}:${selectedVersionId}:${activeTopicId || 'all'}`

  // Land on the newest week whenever the view or its range changes.
  useEffect(() => { setWeek(maxWeek) }, [viewKey, maxWeek])
  useEffect(() => { setSelected(null) }, [viewKey])

  // --- data: graphs, versions, snapshot -----------------------------------

  const loadGraphs = useCallback(async () => {
    try {
      const result = await listGraphs()
      setGraphs(result.graphs || [])
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }, [])

  const loadGraphWorkspace = useCallback(async (graphId, requestedVersionId = null) => {
    if (!graphId) return
    const [{ documents = [] }, { versions: graphVersions = [] }] = await Promise.all([
      listGraphDocuments(graphId),
      listGraphVersions(graphId),
    ])
    setDocs(documents.map(graphDocumentRow))
    setVersions(graphVersions)
    const version =
      graphVersions.find((item) => item.id === requestedVersionId) || graphVersions[0]
    setSelectedVersionId(version?.id || '')
    if (!version) {
      setGraph({ nodes: [], edges: [] })
      setSelected(null)
      return
    }
    const snapshot = await fetchGraphVersion(graphId, version.id)
    const activeSupport =
      graphVersions[0]?.id === version.id ? await getGraphActiveSupport(graphId) : null
    setGraph(graphViewFromSnapshot(snapshot, activeSupport, documents))
    setSelected(null)
  }, [])

  useEffect(() => { loadGraphs() }, [loadGraphs])

  // Keep the current graph across a refresh. The id is only a UI selection;
  // all graph data still comes from the persisted graph APIs.
  useEffect(() => {
    if (selectedGraphId || !graphs.length || !user?.id) return
    const stored = window.localStorage.getItem(`studygraph.activeGraphId:${user.id}`)
    const graphId = graphs.some((g) => String(g.id) === stored) ? stored : ''
    if (graphId) selectGraph(graphId)
  }, [graphs, selectedGraphId, user?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  async function selectGraph(graphId) {
    setSelectedGraphId(graphId)
    setSelectedVersionId('')
    setDocs([])
    setVersions([])
    setGraph({ nodes: [], edges: [] })
    setSelected(null)
    setError('')
    if (user?.id) {
      const key = `studygraph.activeGraphId:${user.id}`
      if (graphId) window.localStorage.setItem(key, String(graphId))
      else window.localStorage.removeItem(key)
    }
    if (!graphId) return
    setBusy('Loading graph…')
    try {
      await loadGraphWorkspace(graphId)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function createGraphFromForm(event) {
    event.preventDefault()
    const name = graphName.trim()
    if (!name) return
    setBusy('Creating graph…')
    try {
      const { graph: created } = await createSupabaseGraph(name)
      setGraphs((current) => [created, ...current])
      setGraphName('')
      setCreatingGraph(false)
      await selectGraph(created.id)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function selectVersion(versionId) {
    setSelectedVersionId(versionId)
    if (!selectedGraphId || !versionId) return
    setBusy('Loading version…')
    setTab('graph')
    try {
      await loadGraphWorkspace(selectedGraphId, versionId)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  function newGraph() {
    setCreatingGraph(true)
    setGraphName('')
    setError('')
  }

  // --- upload -------------------------------------------------------------
  //
  // Several files can be picked at once. They are processed one after another:
  // each is uploaded to the selected graph, then a new graph version is built
  // from it. The graph refreshes after every file, so it grows while the rest
  // are still waiting.

  const pending = useRef([])
  const draining = useRef(false)
  const patchItem = (key, patch) =>
    setQueue((q) => q.map((i) => (i.key === key ? { ...i, ...patch } : i)))

  function uploadBlockedReason() {
    if (!selectedGraphId) return 'Choose a graph before uploading documents.'
    if (!viewingLatestVersion) return 'Select the latest graph version before uploading documents.'
    return ''
  }

  async function processItem({ key, file, name, topicId, graphId }) {
    patchItem(key, { status: 'uploading', message: '' })
    try {
      const { document } = await uploadGraphDocument(graphId, file)
      // Assign the topic as soon as the document exists, so it is filed even
      // if building the version fails.
      if (topicId) library.assignDocs([document.id], topicId)
      patchItem(key, { status: 'reading', message: 'Building graph…' })
      await createGraphVersion(graphId, [document.id])
      patchItem(key, { status: 'done', message: '' })
    } catch (e) {
      patchItem(key, { status: 'failed', message: e.message })
      setError(`Could not process ${name}: ${e.message}`)
    }
    if (activeGraphRef.current === graphId) {
      await loadGraphWorkspace(graphId).catch(() => {})
    }
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

  /** Returns false (and sets an error) when the upload can't start. */
  function enqueue(files, topicId) {
    const blocked = uploadBlockedReason()
    if (blocked) { setError(blocked); return false }

    const stamp = Date.now()
    const items = files.map((file, i) => {
      const ext = extOf(file.name)
      const ok = ALLOWED.includes(ext)
      return {
        key: `${stamp}-${i}-${file.name}`,
        file,
        name: file.name,
        topicId: topicId || null,
        graphId: selectedGraphId,
        status: ok ? 'queued' : 'failed',
        message: ok ? '' : `.${ext} files aren't supported — use PDF, DOCX, PPTX, TXT or MD.`,
      }
    })
    setError('')
    setQueue((q) => [...q, ...items.map(({ file, ...rest }) => rest)])
    pending.current.push(...items.filter((i) => i.status === 'queued'))
    drain()
    return true
  }

  function openUpload() {
    const blocked = uploadBlockedReason()
    if (blocked) { setError(blocked); return }
    setUploadDest(activeTopicId)
    setUploadOpen(true)
  }

  const activeUploads = queue.filter((i) => ACTIVE.includes(i.status)).length

  async function remove(doc) {
    if (!selectedGraphId || !viewingLatestVersion) return
    try {
      await deleteGraphDocument(selectedGraphId, doc.id)
      library.assignDocs([doc.id], null)
      await loadGraphWorkspace(selectedGraphId)
    } catch (e) {
      setError(`Could not remove ${doc.name || doc.filename}: ${e.message}`)
    }
  }

  async function signOut() {
    await logout()
    navigate('/login', { replace: true })
  }

  const t = { count: docs.length, concepts: graph.nodes.length, relationships: graph.edges.length }

  // Header: a topic names itself (with its graph above). Otherwise the graph does.
  const title = found ? found.topic.name : selectedGraph?.name || 'Choose a graph'

  const fileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
  const canUpload = Boolean(selectedGraphId) && viewingLatestVersion

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
        // Graph management, for a sidebar that wants to host the graph list.
        graphItems={graphs}
        selectedGraphId={selectedGraphId}
        onSelectGraph={selectGraph}
        onNewGraph={newGraph}
        creatingGraph={creatingGraph}
        graphName={graphName}
        onGraphNameChange={setGraphName}
        onCreateGraph={createGraphFromForm}
        onCancelCreate={() => setCreatingGraph(false)}
        busy={busy}
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
                  <span>{selectedGraph?.name || 'Graph'}</span>
                </div>
              )}
              <div className="ws-title" title={title}>{title}</div>
              {selectedGraphId && view.nodes.length ? (
                <div className="ws-facts">
                  <span><b>{viewDocs.length}</b> document{viewDocs.length === 1 ? '' : 's'}</span>
                  <span><b>{view.nodes.length}</b> concepts</span>
                  <span><b>{view.edges.length}</b> relationships</span>
                  <span>Week <b>{week}</b> of {maxWeek}</span>
                </div>
              ) : (
                <div className="ws-sub">
                  {!selectedGraphId
                    ? 'Select or create a graph to continue'
                    : viewDocs.length
                      ? `${viewDocs.length} document${viewDocs.length === 1 ? '' : 's'} · no concepts yet`
                      : 'No sources yet'}
                </div>
              )}
            </div>
          </div>

          <div className="ws-top-right">
            {/* Graph switcher — lives here until the sidebar hosts the graph list. */}
            <div className="ws-graphpick">
              {creatingGraph ? (
                <form onSubmit={createGraphFromForm}>
                  <input
                    autoFocus
                    value={graphName}
                    onChange={(e) => setGraphName(e.target.value)}
                    placeholder="Graph name"
                    aria-label="Graph name"
                  />
                  <button type="submit" disabled={!graphName.trim() || Boolean(busy)}>Create</button>
                  <button type="button" onClick={() => setCreatingGraph(false)}>Cancel</button>
                </form>
              ) : (
                <>
                  <select
                    value={selectedGraphId}
                    onChange={(e) => selectGraph(e.target.value)}
                    aria-label="Graph"
                  >
                    <option value="">Choose a graph…</option>
                    {graphs.map((g) => (
                      <option key={g.id} value={String(g.id)}>{g.name}</option>
                    ))}
                  </select>
                  <button type="button" onClick={newGraph}>+ New graph</button>
                </>
              )}
            </div>

            <button type="button" className={`ws-upload${activeUploads ? ' is-busy' : ''}`}
                    onClick={openUpload} disabled={!canUpload}
                    title={!canUpload ? uploadBlockedReason() : undefined}>
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
                  disabled={
                    (x !== 'graph' && !selectedGraphId) ||
                    (['notes', 'quiz'].includes(x) && !viewingLatestVersion)
                  }
                >
                  {x[0].toUpperCase() + x.slice(1)}
                </button>
              ))}
              <button
                type="button"
                className={`ws-tab${chatOpen ? ' on' : ''}`}
                disabled={!selectedGraphId || !viewingLatestVersion}
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
                if (files.length && enqueue(files, activeTopicId)) {
                  setUploadDest(activeTopicId)
                  setUploadOpen(true)
                }
              }}
            >
              {view.nodes.length === 0 ? (
                <div className="empty-state">
                  {!selectedGraphId ? (
                    <>
                      <h3>Choose a graph</h3>
                      <p>Select a saved graph or create one to view its documents and knowledge graph.</p>
                      <button className="btn-primary" onClick={newGraph}>New graph</button>
                    </>
                  ) : (
                    <>
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
                      {viewingLatestVersion && (
                        <UploadDrop
                          tone="dark"
                          title={found ? `Add documents to ${found.topic.name}` : 'Drop your documents here'}
                          onFiles={(files) => enqueue(files, activeTopicId)}
                        />
                      )}
                      <UploadQueue items={queue} />
                    </>
                  )}
                </div>
              ) : (
                <ConceptGraph
                  key={viewKey}
                  nodes={view.nodes}
                  edges={view.edges}
                  versions={versions}
                  selectedVersionId={selectedVersionId}
                  onVersionChange={selectVersion}
                  loadingVersion={busy === 'Loading version…'}
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
              graphId={selectedGraphId}
              node={selected}
              nodes={view.nodes}
              edges={view.edges}
              maxWeek={evolutionMaxWeek}
              onClose={() => setSelected(null)}
            />
          )}

          {tab === 'documents' && (
            <DocumentsTab
              docs={viewDocs} totals={t} topic={found?.topic || null}
              library={library} queue={queue}
              onFiles={(files) => enqueue(files, activeTopicId)}
              onRemove={remove}
              readOnly={!viewingLatestVersion}
            />
          )}

          {tab === 'notes' && <NotesPane graphId={selectedGraphId} readyDocs={readyDocs} />}
          {tab === 'quiz' && <QuizPane graphId={selectedGraphId} onToast={setBusyToast} />}
        </section>
      </main>

      {chatOpen && selectedGraphId && (
        <ChatPanel
          graphId={selectedGraphId}
          onClose={() => setChatOpen(false)}
          onConceptClick={(conceptName) => {
            const node = view.nodes.find(
              (item) => item.name?.toLowerCase() === conceptName?.toLowerCase(),
            )
            if (node) {
              setSelected(node)
              setTab('graph')
            }
          }}
        />
      )}

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
  )
}

/* ====================== documents ====================== */

function DocumentsTab({ docs, totals, topic, library, queue, onFiles, onRemove, readOnly }) {
  return (
    <div className="pane pane-scroll">
      <div className="pane-head">
        <div>
          <h2>{topic ? `Documents in ${topic.name}` : 'All documents'}</h2>
          <p>{topic
            ? 'The sources this topic’s graph is built from. Use the Topic column to move a document somewhere else.'
            : 'Everything this graph was built from. Removing a source takes its concepts out of the graph.'}</p>
        </div>
        <div className="stat-row">
          <div className="stat"><div className="n mono">{topic ? docs.length : totals.count}</div><div className="l">Documents</div></div>
          <div className="stat"><div className="n mono">{topic ? docs.reduce((a, d) => a + (d.nodes ?? d.concepts ?? 0), 0) : totals.concepts}</div><div className="l">Concepts</div></div>
          <div className="stat"><div className="n mono">{topic ? docs.reduce((a, d) => a + (d.relationships ?? 0), 0) : totals.relationships}</div><div className="l">Links</div></div>
        </div>
      </div>

      {readOnly ? (
        <div className="empty">You're viewing an older version of this graph. Switch to the latest version to add or remove documents.</div>
      ) : (
        <UploadDrop
          title={topic ? `Add documents to ${topic.name}` : 'Add documents'}
          onFiles={onFiles}
        />
      )}
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
                <button className="row-btn danger" disabled={readOnly} onClick={() => onRemove(d)}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}