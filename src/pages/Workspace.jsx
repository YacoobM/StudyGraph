import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import ConceptGraph from '../components/ConceptGraph.jsx'
import QuizPane from '../components/QuizPane.jsx'
import { documentTitle } from '../utils/documentTitle.js'
import Inspector from '../components/Inspector.jsx'
import NotesPane from '../components/NotesPane.jsx'
import {
  listDocuments, uploadDocument, deleteDocument, fetchGraph, askQuestion,
  getPipelineStatus, patchDocument, fetchDocumentStats, retryFailedUploadBatches,
} from '../services/api.js'
import '../styles/studygraph.css'
import './Workspace.css'

const TABS = ['graph', 'documents', 'chat', 'notes', 'quiz']
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
const fmtSize = (kb) => (kb == null ? '—' : kb >= 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${kb} KB`)
function fmtWhen(ts) {
  if (!ts) return '—'
  const m = Math.round((Date.now() - ts * 1000) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  const h = Math.round(m / 60)
  return h < 24 ? `${h} hr ago` : `${Math.round(h / 24)} d ago`
}

export default function Workspace() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  const [tab, setTab] = useState('graph')
  const [docs, setDocs] = useState([])
  const [totals, setTotals] = useState(null)
  const [graph, setGraph] = useState({ nodes: [], edges: [] })
  const [selected, setSelected] = useState(null)
  const [week, setWeek] = useState(1)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [canRetry, setCanRetry] = useState(false)
  const [drag, setDrag] = useState(false)
  const setBusyToast = (msg) => { setBusy(msg); setTimeout(() => setBusy(''), 1800) }
  const maxWeek = Math.max(1, ...graph.nodes.map((n) => n.week || 1))
  const fileInput = useRef(null)

  // Notes are written from documents the engine has finished reading, so the
  // picker there only offers rows that have settled.
  const readyDocs = useMemo(
    () => docs.filter((d) => statusClass(d.status) === 'ready'),
    [docs],
  )

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
      const data = await listDocuments()
      const rows = data.documents || []
      setDocs(rows)
      setTotals(data.totals || null)
      setError('')

      const unsettled = rows.filter(
        (d) => ['running', 'queued', 'processing'].includes((d.status || '').toLowerCase()),
      )
      if (unsettled.length === 0) return rows

      const { stats } = await fetchDocumentStats()
      const fixes = unsettled.filter((d) => stats[d.name || d.filename])
      if (fixes.length === 0) return rows

      await Promise.all(fixes.map((d) => {
        const st = stats[d.name || d.filename]
        return patchDocument(d.id, {
          status: 'done',
          concepts: st.concepts,
          relationships: st.edges,
          ...(st.week ? { week: st.week } : {}),
        }).catch(() => null)
      }))

      const fresh = await listDocuments()
      setDocs(fresh.documents || [])
      setTotals(fresh.totals || null)
      return fresh.documents || []
    } catch (e) {
      setError(e.message)
    }
  }, [])

  /**
   * Maps main.py's /api/graph into what ConceptGraph draws.
   *
   * The real contract, which differs from the dev stub's older shape:
   *   node.name            the readable label (node.id is snake_case)
   *   node.weeks           the weeks this concept appears in
   *   node.source          [{file, page...}], not a list of strings
   *   edge.source/.target  snake_case node ids, NOT edge.from/.to
   * Degree is not returned, so it is counted from the edges here.
   */
  const loadGraph = useCallback(async () => {
    try {
      const g = await fetchGraph()
      const rawNodes = g.nodes || []
      const rawEdges = g.edges || []
      if (!rawNodes.length) { setGraph({ nodes: [], edges: [] }); return }

      const weekNo = (w) => {
        const n = parseInt(String(w).replace(/\D+/g, ''), 10)
        return Number.isFinite(n) ? n : 1
      }

      // count connections per node id, since the backend does not send degree
      const degree = {}
      rawEdges.forEach((e) => {
        degree[e.source] = (degree[e.source] || 0) + 1
        degree[e.target] = (degree[e.target] || 0) + 1
      })

      const prelim = rawNodes.map((n) => {
        const weeks = (n.weeks || []).map(weekNo)
        return {
          id: String(n.id),
          name: n.name || String(n.id).replace(/_/g, ' '),
          week: weeks.length ? Math.min(...weeks) : 1,
          weeks,
          degree: degree[n.id] || 0,
          description: n.description || '',
          sources: (n.source || []).map((x) => (typeof x === 'string' ? x : x.file)),
        }
      })

      // "Core" is relative to this graph's own connectivity — a fixed
      // threshold makes every node in a small graph look identical.
      const ranked = [...prelim].map((n) => n.degree).sort((a, b) => b - a)
      const cut = Math.max(1, ranked[Math.floor(ranked.length / 3)] ?? 1)
      const nodes = prelim.map((n) => ({ ...n, kind: n.degree >= cut ? 'core' : 'support' }))

      const ids = new Set(nodes.map((n) => n.id))
      const edges = rawEdges
        .filter((e) => ids.has(String(e.source)) && ids.has(String(e.target)))
        .map((e) => ({
          source: String(e.source),
          target: String(e.target),
          label: e.label || e.relation || 'relates to',
          strength: e.is_inferred ? 'low' : 'medium',
        }))

      setGraph({ nodes, edges })
      setWeek(Math.max(1, ...nodes.map((n) => n.week)))
      return { nodes, edges }
    } catch {
      /* leave what is on screen; the Documents tab reports real errors */
    }
  }, [])

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
      ['running', 'queued', 'processing', 'extracting', 'seeding', 'building graph']
        .includes(String(d.status || '').toLowerCase()))
    if (!pending.length || !gNodes.length) return false

    let stage = 'idle'
    try { stage = (await getPipelineStatus()).stage || 'idle' } catch { return false }
    if (stage === 'running') return false          // genuinely still working

    let changed = false
    for (const d of pending) {
      const file = d.name || d.filename
      const mine = gNodes.filter((n) => (n.sources || []).includes(file))
      if (!mine.length) continue                   // nothing of this file in the graph yet
      const ids = new Set(mine.map((n) => n.id))
      const links = gEdges.filter((e) => ids.has(e.source) && ids.has(e.target)).length
      await patchDocument(d.id, {
        status: 'done', concepts: mine.length, relationships: links,
      }).catch(() => {})
      changed = true
    }
    return changed
  }, [])

  useEffect(() => {
    (async () => {
      const [docsData, g] = await Promise.all([loadDocs(), loadGraph()])
      if (docsData && g && await reconcile(docsData, g.nodes, g.edges)) loadDocs()
    })()
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  // --- upload -------------------------------------------------------------

  async function handleFile(file) {
    if (!file) return
    const ext = extOf(file.name)
    if (!ALLOWED.includes(ext)) {
      setError(`${file.name} is a .${ext} file. Use PDF, DOCX, PPTX, TXT or MD.`)
      return
    }

    setError('')
    setCanRetry(false)
    setBusy(`Uploading ${file.name}…`)
    try {
      const result = await uploadDocument(file, 'llm', null, (s) => {
        if (s.stage === 'running') setBusy(`${s.operation || 'Processing'}…`)
      }, null)
      if (result.raw?.stage === 'partially_processed') {
        setError(`Some batches for ${file.name} could not be processed.`)
        setCanRetry(true)
      }
      setBusy('')
      await loadDocs()
      await loadGraph()          // the graph is rebuilt from the new concepts
      setTab('graph')
    } catch (e) {
      setBusy('')
      setError(`Could not process ${file.name}: ${e.message}`)
      loadDocs()
    }
  }

  async function retryFailed() {
    setCanRetry(false)
    setError('')
    setBusy('Retrying failed extraction batches…')
    try {
      const status = await retryFailedUploadBatches()
      if (status.stage === 'partially_processed') {
        setError('Some extraction batches still could not be processed.')
        setCanRetry(true)
      }
      await loadDocs()
      await loadGraph()
    } catch (e) {
      setError(`Could not retry failed batches: ${e.message}`)
      setCanRetry(true)
    } finally {
      setBusy('')
    }
  }

  async function remove(doc) {
    try {
      await deleteDocument(doc.id)
      await loadDocs()
      await loadGraph()
    } catch (e) {
      setError(`Could not remove ${doc.name || doc.filename}: ${e.message}`)
    }
  }

  function newGraph() {
    // Nothing to create up front — a graph is whatever the documents produce,
    // so this goes where sources are added.
    setTab('documents')
    setError('')
  }

  async function signOut() {
    await logout()
    navigate('/login', { replace: true })
  }

  const t = totals || { count: docs.length, concepts: 0, relationships: 0 }

  return (
    <div className="ws">
      {/* ---------------- sidebar ---------------- */}
      <aside className="ws-side">
        <div className="side-brand" onClick={() => navigate('/')}>StudyGraph</div>

        <button className="side-new" onClick={newGraph}>+ New graph</button>

        <div className="side-sec">Your graphs</div>
        <button className="side-item on">
          <span className="si-dot" />
          {docs.length ? `${docs.length} source${docs.length > 1 ? 's' : ''}` : 'Untitled graph'}
        </button>

        <div className="side-sec">At a glance</div>
        <div className="side-stats">
          <div><span className="mono">{t.count}</span> documents</div>
          <div><span className="mono">{t.concepts}</span> concepts</div>
          <div><span className="mono">{t.relationships}</span> links</div>
        </div>

        <div className="side-foot">
          <div className="side-user">
            <div className="su-avatar">{(user?.name || '··').slice(0, 2).toUpperCase()}</div>
            <div>
              <div className="su-name">{user?.name || 'Not signed in'}</div>
              <div className="su-mail">{user?.email || ''}</div>
            </div>
          </div>
          <button className="side-signout" onClick={signOut}>Sign out</button>
        </div>
      </aside>

      {/* ---------------- main ---------------- */}
      <main className="ws-main">
        <header className="ws-top">
          <div>
            <div className="ws-title">
              {docs.length ? documentTitle(docs[0]) : 'Untitled graph'}
            </div>
            <div className="ws-sub">
              {graph.nodes.length
                ? `${graph.nodes.length} concepts · ${graph.edges.length} links`
                : 'No sources yet'}
            </div>
          </div>
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
          </nav>
        </header>

        {busy && <div className="ws-banner">{busy}</div>}
        {error && (
          <div className="ws-banner err">
            <span>{error}</span>
            {canRetry && <button onClick={retryFailed}>Retry failed batches</button>}
          </div>
        )}

        <section className="ws-body">
          {tab === 'graph' && (
            <div className={`graph-canvas-wrap${graph.nodes.length ? '' : ' is-empty'}`}>
              {graph.nodes.length === 0 ? (
                <div className="empty-state">
                  <h3>Nothing here yet</h3>
                  <p>Drop a PDF and StudyGraph will start building the graph as
                     soon as it has something to read.</p>
                  <button className="btn-primary" onClick={() => setTab('documents')}>
                    Upload a document
                  </button>
                </div>
              ) : (
                <ConceptGraph
                  nodes={graph.nodes}
                  edges={graph.edges}
                  week={week}
                  maxWeek={maxWeek}
                  onWeekChange={setWeek}
                  onSelectNode={setSelected}
                  selectedId={selected?.id}
                />
              )}
            </div>
          )}
          {tab === 'graph' && graph.nodes.length > 0 && (
            <Inspector
              node={selected}
              nodes={graph.nodes}
              edges={graph.edges}
              maxWeek={maxWeek}
            />
          )}

          {tab === 'documents' && (
            <DocumentsTab
              docs={docs} totals={t} drag={drag} setDrag={setDrag}
              fileInput={fileInput} onFile={handleFile} onRemove={remove} busy={busy}
            />
          )}

          {tab === 'chat' && <ChatTab hasDocs={docs.length > 0} />}
          {tab === 'notes' && <NotesPane readyDocs={readyDocs} />}
          {tab === 'quiz' && <QuizPane onToast={setBusyToast} />}
        </section>
      </main>
    </div>
  )
}

/* ====================== documents ====================== */

function DocumentsTab({ docs, totals, drag, setDrag, fileInput, onFile, onRemove, busy }) {
  return (
    <div className="pane pane-scroll">
      <div className="pane-head">
        <div>
          <h2>Documents</h2>
          <p>Everything this graph was built from. Removing a source takes its
             concepts out of the graph.</p>
        </div>
        <div className="stat-row">
          <div className="stat"><div className="n mono">{totals.count}</div><div className="l">Documents</div></div>
          <div className="stat"><div className="n mono">{totals.concepts}</div><div className="l">Concepts</div></div>
          <div className="stat"><div className="n mono">{totals.relationships}</div><div className="l">Links</div></div>
        </div>
      </div>

      <div
        className={`drop${drag ? ' over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true) }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); onFile(e.dataTransfer?.files?.[0]) }}
      >
        <h3>Add a document</h3>
        <p>Drop a PDF, DOCX, PPTX or TXT here, or choose a file.</p>
        <button className="btn-accent" disabled={Boolean(busy)}
                onClick={() => fileInput.current?.click()}>
          {busy ? 'Working…' : 'Choose a file'}
        </button>
        <input ref={fileInput} type="file" accept=".pdf,.docx,.pptx,.txt,.md"
               onChange={(e) => { onFile(e.target.files?.[0]); e.target.value = '' }} />
      </div>

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
              No documents yet. Add one above and StudyGraph will start reading it.
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
                <button className="row-btn danger" onClick={() => onRemove(d)}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ====================== chat ====================== */

function ChatTab({ hasDocs }) {
  const [messages, setMessages] = useState([
    { who: 'ai', text: 'Ask me anything about the documents you have uploaded.' },
  ])
  const [text, setText] = useState('')
  const [thinking, setThinking] = useState(false)
  const scroll = useRef(null)

  useEffect(() => {
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight
  }, [messages, thinking])

  async function send() {
    const q = text.trim()
    if (!q || thinking) return
    setMessages((m) => [...m, { who: 'me', text: q }])
    setText('')
    setThinking(true)
    try {
      const r = await askQuestion(q)
      setMessages((m) => [...m, {
        who: 'ai', text: r.answer || 'No answer came back.', cite: r.citation_label,
      }])
    } catch (e) {
      setMessages((m) => [...m, {
        who: 'ai',
        text: e.response?.status === 400
          ? 'No documents have been processed yet. Add one in the Documents tab first.'
          : `Could not reach the answer service: ${e.message}`,
      }])
    } finally {
      setThinking(false)
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
          placeholder={hasDocs ? 'Ask about your documents…' : 'Upload a document first'}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
        />
        <button className="btn-accent" onClick={send} disabled={thinking}>Ask</button>
      </div>
    </div>
  )
}
