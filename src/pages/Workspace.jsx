import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import ConceptGraph from '../components/ConceptGraph.jsx'
import QuizPane from '../components/QuizPane.jsx'
import Inspector from '../components/Inspector.jsx'
import NotesPane from '../components/NotesPane.jsx'
import ChatPanel from '../components/ChatPanel.jsx'
import SubjectSidebar from '../components/SubjectSidebar.jsx'
import UploadDialog, { UploadDrop, UploadQueue } from '../components/UploadPanel.jsx'
import { useLibrary } from '../lib/library.js'

import '../styles/studygraph.css'
import './Workspace.css'

import {
  createGraph as createSupabaseGraph,
  listGraphVersions,
  getGraphActiveSupport,
  fetchGraphVersion,
  listGraphDocuments,
  uploadGraphDocument,
  createGraphVersion,
  deleteGraphDocument,
  refreshDegradedConcepts,
} from '../services/api.js'

const TABS = ['graph', 'documents', 'notes', 'quiz']
const ALLOWED = ['pdf', 'docx', 'pptx', 'txt', 'md']
const extOf = (n) => (n.split('.').pop() || '').toLowerCase()

function statusClass(s) {
  const v = (s || '').toLowerCase()
  if (['done', 'ready', 'processed', 'chunked', 'complete', 'completed'].includes(v)) return 'ready'
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
      description: concept.description || '',
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

  // --- UI state ---
  const [tab, setTab] = useState('graph')
  const [chatOpen, setChatOpen] = useState(false)
  const [chatWidth, setChatWidth] = useState(380)
  const [sideOpen, setSideOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [uploadDest, setUploadDest] = useState(null)
  const [queue, setQueue] = useState([])
  const [dropOver, setDropOver] = useState(false)
  const [selected, setSelected] = useState(null)
  const [week, setWeek] = useState(1)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [toast, setToast] = useState(null)
  const [refreshingDegraded, setRefreshingDegraded] = useState(false)
  const setBusyToast = (msg) => { setBusy(msg); setTimeout(() => setBusy(''), 1800) }

  // --- Supabase state (loaded on demand per graph) ---
  const [versions, setVersions] = useState([])
  const [selectedVersionId, setSelectedVersionId] = useState('')
  const [docs, setDocs] = useState([])
  const [graph, setGraph] = useState({ nodes: [], edges: [] })

  const activeGraphRef = useRef('')
  const loadRequestRef = useRef(0)
  const graphMetadataRef = useRef(new Map())
  const graphSnapshotsRef = useRef(new Map())
  useEffect(() => { activeGraphRef.current = library.activeGraphId }, [library.activeGraphId])

  // --- derived ------------------------------------------------------------

  const viewingLatestVersion = !versions.length || selectedVersionId === versions[0]?.id
  const found = library.findGraph(library.activeGraphId)

  // Notes are written from documents the engine has finished reading.
  const readyDocs = useMemo(
    () => docs.filter((d) => statusClass(d.status) === 'ready'),
    [docs],
  )

  // The graph view is what the backend returned; shapeGraph ranks its nodes.
  const view = useMemo(() => shapeGraph(graph.nodes, graph.edges), [graph])
  const degradedCount = useMemo(
    () => view.nodes.filter((node) => node.status === 'needs_regeneration').length,
    [view.nodes],
  )
  const evolutionMaxWeek = Math.max(1, ...docs.map((d) => weekNumber(d.week)))
  const maxWeek = Math.max(
    evolutionMaxWeek,
    ...view.nodes.flatMap((n) => n.weeks || [n.week || 1]),
  )
  const viewKey = `${library.activeGraphId}:${selectedVersionId}`

  // Land on the newest week whenever the view or its range changes.
  useEffect(() => { setWeek(maxWeek) }, [viewKey, maxWeek])
  useEffect(() => { setSelected(null) }, [viewKey])

  // --- data: load Supabase graph when library.activeGraphId changes ---

  const loadGraphWorkspace = useCallback(async (
    supabaseGraphId,
    requestedVersionId = null,
    { refreshMetadata = true } = {},
  ) => {
    const requestId = ++loadRequestRef.current
    if (!supabaseGraphId) {
      setDocs([])
      setVersions([])
      setGraph({ nodes: [], edges: [] })
      setSelected(null)
      return
    }
    try {
      let metadata = graphMetadataRef.current.get(supabaseGraphId)
      if (refreshMetadata || !metadata) {
        const [{ documents = [] }, { versions: graphVersions = [] }] = await Promise.all([
          listGraphDocuments(supabaseGraphId),
          listGraphVersions(supabaseGraphId),
        ])
        metadata = { documents, versions: graphVersions }
        graphMetadataRef.current.set(supabaseGraphId, metadata)
        graphSnapshotsRef.current.delete(`${supabaseGraphId}:${graphVersions[0]?.id}`)
        if (graphVersions[0]) graphSnapshotsRef.current.delete(`${supabaseGraphId}:${graphVersions[0].id}:support`)
        setDocs(documents.map(graphDocumentRow))
        setVersions(graphVersions)
      }
      if (requestId !== loadRequestRef.current) return
      const { documents, versions: graphVersions } = metadata
      const version = graphVersions.find((item) => item.id === requestedVersionId) || graphVersions[0]
      setSelectedVersionId(version?.id || '')
      if (!version) {
        setGraph({ nodes: [], edges: [] })
        setSelected(null)
        return
      }
      const cacheKey = `${supabaseGraphId}:${version.id}`
      let snapshot = graphSnapshotsRef.current.get(cacheKey)
      if (!snapshot) {
        snapshot = await fetchGraphVersion(supabaseGraphId, version.id)
        if (requestId !== loadRequestRef.current) return
        graphSnapshotsRef.current.set(cacheKey, snapshot)
      }
      let activeSupport = null
      if (graphVersions[0]?.id === version.id) {
        const supportKey = `${cacheKey}:support`
        activeSupport = graphSnapshotsRef.current.get(supportKey)
        if (!activeSupport) {
          activeSupport = await getGraphActiveSupport(supabaseGraphId)
          graphSnapshotsRef.current.set(supportKey, activeSupport)
        }
      }
      if (requestId !== loadRequestRef.current) return
      setGraph(graphViewFromSnapshot(snapshot, activeSupport, documents))
      setSelected(null)
    } catch (e) {
      setError(e.message)
    }
  }, [])

  // When the library's active graph changes, load its Supabase data.
  useEffect(() => {
  const g = library.findGraph(library.activeGraphId)
  const supabaseId = g?.graph?.supabaseId
  // Do not leave the previous graph's documents visible while the new graph
  // is loading. The request guard above also prevents late responses from
  // putting the old graph's data back into this state.
  loadRequestRef.current += 1
  setDocs([])
  setVersions([])
  setGraph({ nodes: [], edges: [] })
  setSelected(null)
  if (supabaseId) {
    setBusy('Loading graph…')
    loadGraphWorkspace(supabaseId).catch(() => {}).finally(() => setBusy(''))
  } else {
  }
}, [library.activeGraphId])

  async function selectVersion(versionId) {
    setSelectedVersionId(versionId)
    const g = library.findGraph(library.activeGraphId)
    const supabaseId = g?.graph?.supabaseId
    if (!supabaseId || !versionId) return
    setBusy('Loading version…')
    setTab('graph')
    try {
      await loadGraphWorkspace(supabaseId, versionId, { refreshMetadata: false })
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy('')
    }
  }

  async function regenerateDegradedConcepts() {
    const supabaseId = library.findGraph(library.activeGraphId)?.graph?.supabaseId
    if (!supabaseId || !degradedCount) return
    if (!window.confirm(
      `Refresh the definitions for ${degradedCount} degraded concept${degradedCount === 1 ? '' : 's'}?\n\n`
      + 'Only affected definitions will be updated. The graph structure will not be rebuilt.',
    )) return
    setRefreshingDegraded(true)
    setToast({ message: 'Refreshing affected concept definitions…', kind: 'info' })
    try {
      const result = await refreshDegradedConcepts(supabaseId)
      await loadGraphWorkspace(supabaseId)
      setToast({
        message: result.updated
          ? `Updated ${result.updated} concept definition${result.updated === 1 ? '' : 's'}.`
          : 'No degraded definitions needed updating.',
        kind: 'success',
      })
    } catch (e) {
      setToast({ message: `Could not refresh definitions: ${e.message}`, kind: 'error' })
    } finally {
      setRefreshingDegraded(false)
      window.setTimeout(() => setToast(null), 4500)
    }
  }

  // --- upload -------------------------------------------------------------
  //
  // Files are queued and processed one at a time. When uploading to a local
  // graph (no supabaseId yet), the first file triggers graph creation on the
  // backend. Subsequent files go to the same graph. The graph view refreshes
  // after each file.

  const pending = useRef([])
  const draining = useRef(false)
  // React state updates are asynchronous. This ref keeps a newly-created
  // Supabase graph available to the next queued file immediately, even when
  // several files were selected in the same picker action.
  const supabaseIds = useRef(new Map())
  const patchItem = (key, patch) =>
    setQueue((q) => q.map((i) => (i.key === key ? { ...i, ...patch } : i)))

  async function processItem({ key, file, name, localGraphId, topicId }) {
    const g = library.findGraph(localGraphId)
    if (!g) {
      patchItem(key, { status: 'failed', message: 'Graph not found' })
      setError(`Graph for ${name} not found`)
      return
    }

    let supabaseGraphId = supabaseIds.current.get(localGraphId) || g.graph.supabaseId
    if (supabaseGraphId) supabaseIds.current.set(localGraphId, supabaseGraphId)
    patchItem(key, { status: 'uploading', message: '' })
    try {
      // Create the Supabase graph on first upload to this local graph
      if (!supabaseGraphId) {
        patchItem(key, { status: 'uploading', message: 'Creating graph…' })
        const { graph: created } = await createSupabaseGraph(g.graph.name, topicId)
        supabaseGraphId = created.id
        supabaseIds.current.set(localGraphId, supabaseGraphId)
        library.promoteGraphToSupabase(localGraphId, supabaseGraphId)
      }

      const { document } = await uploadGraphDocument(supabaseGraphId, file)
      library.assignDocs([document.id], localGraphId)
      patchItem(key, { status: 'reading', message: 'Building graph…' })
      await createGraphVersion(supabaseGraphId, [document.id])
      patchItem(key, { status: 'done', message: '' })
    } catch (e) {
      patchItem(key, { status: 'failed', message: e.message })
      setError(`Could not process ${name}: ${e.message}`)
    }

    // Refresh the view if this item is still for the active graph
    if (activeGraphRef.current === localGraphId && supabaseGraphId) {
      await loadGraphWorkspace(supabaseGraphId).catch(() => {})
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

  function enqueue(files, localGraphId) {
    if (!localGraphId) {
      setError('Choose or create a graph before uploading documents.')
      return false
    }

    const stamp = Date.now()
    const items = files.map((file, i) => {
      const ext = extOf(file.name)
      const ok = ALLOWED.includes(ext)
      return {
        key: `${stamp}-${i}-${file.name}`,
        file,
        name: file.name,
        localGraphId,
        topicId: library.findGraph(localGraphId)?.topic?.id || null,
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
    if (!library.activeGraphId) {
      setError('Choose or create a graph before uploading documents.')
      return
    }
    setUploadDest(library.activeGraphId)
    setUploadOpen(true)
  }

  const activeUploads = queue.filter((i) => ACTIVE.includes(i.status)).length

  async function remove(doc) {
    const g = library.findGraph(library.activeGraphId)
    const supabaseId = g?.graph?.supabaseId
    if (!supabaseId || !viewingLatestVersion) return
    try {
      await deleteGraphDocument(supabaseId, doc.id)
      library.assignDocs([doc.id], null)
      await loadGraphWorkspace(supabaseId)
    } catch (e) {
      setError(`Could not remove ${doc.name || doc.filename}: ${e.message}`)
    }
  }

  async function signOut() {
    await logout()
    navigate('/login', { replace: true })
  }

  const t = { count: docs.length, concepts: graph.nodes.length, relationships: graph.edges.length }

  // Header: subject › graph (from library structure)
  const title = found?.graph.name || 'Choose a graph'

  const fileDrag = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
  const canUpload = Boolean(library.activeGraphId) && viewingLatestVersion
  const graphLoading = busy === 'Loading graph…'

  return (
    <div className={`ws${sidebarCollapsed ? ' sidebar-collapsed' : ''}${sideOpen ? ' sidebar-drawer-open' : ''}`}>
      <SubjectSidebar
        library={library}
        docs={docs}
        open={sideOpen}
        onClose={() => setSideOpen(false)}
        onHome={() => navigate('/')}
        user={user}
        onSignOut={signOut}
      />
      <button
        type="button"
        className="sidebar-edge-toggle"
        aria-label="Toggle sidebar"
        title="Toggle sidebar"
        onClick={() => {
          const isMobile = window.matchMedia('(max-width: 900px)').matches
          const isOpen = isMobile ? sideOpen : !sidebarCollapsed
          if (isOpen) {
            setSideOpen(false)
            setSidebarCollapsed(true)
          } else {
            setSidebarCollapsed(false)
            setSideOpen(true)
          }
        }}
      >
        <span className="sidebar-toggle-open" aria-hidden="true">›</span>
        <span className="sidebar-toggle-close" aria-hidden="true">‹</span>
      </button>

      {/* ---------------- main ---------------- */}
      <main className="ws-main">
        <header className="ws-top">
          <div className="ws-top-left">
            <div className="ws-head">
              {found && (
                <div className="ws-crumb">
                  <span title={found.subject.name}>{found.subject.name}</span>
                  <span aria-hidden="true">›</span>
                  <span title={found.topic.name}>{found.topic.name}</span>
                </div>
              )}
              <div className="ws-title" title={title}>{title}</div>
              {library.activeGraphId && view.nodes.length ? (
                <div className="ws-facts">
                  <span><b>{docs.length}</b> document{docs.length === 1 ? '' : 's'}</span>
                  <span><b>{view.nodes.length}</b> concepts</span>
                  <span><b>{view.edges.length}</b> relationships</span>
                  <span>Week <b>{week}</b> of {maxWeek}</span>
                </div>
              ) : (
                <div className="ws-sub">
                  {!library.activeGraphId
                    ? 'Select or create a graph in the sidebar'
                    : docs.length
                      ? `${docs.length} document${docs.length === 1 ? '' : 's'} · no concepts yet`
                      : 'No sources yet'}
                </div>
              )}
            </div>
          </div>

          <div className="ws-top-right">
            <button type="button" className={`ws-upload${activeUploads ? ' is-busy' : ''}`}
                    onClick={openUpload} disabled={!canUpload}
                    title={!canUpload ? 'Choose a graph before uploading' : undefined}>
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
            <nav className="ws-tabs" aria-label="Workspace views">
              {TABS.map((x) => (
                <button
                  key={x}
                  className={`ws-tab${tab === x ? ' on' : ''}`}
                  onClick={() => setTab(x)}
                  disabled={
                    (x !== 'graph' && !library.activeGraphId) ||
                    (['notes', 'quiz'].includes(x) && !viewingLatestVersion)
                  }
                >
                  {x[0].toUpperCase() + x.slice(1)}
                </button>
              ))}
              <button
                type="button"
                className={`ws-tab${chatOpen ? ' on' : ''}`}
                disabled={!library.activeGraphId || !viewingLatestVersion}
                onClick={() => setChatOpen((open) => !open)}
              >
                Ask StudyGraph
              </button>
            </nav>
          </div>
        </header>

        {busy && !graphLoading && busy !== 'Loading version…' && (
          <div className="ws-banner">{busy}</div>
        )}
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
                if (files.length && enqueue(files, library.activeGraphId)) {
                  setUploadDest(library.activeGraphId)
                  setUploadOpen(true)
                }
              }}
            >
              {graphLoading ? (
                <div className="graph-loading" role="status" aria-live="polite">
                  <svg className="graph-loading-art" viewBox="0 0 96 72" fill="none" aria-hidden="true">
                    <path className="graph-loading-link" d="M21 48 42 22l29 12-16 23-34-9Z" />
                    <circle className="graph-loading-node graph-loading-node-a" cx="21" cy="48" r="6" />
                    <circle className="graph-loading-node graph-loading-node-b" cx="42" cy="22" r="7" />
                    <circle className="graph-loading-node graph-loading-node-c" cx="71" cy="34" r="5" />
                    <circle className="graph-loading-node graph-loading-node-d" cx="55" cy="57" r="6" />
                  </svg>
                  <h2>{busy === 'Loading version…' ? 'Opening graph version' : 'Loading your graph'}</h2>
                  <p>Gathering concepts and connections</p>
                  <span className="graph-loading-track" aria-hidden="true"><i /></span>
                </div>
              ) : view.nodes.length === 0 ? (
                <div className="empty-state">
                  {!library.activeGraphId ? (
                    <>
                      <h3>Choose a graph</h3>
                      <p>Select or create a graph in the sidebar to view its documents and knowledge graph.</p>
                    </>
                  ) : (
                    <>
                      {docs.length ? (
                        <>
                          <h3>Still reading your documents</h3>
                          <p>Concepts appear here as soon as each file has been read.</p>
                        </>
                      ) : (
                        <>
                          <h3>{found ? `Nothing in ${found.graph.name} yet` : 'Nothing here yet'}</h3>
                          <p>Add your lecture notes or slides and StudyGraph will build the
                             graph right here.</p>
                        </>
                      )}
                      {viewingLatestVersion && (
                        <UploadDrop
                          tone="dark"
                          title={found ? `Add documents to ${found.graph.name}` : 'Drop your documents here'}
                          onFiles={(files) => enqueue(files, library.activeGraphId)}
                        />
                      )}
                      <UploadQueue items={queue} />
                    </>
                  )}
                </div>
              ) : (
                <ConceptGraph
                  key={library.activeGraphId}
                  nodes={view.nodes}
                  edges={view.edges}
                  versions={versions}
                  selectedVersionId={selectedVersionId}
                  onVersionChange={selectVersion}
                  loadingVersion={busy === 'Loading version…'}
                  week={week}
                  onSelectNode={setSelected}
                  selectedId={selected?.id}
                  degradedCount={degradedCount}
                  onRefreshDegraded={regenerateDegradedConcepts}
                  refreshingDegraded={refreshingDegraded}
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
              graphId={found?.graph?.supabaseId}
              node={selected}
              nodes={view.nodes}
              edges={view.edges}
              maxWeek={evolutionMaxWeek}
              onClose={() => setSelected(null)}
            />
          )}

          {tab === 'documents' && (
            <DocumentsTab
              docs={docs} totals={t} topic={found?.topic || null}
              library={library} queue={queue}
              onFiles={(files) => enqueue(files, library.activeGraphId)}
              onRemove={remove}
              readOnly={!viewingLatestVersion}
            />
          )}

          {tab === 'notes' && <NotesPane graphId={found?.graph?.supabaseId} readyDocs={readyDocs} />}
          {tab === 'quiz' && <QuizPane graphId={found?.graph?.supabaseId} onToast={setBusyToast} />}
        </section>
      </main>

      {chatOpen && library.activeGraphId && (
        <>
          <button
            type="button"
            className="chat-backdrop"
            aria-label="Close StudyGraph chat"
            onClick={() => setChatOpen(false)}
          />
          <ChatPanel
            graphId={found?.graph?.supabaseId}
            width={chatWidth}
            onResize={setChatWidth}
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
        </>
      )}

      {toast && (
        <div className={`ws-toast ${toast.kind}`} role="status">
          {toast.message}
        </div>
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
            <th>File</th><th>Status</th><th>Week</th>
            <th className="r">Concepts</th><th className="r">Links</th><th />
          </tr>
        </thead>
        <tbody>
          {docs.length === 0 ? (
            <tr><td colSpan={6}><div className="empty">
              No documents yet. Add some above and StudyGraph will start reading them.
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
