import React, { useEffect, useRef, useState } from 'react'

/**
 * Left panel: Subject → Topic → Graph, in the style of a chat app's history
 * list. Picking a topic opens that topic's graph; "All documents" shows the
 * whole graph. Purely presentational — every change goes through the
 * callbacks from useLibrary (src/lib/library.js).
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
  graph: <><circle cx="6" cy="7" r="2.2" /><circle cx="18" cy="8" r="2.2" /><circle cx="11" cy="17" r="2.2" /><path d="M8 8.5 10 15" /><path d="M16.3 9.8 12.6 15.3" /><path d="M8.2 7.2 15.8 7.8" /></>,
  all: <><rect x="4" y="4" width="7" height="7" rx="1.5" /><rect x="13" y="4" width="7" height="7" rx="1.5" /><rect x="4" y="13" width="7" height="7" rx="1.5" /><rect x="13" y="13" width="7" height="7" rx="1.5" /></>,
}

/** Single-line text field that commits on Enter / blur and cancels on Esc. */
function InlineInput({ initial = '', placeholder, label, onSubmit, onCancel }) {
  const [value, setValue] = useState(initial)
  const ref = useRef(null)
  const done = useRef(false)
  useEffect(() => { ref.current?.focus(); ref.current?.select() }, [])

  const commit = () => {
    if (done.current) return
    done.current = true
    const v = value.trim()
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

export default function SubjectSidebar({
  library, docs, open, onClose, onHome, user, onSignOut,
}) {
  const {
    subjects, activeTopicId, addSubject, renameSubject, toggleSubject, deleteSubject,
    addTopic, renameTopic, deleteTopic, setActiveTopic,
  } = library

  // what is currently being typed into: { kind: 'new-subject' | 'rename-subject'
  //   | 'new-topic' | 'rename-topic', id }
  const [editing, setEditing] = useState(null)
  const stop = () => setEditing(null)

  const liveIds = new Set(docs.map((d) => String(d.id)))
  const countIn = (t) => t.docIds.filter((id) => liveIds.has(String(id))).length

  const pick = (topicId) => { setActiveTopic(topicId); onClose?.() }

  const confirmDeleteSubject = (s) => {
    const n = s.topics.length
    const ok = window.confirm(
      `Delete the subject "${s.name}"${n ? ` and its ${n} topic${n > 1 ? 's' : ''}` : ''}?\n\n`
      + 'Your uploaded documents are kept — they just stop being grouped here.',
    )
    if (ok) deleteSubject(s.id)
  }
  const confirmDeleteTopic = (t) => {
    const ok = window.confirm(
      `Delete the topic "${t.name}"?\n\nIts documents are kept and still appear under All documents.`,
    )
    if (ok) deleteTopic(t.id)
  }

  return (
    <>
      <div className={`ws-side-backdrop${open ? ' on' : ''}`} onClick={onClose} aria-hidden="true" />
      <aside className={`ws-side${open ? ' open' : ''}`} aria-label="Subjects and topics">
        <button type="button" className="side-brand" onClick={onHome}>StudyGraph</button>

        <button type="button" className="side-new"
                onClick={() => setEditing({ kind: 'new-subject' })}>
          <Icon d={I.plus} /> New subject
        </button>

        <button type="button" className={`side-all${activeTopicId ? '' : ' on'}`}
                aria-current={activeTopicId ? undefined : 'page'}
                onClick={() => pick(null)}>
          <Icon d={I.all} />
          <span className="st-name">All documents</span>
          <span className="st-count">{docs.length}</span>
        </button>

        <div className="side-sec">Subjects</div>

        <nav className="side-tree">
          {editing?.kind === 'new-subject' && (
            <div className="st-editrow">
              <InlineInput
                placeholder="Subject name, e.g. FIT3143"
                label="New subject name"
                onSubmit={(name) => { addSubject(name); stop() }}
                onCancel={stop}
              />
            </div>
          )}

          {subjects.length === 0 && editing?.kind !== 'new-subject' && (
            <p className="side-hint">
              Group your documents by subject and topic. Start with <b>New subject</b>,
              then add a topic and upload into it.
            </p>
          )}

          <ul className="st-list">
            {subjects.map((s) => (
              <li key={s.id} className="st-subject">
                {editing?.kind === 'rename-subject' && editing.id === s.id ? (
                  <div className="st-editrow">
                    <InlineInput initial={s.name} label="Rename subject"
                                 onSubmit={(name) => { renameSubject(s.id, name); stop() }}
                                 onCancel={stop} />
                  </div>
                ) : (
                  <div className="st-row st-row-subject">
                    <button type="button" className="st-main" aria-expanded={s.open}
                            onClick={() => toggleSubject(s.id)}>
                      <span className={`st-chev${s.open ? ' open' : ''}`}><Icon d={I.chevron} size={12} /></span>
                      <span className="st-name" title={s.name}>{s.name}</span>
                    </button>
                    <span className="st-actions">
                      <button type="button" aria-label={`Add a topic to ${s.name}`} title="Add topic"
                              onClick={() => { toggleSubject(s.id, true); setEditing({ kind: 'new-topic', id: s.id }) }}>
                        <Icon d={I.plus} size={13} />
                      </button>
                      <button type="button" aria-label={`Rename ${s.name}`} title="Rename"
                              onClick={() => setEditing({ kind: 'rename-subject', id: s.id })}>
                        <Icon d={I.pencil} size={13} />
                      </button>
                      <button type="button" aria-label={`Delete ${s.name}`} title="Delete"
                              onClick={() => confirmDeleteSubject(s)}>
                        <Icon d={I.trash} size={13} />
                      </button>
                    </span>
                  </div>
                )}

                {s.open && (
                  <ul className="st-topics">
                    {s.topics.map((t) => (
                      <li key={t.id}>
                        {editing?.kind === 'rename-topic' && editing.id === t.id ? (
                          <div className="st-editrow st-editrow-topic">
                            <InlineInput initial={t.name} label="Rename topic"
                                         onSubmit={(name) => { renameTopic(t.id, name); stop() }}
                                         onCancel={stop} />
                          </div>
                        ) : (
                          <div className={`st-row st-row-topic${activeTopicId === t.id ? ' on' : ''}`}>
                            <button type="button" className="st-main"
                                    aria-current={activeTopicId === t.id ? 'page' : undefined}
                                    onClick={() => pick(t.id)}>
                              <Icon d={I.graph} size={13} />
                              <span className="st-name" title={t.name}>{t.name}</span>
                              <span className="st-count">{countIn(t)}</span>
                            </button>
                            <span className="st-actions">
                              <button type="button" aria-label={`Rename ${t.name}`} title="Rename"
                                      onClick={() => setEditing({ kind: 'rename-topic', id: t.id })}>
                                <Icon d={I.pencil} size={13} />
                              </button>
                              <button type="button" aria-label={`Delete ${t.name}`} title="Delete"
                                      onClick={() => confirmDeleteTopic(t)}>
                                <Icon d={I.trash} size={13} />
                              </button>
                            </span>
                          </div>
                        )}
                      </li>
                    ))}

                    {editing?.kind === 'new-topic' && editing.id === s.id ? (
                      <li className="st-editrow st-editrow-topic">
                        <InlineInput placeholder="Topic name, e.g. Week 2 — Threads"
                                     label={`New topic in ${s.name}`}
                                     onSubmit={(name) => { addTopic(s.id, name); stop(); onClose?.() }}
                                     onCancel={stop} />
                      </li>
                    ) : (
                      <li>
                        <button type="button" className="st-addtopic"
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
