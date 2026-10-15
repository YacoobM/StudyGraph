import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../lib/auth.jsx'
import ConceptGraph from '../../components/graph/ConceptGraph.jsx'
import QuizPane from '../../components/quiz/QuizPane.jsx'
import Inspector from '../../components/graph/Inspector.jsx'
import NotesPane from '../../components/notes/NotesPane.jsx'
import ChatPanel from '../../components/chat/ChatPanel.jsx'
import SubjectSidebar from '../../components/library/SubjectSidebar.jsx'
import UploadDialog, { UploadDrop, UploadQueue } from '../../components/library/UploadPanel.jsx'
import { useLibrary } from '../../lib/library.js'

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
} from '../../services/api.js'

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
 * Groups a concept's provenance rows into one source per document, keeping
 * the pages and excerpts so the inspector can show real evidence.
 */
function sourcesFor(rows, documentsById) {
  const bySource = new Map()
  rows.forEach((row) => {
    const documentId = row.document_id ? String(row.document_id) : null
    const document = documentId ? documentsById.get(documentId) : null
    const name = (document?.filename || row.document_filename || '').trim()
    const key = documentId || `name:${name || 'unknown'}`
    const week = Number(row.source_week) || weekNumber(document?.week)
    let source = bySource.get(key)
    if (!source) {
      source = {
        key,
        name: name || 'Unnamed document',
        documentId,
        week,
        weekLabel: `Week ${week}`,
        pages: new Set(),
        excerpts: [],
        // A row without a document id was never linked, which is not the same as deleted.
        deleted: Boolean(documentId && !document),
        unlinked: !documentId,
      }
      bySource.set(key, source)
    }
    source.week = Math.min(source.week, week)
    source.weekLabel = `Week ${source.week}`
    const start = Number(row.page_start)
    const end = Number(row.page_end) || start
    if (Number.isFinite(start) && start > 0) {
      for (let page = start; page <= Math.min(end, start + 20); page += 1) source.pages.add(page)
    }
    const excerpt = String(row.source_excerpt || '').trim()
    if (excerpt && !source.excerpts.includes(excerpt)) source.excerpts.push(excerpt)
  })
  return [...bySource.values()]
    .map((source) => ({ ...source, pages: [...source.pages].sort((a, b) => a - b) }))
    .sort((a, b) => a.week - b.week || a.name.localeCompare(b.name))
}

