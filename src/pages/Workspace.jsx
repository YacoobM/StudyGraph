import React, { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth.jsx'
import ConceptGraph from '../components/ConceptGraph.jsx'
import QuizPane from '../components/QuizPane.jsx'
import {
  listDocuments, uploadDocument, deleteDocument, fetchGraph, askQuestion,
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
  const [drag, setDrag] = useState(false)
  const setBusyToast = (msg) => { setBusy(msg); setTimeout(() => setBusy(''), 1800) }
  const maxWeek = Math.max(1, ...graph.nodes.map((n) => n.week || 1))
  const fileInput = useRef(null)

  // --- data ---------------------------------------------------------------

  const loadDocs = useCallback(async () => {
    try {
      const data = await listDocuments()
      setDocs(data.documents || [])
      setTotals(data.totals || null)
      setError('')
    } catch (e) {
      setError(e.message)
    }
  }, [])

  /** Maps the backend's {nodes,edges} into what ConceptGraph draws. */
  const loadGraph = useCallback(async () => {
    try {
      const g = await fetchGraph()
      const raw = (g.nodes || []).map((n) => {
        const weeks = (n.weeks || [])
          .map((w) => parseInt(String(w).replace(/\D+/g, ''), 10))
          .filter(Number.isFinite)
        return {
          id: String(n.id ?? n.label),
          name: n.label ?? n.id,
          week: weeks.length ? Math.min(...weeks) : 1,
          degree: n.degree ?? 0,
          color: n.color,
          sources: n.sources || [],
        }
      })

      // "Core" is relative, not an absolute degree cut-off. A fixed threshold
      // makes every node in a small graph look identical, which is exactly
      // when the emphasis is most useful. Take the best-connected third,
      // requiring at least one connection to qualify.
      const sorted = [...raw].map((n) => n.degree).sort((a, b) => b - a)
      const cut = Math.max(1, sorted[Math.floor(sorted.length / 3)] ?? 1)
      const nodes = raw.map((n) => ({ ...n, kind: n.degree >= cut ? 'core' : 'support' }))

      const ids = new Set(nodes.map((n) => n.id))
      const edges = (g.edges || [])
        .filter((e) => ids.has(String(e.from)) && ids.has(String(e.to)))
        .map((e) => ({
          source: String(e.from), target: String(e.to),
          label: e.relation || 'relates to', strength: 'medium',
        }))
      setGraph({ nodes, edges })
      setWeek(Math.max(1, ...nodes.map((n) => n.week || 1)))
      if (nodes.length && !selected) setSelected(nodes[0])
    } catch {
      /* leave whatever is on screen; the documents tab reports real errors */
    }
  }, [selected])

  useEffect(() => { loadDocs(); loadGraph() }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  // --- upload -------------------------------------------------------------

  async function handleFile(file) {
    if (!file) return
    const ext = extOf(file.name)
    if (!ALLOWED.includes(ext)) {
      setError(`${file.name} is a .${ext} file. Use PDF, DOCX, PPTX, TXT or MD.`)
      return
    }

    setError('')
    setBusy(`Uploading ${file.name}…`)
    try {
      await uploadDocument(file, 'llm', null, (s) => {
        if (s.stage === 'running') setBusy(`${s.operation || 'Processing'}…`)
      }, null)
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
              {docs.length ? docs[0].name || docs[0].filename : 'Untitled graph'}
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
        {error && <div className="ws-banner err">{error}</div>}

        <section className="ws-body">
          {tab === 'graph' && (
            <div className={`graph-canvas-wrap${graph.nodes.length ? '' : ' is-empty'}`}>
              {graph.nodes.length === 0 ? (
                <div className="empty-state">
                  <h3>No concepts yet</h3>
                  <p>Add a document and StudyGraph will map what is inside it.</p>
                  <button className="btn-primary" onClick={() => setTab('documents')}>
                    Upload a document
                  </button>
                </div>
              ) : (
                <>
                  <ConceptGraph
                    nodes={graph.nodes}
                    edges={graph.edges}
                    week={week}
                    maxWeek={maxWeek}
                    onSelectNode={setSelected}
                  />
                  {maxWeek > 1 && (
                    <div className="ws-scrubber">
                      <span className="scrub-label">Week</span>
                      <input className="scrub" type="range" min="1" max={maxWeek} value={week}
                             onChange={(e) => setWeek(Number(e.target.value))} />
                      <span className="scrub-num mono">{week} / {maxWeek}</span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {tab === 'documents' && (
            <DocumentsTab
              docs={docs} totals={t} drag={drag} setDrag={setDrag}
              fileInput={fileInput} onFile={handleFile} onRemove={remove} busy={busy}
            />
          )}

          {tab === 'chat' && <ChatTab hasDocs={docs.length > 0} />}
          {tab === 'notes' && <NotesTab graph={graph} docs={docs} />}
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

/* ====================== notes ====================== */

function NotesTab({ graph, docs }) {
  if (!graph.nodes.length) {
    return (
      <div className="pane pane-scroll">
        <div className="empty tall">
          <h3>No notes yet</h3>
          <p>Notes are written from the concepts StudyGraph finds. Add a document to start.</p>
        </div>
      </div>
    )
  }

  const core = graph.nodes.filter((n) => n.kind === 'core')
  const linksFor = (n) => graph.edges.filter(
    (e) => (e.source.id ?? e.source) === n.id || (e.target.id ?? e.target) === n.id)

  return (
    <div className="pane pane-scroll">
      <div className="pane-head">
        <div>
          <h2>Study notes</h2>
          <p>Built from {docs.length} source{docs.length === 1 ? '' : 's'} — {graph.nodes.length} concepts.</p>
        </div>
      </div>
      <div className="notes">
        {(core.length ? core : graph.nodes.slice(0, 8)).map((n) => (
          <article className="note" key={n.id}>
            <h3>{n.name}</h3>
            <ul>
              {linksFor(n).slice(0, 6).map((e, i) => {
                const other = (e.source.id ?? e.source) === n.id
                  ? (e.target.name ?? e.target.id ?? e.target)
                  : (e.source.name ?? e.source.id ?? e.source)
                return <li key={i}><span className="rel">{e.label}</span> {other}</li>
              })}
              {linksFor(n).length === 0 && <li className="muted">No connections recorded yet.</li>}
            </ul>
          </article>
        ))}
      </div>
    </div>
  )
}
