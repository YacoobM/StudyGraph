import React, { useEffect, useRef, useState } from 'react'

/**
 * Left panel: Subject → Topic → Graph, in the style of a chat app's history
 * list. A topic can hold any number of graphs; picking a graph opens it.
 * "All documents" shows the whole graph. Purely presentational — every change
 * goes through the callbacks from useLibrary (src/lib/library.js).
 */

function Icon({ d, size = 14 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {d}
    </svg>
  )
}
const I = {
  chevron: <path d="m9 6 6 6-6 6" />,
  plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  pencil: <><path d="M4 20h4L19 9l-4-4L4 16v4Z" /><path d="m13.5 6.5 4 4" /></>,
  trash: <><path d="M4 7h16" /><path d="M9 7V4h6v3" /><path d="M6 7l1 13h10l1-13" /></>,
  folder: <path d="M3.5 7.5a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z" />,
  graph: <><circle cx="6" cy="7" r="2.2" /><circle cx="18" cy="8" r="2.2" /><circle cx="11" cy="17" r="2.2" /><path d="M8 8.5 10 15" /><path d="M16.3 9.8 12.6 15.3" /><path d="M8.2 7.2 15.8 7.8" /></>,
  all: <><rect x="4" y="4" width="7" height="7" rx="1.5" /><rect x="13" y="4" width="7" height="7" rx="1.5" /><rect x="4" y="13" width="7" height="7" rx="1.5" /><rect x="13" y="13" width="7" height="7" rx="1.5" /></>,
}

/**
 * Single-line text field that commits on Enter / blur and cancels on Esc.
 * With `fallback`, an empty field commits the fallback name instead of
 * cancelling (used for "Graph 2" and the like).
 */
function InlineInput({ initial = '', placeholder, label, fallback, onSubmit, onCancel }) {
  const [value, setValue] = useState(initial)
  const ref = useRef(null)
  const done = useRef(false)
  useEffect(() => { ref.current?.focus(); ref.current?.select() }, [])

  const commit = () => {
    if (done.current) return
    done.current = true
    const v = value.trim() || fallback || ''
    if (v) onSubmit(v)
    else onCancel()
  }
  return (
    <input
      ref={ref}
      className="st-input"
      value={value}
      placeholder={placeholder}
      aria-label={label}
      maxLength={80}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit() }
        if (e.key === 'Escape') { e.preventDefault(); done.current = true; onCancel() }
      }}
    />
  )
}

/** A row's hover actions: [label, icon, onClick]. */
function Actions({ items }) {
  return (
    <span className="st-actions">
      {items.map(([label, icon, onClick]) => (
        <button key={label} type="button" aria-label={label} title={label.split(' ')[0]} onClick={onClick}>
          <Icon d={icon} size={13} />
        </button>
      ))}
    </span>
  )
}