/** Turns a Supabase graph-version snapshot into the nodes and edges ConceptGraph draws. */
function graphViewFromSnapshot(snapshot, activeSupport = null, documents = []) {
  const concepts = snapshot?.concepts || []
  const relationships = snapshot?.relationships || []
  const documentsById = new Map(documents.map((d) => [String(d.id), d]))
  const rowsByConcept = new Map()
  ;(snapshot?.concept_provenance || []).forEach((row) => {
    const id = String(row.concept_id)
    if (!rowsByConcept.has(id)) rowsByConcept.set(id, [])
    rowsByConcept.get(id).push(row)
  })

  const nodes = concepts.map((concept) => {
    let sources = sourcesFor(rowsByConcept.get(String(concept.id)) || [], documentsById)
    if (!sources.length) {
      // Older versions have no provenance rows; fall back to the documents that support the concept.
      sources = (activeSupport?.concepts?.[concept.normalized_name] || [])
        .map((documentId) => documentsById.get(String(documentId)))
        .filter(Boolean)
        .map((document) => ({
          key: String(document.id),
          name: document.filename,
          documentId: String(document.id),
          week: weekNumber(document.week),
          weekLabel: `Week ${weekNumber(document.week)}`,
          pages: [],
          excerpts: [],
          deleted: false,
          unlinked: false,
        }))
    }
    const weeks = [...new Set(sources.map((s) => s.week))].sort((a, b) => a - b)

    return {
      id: String(concept.id),
      name: concept.name,
      description: concept.description || '',
      aliases: concept.aliases || [],
      status: concept.status || 'active',
      week: weeks[0] || 1,
      weeks: weeks.length ? weeks : [1],
      sources,
    }
  })

  const edges = relationships.map((r) => ({
    source: String(r.source_concept_id),
    target: String(r.target_concept_id),
    label: String(r.relationship_type || 'relates to').replace(/_/g, ' '),
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
  // One place for short confirmations; errors stay in the banner until dismissed.
  const toastTimer = useRef(null)
  const notify = useCallback((message, kind = 'success', ms = 3200) => {
    window.clearTimeout(toastTimer.current)
    setToast({ message, kind })
    if (ms) toastTimer.current = window.setTimeout(() => setToast(null), ms)
  }, [])
  useEffect(() => () => window.clearTimeout(toastTimer.current), [])

  // --- Supabase state (loaded on demand per graph) ---
  const [versions, setVersions] = useState([])
  const [selectedVersionId, setSelectedVersionId] = useState('')
  const [docs, setDocs] = useState([])
  const [graph, setGraph] = useState({ nodes: [], edges: [] })

  const activeGraphRef = useRef('')
  const loadRequestRef = useRef(0)
  // Keeps the sidebar's document counts in step with what the backend returned.
  const syncDocsRef = useRef(null)
  syncDocsRef.current = library.setGraphDocs
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
        if (requestId !== loadRequestRef.current) return
        metadata = { documents, versions: graphVersions }
        graphMetadataRef.current.set(supabaseGraphId, metadata)
        syncDocsRef.current?.(supabaseGraphId, documents.map((d) => String(d.id)))
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
      if (requestId !== loadRequestRef.current) return
      setError(e.message)
      throw e
    }
  }, [])

  // Load the active graph. Depends on the backend id too: after a refresh the
  // library can resolve the id after the selection is restored.
  const activeSupabaseId = found?.graph?.supabaseId || null
  const [graphStatus, setGraphStatus] = useState('idle')
  useEffect(() => {
    // Clear the previous graph first; the request guard drops late responses.
    loadRequestRef.current += 1
    setDocs([])
    setVersions([])
    setGraph({ nodes: [], edges: [] })
    setSelected(null)
    if (!activeSupabaseId) {
      setGraphStatus('idle')
      return
    }
    let live = true
    setGraphStatus('loading')
    loadGraphWorkspace(activeSupabaseId)
      .then(() => live && setGraphStatus('ready'))
      .catch(() => live && setGraphStatus('error'))
    return () => { live = false }
  }, [library.activeGraphId, activeSupabaseId, loadGraphWorkspace])

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
    notify('Refreshing affected concept definitions…', 'info', 0)
    try {
      const result = await refreshDegradedConcepts(supabaseId)
      await loadGraphWorkspace(supabaseId)
      notify(result.updated
        ? `Updated ${result.updated} concept definition${result.updated === 1 ? '' : 's'}.`
        : 'No degraded definitions needed updating.')
    } catch (e) {
      notify(`Could not refresh definitions: ${e.message}`, 'error', 6000)
    } finally {
      setRefreshingDegraded(false)
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
    // Same name as a document already in the open graph: skip it rather than duplicate its concepts.
    const existing = localGraphId === library.activeGraphId
      ? new Set(docs.map((d) => String(d.name || d.filename).toLowerCase()))
      : new Set()
    const items = files.map((file, i) => {
      const ext = extOf(file.name)
      let message = ''
      if (ext === 'json') {
        message = 'JSON files are not learning material. Use PDF, DOCX, PPTX, TXT or MD.'
      } else if (!ALLOWED.includes(ext)) {
        message = `.${ext} files aren't supported. Use PDF, DOCX, PPTX, TXT or MD.`
      } else if (existing.has(file.name.toLowerCase())) {
        message = 'Already in this graph. Remove the old copy first to replace it.'
      }
      return {
        key: `${stamp}-${i}-${file.name}`,
        file,
        name: file.name,
        localGraphId,
        topicId: library.findGraph(localGraphId)?.topic?.id || null,
        status: message ? 'failed' : 'queued',
        message,
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
    const name = doc.name || doc.filename
    if (!window.confirm(`Remove "${name}" from this graph?\n\nIts concepts leave the graph in a new version. Older versions keep them.`)) return
    try {
      await deleteGraphDocument(supabaseId, doc.id)
      library.assignDocs([doc.id], null)
      await loadGraphWorkspace(supabaseId)
    } catch (e) {
      setError(`Could not remove ${name}: ${e.message}`)
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
  const graphLoading = graphStatus === 'loading'

  return (
    <div className={`ws${sidebarCollapsed ? ' sidebar-collapsed' : ''}${sideOpen ? ' sidebar-drawer-open' : ''}`}>
      <SubjectSidebar
        library={library}
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
              {graphLoading ? (
                <div className="ws-sub ws-sub-loading" role="status">
                  <span className="ws-skel" aria-hidden="true" />
                  Loading graph…
                </div>
              ) : library.activeGraphId && view.nodes.length ? (
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
                  aria-current={tab === x ? 'page' : undefined}
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
                aria-expanded={chatOpen}
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
          <div className="ws-banner err" role="alert">
            <span>{error}</span>
            <button type="button" className="ws-banner-x" onClick={() => setError('')} aria-label="Dismiss">×</button>
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
                    <GettingStarted hasSubjects={library.subjects.length > 0} />
                  ) : (
                    <>
                      {docs.some((d) => ['working', 'queued'].includes(statusClass(d.status))) ? (
                        <>
                          <h3>Still reading your documents</h3>
                          <p>Concepts appear here as soon as each file has been read.</p>
                        </>
                      ) : docs.length ? (
                        <>
                          <h3>No concepts found yet</h3>
                          <p>
                            {docs.some((d) => statusClass(d.status) === 'failed')
                              ? 'Some documents could not be read. The Documents tab shows which ones.'
                              : 'StudyGraph read these documents but found no concepts. Scanned or image-only pages can come back empty. Try another file.'}
                          </p>
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
              onSelectConcept={(id) => setSelected(view.nodes.find((n) => n.id === id) || null)}
            />
          )}

          {tab === 'documents' && (
            <DocumentsTab
              docs={docs} totals={t}
              queue={queue}
              onFiles={(files) => enqueue(files, library.activeGraphId)}
              onRemove={remove}
              readOnly={!viewingLatestVersion}
            />
          )}

          {tab === 'notes' && <NotesPane graphId={found?.graph?.supabaseId} readyDocs={readyDocs} />}
          {tab === 'quiz' && <QuizPane graphId={found?.graph?.supabaseId} onToast={notify} />}
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
        <div className={`ws-toast ${toast.kind}`} role={toast.kind === 'error' ? 'alert' : 'status'}>
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

/* ====================== first visit ====================== */

const INTRO_KEY = 'studygraph.intro.dismissed'
const readFlag = (key) => { try { return window.localStorage.getItem(key) === '1' } catch { return false } }

/** A short, dismissible guide shown until the reader closes it. */
function GettingStarted({ hasSubjects }) {
  const [hidden, setHidden] = useState(() => readFlag(INTRO_KEY))
  const dismiss = () => {
    setHidden(true)
    try { window.localStorage.setItem(INTRO_KEY, '1') } catch { /* private mode */ }
  }
  if (hidden) {
    return (
      <>
        <h3>Choose a graph</h3>
        <p>Select or create a graph in the sidebar to view its documents and knowledge graph.</p>
      </>
    )
  }
  const steps = [
    ['Organise', hasSubjects ? 'Pick a graph in the sidebar, or add a new one under a topic.' : 'Create a subject, a topic and a graph in the sidebar.'],
    ['Upload', 'Add lecture slides or notes: PDF, DOCX, PPTX, TXT or MD.'],
    ['Explore', 'Click a concept to see its connections, sources and how it evolved.'],
    ['Revise', 'Turn the graph into revision notes and quizzes from the tabs above.'],
  ]
  return (
    <div className="intro" role="region" aria-label="Getting started">
      <h3>Welcome to StudyGraph</h3>
      <p>Four steps from your lecture material to a study plan.</p>
      <ol className="intro-steps">
        {steps.map(([title, text], i) => (
          <li key={title}><span className="intro-n">{i + 1}</span><b>{title}</b><span>{text}</span></li>
        ))}
      </ol>
      <button type="button" className="intro-dismiss" onClick={dismiss}>Got it</button>
    </div>
  )
}

/* ====================== documents ====================== */

function DocumentsTab({ docs, totals, queue, onFiles, onRemove, readOnly }) {
  return (
    <div className="pane pane-scroll">
      <div className="pane-head">
        <div>
          <h2>Documents</h2>
          <p>The sources this graph is built from. Removing one takes its concepts out of the next graph version.</p>
        </div>
        {/* Graph-wide totals: per-document counts overlap, so they are not summed. */}
        <div className="stat-row">
          <div className="stat"><div className="n mono">{totals.count}</div><div className="l">Documents</div></div>
          <div className="stat"><div className="n mono">{totals.concepts}</div><div className="l">Concepts</div></div>
          <div className="stat"><div className="n mono">{totals.relationships}</div><div className="l">Links</div></div>
        </div>
      </div>

      {readOnly ? (
        <div className="empty">You're viewing an older version of this graph. Switch to the latest version to add or remove documents.</div>
      ) : (
        <UploadDrop
          title="Add documents to this graph"
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