export default function SubjectSidebar({
  library, docs, open, onClose, onHome, user, onSignOut,
}) {
  const {
    subjects, activeGraphId,
    addSubject, renameSubject, toggleSubject, deleteSubject,
    addTopic, renameTopic, toggleTopic, deleteTopic,
    addGraph, renameGraph, deleteGraph, setActiveGraph,
  } = library

  // What is being typed into: { kind, id } where kind is one of
  // new-subject | rename-subject | new-topic | rename-topic | new-graph | rename-graph
  const [editing, setEditing] = useState(null)
  const stop = () => setEditing(null)
  const is = (kind, id) => editing?.kind === kind && (id === undefined || editing.id === id)

  const liveIds = new Set(docs.map((d) => String(d.id)))
  const countIn = (g) => g.docIds.filter((id) => liveIds.has(String(id))).length

  const pick = (graphId) => { setActiveGraph(graphId); onClose?.() }

  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`
  const confirmDelete = (kind, name, detail) => window.confirm(
    `Delete the ${kind} "${name}"${detail ? ` and ${detail}` : ''}?\n\n`
    + 'Your uploaded documents are kept — they still appear under All documents.',
  )

  return (
    <>
      <div className={`ws-side-backdrop${open ? ' on' : ''}`} onClick={onClose} aria-hidden="true" />
      <aside className={`ws-side${open ? ' open' : ''}`} aria-label="Subjects, topics and graphs">
        <button type="button" className="side-brand" onClick={onHome}>StudyGraph</button>

        <button type="button" className="side-new" onClick={() => setEditing({ kind: 'new-subject' })}>
          <Icon d={I.plus} /> New subject
        </button>

        <button type="button" className={`side-all${activeGraphId ? '' : ' on'}`}
                aria-current={activeGraphId ? undefined : 'page'}
                onClick={() => pick(null)}>
          <Icon d={I.all} />
          <span className="st-name">All documents</span>
          <span className="st-count">{docs.length}</span>
        </button>

        <div className="side-sec">Subjects</div>

        <nav className="side-tree">
          {is('new-subject') && (
            <div className="st-editrow">
              <InlineInput placeholder="Subject name, e.g. FIT3143" label="New subject name"
                           onSubmit={(name) => { addSubject(name); stop() }} onCancel={stop} />
            </div>
          )}

          {subjects.length === 0 && !is('new-subject') && (
            <p className="side-hint">
              Organise your work as <b>Subject → Topic → Graph</b>. Start with <b>New subject</b>,
              add a topic, then create as many graphs in it as you need.
            </p>
          )}

          <ul className="st-list">
            {subjects.map((s) => (
              <li key={s.id} className="st-subject">
                {is('rename-subject', s.id) ? (
                  <div className="st-editrow">
                    <InlineInput initial={s.name} label="Rename subject"
                                 onSubmit={(name) => { renameSubject(s.id, name); stop() }} onCancel={stop} />
                  </div>
                ) : (
                  <div className="st-row st-row-subject">
                    <button type="button" className="st-main" aria-expanded={s.open}
                            onClick={() => toggleSubject(s.id)}>
                      <span className={`st-chev${s.open ? ' open' : ''}`}><Icon d={I.chevron} size={12} /></span>
                      <span className="st-name" title={s.name}>{s.name}</span>
                    </button>
                    <Actions items={[
                      [`Add a topic to ${s.name}`, I.plus, () => { toggleSubject(s.id, true); setEditing({ kind: 'new-topic', id: s.id }) }],
                      [`Rename ${s.name}`, I.pencil, () => setEditing({ kind: 'rename-subject', id: s.id })],
                      [`Delete ${s.name}`, I.trash, () => {
                        if (confirmDelete('subject', s.name, s.topics.length ? `its ${plural(s.topics.length, 'topic')}` : '')) deleteSubject(s.id)
                      }],
                    ]} />
                  </div>
                )}

                {s.open && (
                  <ul className="st-topics">
                    {s.topics.map((t) => (
                      <li key={t.id} className="st-topic">
                        {is('rename-topic', t.id) ? (
                          <div className="st-editrow st-lvl-1">
                            <InlineInput initial={t.name} label="Rename topic"
                                         onSubmit={(name) => { renameTopic(t.id, name); stop() }} onCancel={stop} />
                          </div>
                        ) : (
                          <div className="st-row st-row-topic">
                            <button type="button" className="st-main" aria-expanded={t.open}
                                    onClick={() => toggleTopic(t.id)}>
                              <span className={`st-chev${t.open ? ' open' : ''}`}><Icon d={I.chevron} size={11} /></span>
                              <Icon d={I.folder} size={13} />
                              <span className="st-name" title={t.name}>{t.name}</span>
                              <span className="st-count" title={plural(t.graphs.length, 'graph')}>{t.graphs.length}</span>
                            </button>
                            <Actions items={[
                              [`New graph in ${t.name}`, I.plus, () => { toggleTopic(t.id, true); setEditing({ kind: 'new-graph', id: t.id }) }],
                              [`Rename ${t.name}`, I.pencil, () => setEditing({ kind: 'rename-topic', id: t.id })],
                              [`Delete ${t.name}`, I.trash, () => {
                                if (confirmDelete('topic', t.name, t.graphs.length ? `its ${plural(t.graphs.length, 'graph')}` : '')) deleteTopic(t.id)
                              }],
                            ]} />
                          </div>
                        )}

                        {t.open && (
                          <ul className="st-graphs">
                            {t.graphs.map((g) => (
                              <li key={g.id}>
                                {is('rename-graph', g.id) ? (
                                  <div className="st-editrow st-lvl-2">
                                    <InlineInput initial={g.name} label="Rename graph"
                                                 onSubmit={(name) => { renameGraph(g.id, name); stop() }} onCancel={stop} />
                                  </div>
                                ) : (
                                  <div className={`st-row st-row-graph${activeGraphId === g.id ? ' on' : ''}`}>
                                    <button type="button" className="st-main"
                                            aria-current={activeGraphId === g.id ? 'page' : undefined}
                                            onClick={() => pick(g.id)}>
                                      <Icon d={I.graph} size={13} />
                                      <span className="st-name" title={g.name}>{g.name}</span>
                                      <span className="st-count" title={plural(countIn(g), 'document')}>{countIn(g)}</span>
                                    </button>
                                    <Actions items={[
                                      [`Rename ${g.name}`, I.pencil, () => setEditing({ kind: 'rename-graph', id: g.id })],
                                      [`Delete ${g.name}`, I.trash, () => { if (confirmDelete('graph', g.name)) deleteGraph(g.id) }],
                                    ]} />
                                  </div>
                                )}
                              </li>
                            ))}

                            {is('new-graph', t.id) ? (
                              <li className="st-editrow st-lvl-2">
                                <InlineInput placeholder={`Graph ${t.graphs.length + 1}`}
                                             fallback={`Graph ${t.graphs.length + 1}`}
                                             label={`New graph in ${t.name}`}
                                             onSubmit={(name) => { addGraph(t.id, name); stop(); onClose?.() }}
                                             onCancel={stop} />
                              </li>
                            ) : (
                              <li>
                                <button type="button" className="st-add st-lvl-2"
                                        onClick={() => setEditing({ kind: 'new-graph', id: t.id })}>
                                  <Icon d={I.plus} size={12} /> New graph
                                </button>
                              </li>
                            )}
                          </ul>
                        )}
                      </li>
                    ))}

                    {is('new-topic', s.id) ? (
                      <li className="st-editrow st-lvl-1">
                        <InlineInput placeholder="Topic name, e.g. Week 2 — Threads"
                                     label={`New topic in ${s.name}`}
                                     onSubmit={(name) => {
                                       const id = addTopic(s.id, name)
                                       // go straight on to naming its first graph
                                       setEditing({ kind: 'new-graph', id })
                                     }}
                                     onCancel={stop} />
                      </li>
                    ) : (
                      <li>
                        <button type="button" className="st-add st-lvl-1"
                                onClick={() => setEditing({ kind: 'new-topic', id: s.id })}>
                          <Icon d={I.plus} size={12} /> Add topic
                        </button>
                      </li>
                    )}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </nav>

        <div className="side-foot">
          <div className="side-user">
            <div className="su-avatar">{(user?.name || user?.email || '··').slice(0, 2).toUpperCase()}</div>
            <div className="su-text">
              <div className="su-name">{user?.name || user?.user_metadata?.full_name || 'Signed in'}</div>
              <div className="su-mail">{user?.email || ''}</div>
            </div>
          </div>
          <button type="button" className="side-signout" onClick={onSignOut}>Sign out</button>
        </div>
      </aside>
    </>
  )
}
