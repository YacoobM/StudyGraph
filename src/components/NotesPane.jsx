import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { listNotes, createNote, getSection, setSectionDone, validDay } from '../data/notes.js'
import { saveNoteQuizProgress } from '../services/api.js'
import QuizRunner from './QuizRunner.jsx'
import './NotesPane.css'

/* --------------------------------------------------------------- helpers */

/** A paragraph: a plain string, or strings mixed with bold and key marks (no HTML). */
function Rich({ parts }) {
  const list = typeof parts === 'string' ? [parts] : parts || []
  return (
    <>
      {list.map((p, i) => {
        if (typeof p === 'string') return <Fragment key={i}>{p}</Fragment>
        if (p.key) {
          return <mark className="nt-key" key={i}>{p.bold ? <b>{p.text}</b> : p.text}</mark>
        }
        if (p.bold) return <b key={i}>{p.text}</b>
        return <Fragment key={i}>{p.text}</Fragment>
      })}
    </>
  )
}

const Icon = ({ d, size = 16 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9"
       strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
)
const I = {
  back: <path d="M19 12H6M11 6l-6 6 6 6" />,
  next: <path d="M5 12h13M13 6l6 6-6 6" />,
  check: <path d="m5 12.5 4.2 4.2L19 7" />,
  plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
  contents: <><path d="M4 6h16" /><path d="M4 12h10" /><path d="M4 18h13" /></>,
  sources: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" /></>,
  note: <><path d="M5 4h11l3 3v13H5z" /><path d="M9 10h6M9 14h6M9 18h3" /></>,
}

const pctOf = (done, total) => (total ? Math.round((done / total) * 100) : 0)
const kindLabel = (n) => (n.kind === 'plan' ? `${n.days}-day plan` : 'Full notes')
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`

function Progress({ done, total, label = true }) {
  const pct = pctOf(done, total)
  return (
    <div className="nt-progress">
      <div className="nt-progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}
           aria-label={`${done} of ${total} sections complete`}>
        <span style={{ width: `${pct}%` }} />
      </div>
      {label && <span className="nt-progress-txt">{done} of {total} done</span>}
    </div>
  )
}

/** The contents rail: a plan grouped by day, a full note as one run. */
function groupsFor(note) {
  const numbered = note.sections.map((s, index) => ({ ...s, index }))
  if (note.kind !== 'plan') return [{ label: null, items: numbered }]
  const out = []
  numbered.forEach((s) => {
    const label = `Day ${s.day}`
    if (!out.length || out[out.length - 1].label !== label) out.push({ label, day: s.day, items: [] })
    out[out.length - 1].items.push(s)
  })
  return out
}

const wide = () => typeof window === 'undefined' || window.matchMedia('(min-width: 1100px)').matches

/* ============================================================ list view */

function ListView({ notes, loading, error, planning, canCreate, onOpen, onCreate, onRegenerate }) {
  return (
    <div className="pane pane-scroll nt-view">
      <div className="nt-col">
        <header className="nt-head">
          <div>
            <h2 className="nt-title">Revision notes</h2>
            <p className="nt-intro">Written from the documents in this graph. Split them into days for a study plan.</p>
          </div>
          <button className="nt-btn primary" onClick={onCreate}>
            <Icon d={I.plus} /> New notes
          </button>
        </header>

        {error && <div className="notice-error" role="alert">{error}</div>}

        {loading ? (
          <div role="status" aria-label="Loading notes">
            <div className="skel-card"><span className="skel w40" /><span className="skel w70" /><span className="skel w55" /></div>
            <div className="skel-card"><span className="skel w40" /><span className="skel w70" /></div>
          </div>
        ) : notes.length === 0 ? (
          <div className="nt-empty">
            <span className="nt-empty-icon"><Icon d={I.note} size={26} /></span>
            <h3>No notes yet</h3>
            <p>{canCreate
              ? 'Pick the documents to cover and StudyGraph writes sectioned notes, each with a short quiz.'
              : 'Add documents to this graph first. Notes are written once they have been read.'}</p>
            {canCreate && <button className="nt-btn primary" onClick={onCreate}>Create notes</button>}
          </div>
        ) : (
          <ul className="nt-cards">
            {notes.map((n) => (
              <li className={`nt-card${n.status === 'stale' ? ' stale' : ''}`} key={n.id}>
                <button className="nt-card-open" onClick={() => onOpen(n.id)}>
                  <span className="nt-card-top">
                    <span className={`nt-tag ${n.kind}`}>{kindLabel(n)}</span>
                    {n.status === 'stale' && <span className="nt-tag stale">Older graph version</span>}
                    {n.created && <span className="nt-card-date">{n.created}</span>}
                  </span>
                  <span className="nt-card-name">{n.title}</span>
                  <span className="nt-card-sub">{plural(n.docCount, 'document')} · {plural(n.total, 'section')}</span>
                  {n.kind === 'plan' && (
                    <span className="nt-days-row" aria-hidden="true">
                      {groupsFor(n).map((g) => {
                        const done = g.items.every((s) => s.done)
                        return <span key={g.label} className={`nt-day-dot${done ? ' done' : ''}`}>{g.day}</span>
                      })}
                    </span>
                  )}
                  <Progress done={n.done} total={n.total} />
                </button>
                {n.status === 'stale' && (
                  <button className="nt-btn small" onClick={() => onRegenerate(n)} disabled={planning}>
                    {planning ? 'Regenerating…' : 'Regenerate'}
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

/* ========================================================== create view */

function CreateView({ readyDocs, planning, error, onCancel, onGenerate }) {
  const [picked, setPicked] = useState(() => readyDocs.map((d) => d.id))
  const [split, setSplit] = useState(false)
  const [days, setDays] = useState(3)
  const [flagged, setFlagged] = useState(false)
  const docBlock = useRef(null)
  const allPicked = picked.length > 0 && picked.length === readyDocs.length

  function toggle(id) {
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))
    setFlagged(false)
  }

  // Always clickable: with nothing picked it points at the block that needs an answer.
  function generate() {
    if (!picked.length) {
      setFlagged(true)
      docBlock.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      return
    }
    onGenerate({ docs: readyDocs.filter((d) => picked.includes(d.id)), split, days })
  }

  return (
    <div className="pane pane-scroll nt-view">
      <div className="nt-col">
        <button className="nt-back" onClick={onCancel} disabled={planning}>
          <Icon d={I.back} /> Notes
        </button>
        <h2 className="nt-title">New notes</h2>

        <fieldset className={`nt-block${flagged && !picked.length ? ' needs' : ''}`} ref={docBlock}>
          <div className="nt-label-row">
            <legend className="nt-label">Documents to cover</legend>
            {readyDocs.length > 0 && (
              <button className="nt-link" onClick={() => { setPicked(allPicked ? [] : readyDocs.map((d) => d.id)); setFlagged(false) }}>
                {allPicked ? 'Clear all' : 'Select all'}
              </button>
            )}
          </div>
          {readyDocs.length === 0 ? (
            <p className="nt-muted">No documents are ready yet. They appear here once StudyGraph has read them.</p>
          ) : (
            <div className="nt-pick-list">
              {readyDocs.map((d) => {
                const on = picked.includes(d.id)
                return (
                  <label className={`nt-pick${on ? ' on' : ''}`} key={d.id}>
                    <input type="checkbox" checked={on} onChange={() => toggle(d.id)} />
                    <span className="nt-cbx" aria-hidden="true"><Icon d={I.check} size={13} /></span>
                    <span className="nt-pick-main">
                      <span className="nt-pick-name">{d.name || d.filename}</span>
                      <span className="nt-pick-sub">{plural(d.nodes ?? d.concepts ?? 0, 'concept')}{d.week ? ` · ${d.week}` : ''}</span>
                    </span>
                  </label>
                )
              })}
            </div>
          )}
          {flagged && !picked.length && <p className="nt-field-msg" role="alert">Pick at least one document.</p>}
        </fieldset>

        <div className="nt-block">
          <label className="nt-switch-row">
            <input type="checkbox" checked={split} onChange={() => setSplit((s) => !s)} />
            <span className="nt-switch" aria-hidden="true" />
            <span>
              <span className="nt-switch-title">Split into days</span>
              <span className="nt-switch-desc">Spreads the sections over a number of days, one block per day.</span>
            </span>
          </label>
          {split && (
            <div className="nt-days">
              <div className="nt-stepper" role="group" aria-label="Number of days">
                <button onClick={() => setDays((d) => Math.max(1, d - 1))} aria-label="Fewer days" disabled={days <= 1}>−</button>
                <output className="val" aria-live="polite">{days}</output>
                <button onClick={() => setDays((d) => Math.min(10, d + 1))} aria-label="More days" disabled={days >= 10}>+</button>
              </div>
              <p className="nt-muted">days. With fewer sections than days, the plan is shortened so no day is empty.</p>
            </div>
          )}
        </div>

        <div className="nt-actions">
          <div className="nt-summary">
            {picked.length ? <><b>{picked.length}</b> {picked.length > 1 ? 'documents' : 'document'}{split && <> over up to <b>{days}</b> days</>}</> : 'Nothing selected'}
          </div>
          <button className="nt-btn" onClick={onCancel} disabled={planning}>Cancel</button>
          <button className="nt-btn primary" onClick={generate} disabled={planning} aria-busy={planning}>
            {planning ? <><span className="nt-spinner" aria-hidden="true" />Planning…</> : 'Generate notes'}
          </button>
        </div>
        {planning && (
          <p className="nt-working" role="status">Grouping the graph's concepts into sections. This takes a few seconds.</p>
        )}
        {error && <div className="notice-error" role="alert">{error}</div>}
      </div>
    </div>
  )
}

/* ========================================================== detail view */

function SectionBody({ section }) {
  return (
    <div className="nt-body">
      {section.body?.map((p, i) => (
        <div className="nt-para" key={i}>
          {p.heading && <h3 className="nt-para-head">{p.heading}</h3>}
          <p><Rich parts={p.parts} /></p>
        </div>
      ))}

      {section.example && (
        <aside className="nt-eg">
          <div className="nt-block-label">{section.example.title || 'Example'}</div>
          {section.example.paragraphs.map((p, i) => <p key={i}><Rich parts={p} /></p>)}
        </aside>
      )}

      {section.table && (
        <div className="nt-tbl-wrap">
          <table className="nt-tbl">
            <thead><tr>{section.table.cols.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
            <tbody>
              {section.table.rows.map((row, i) => (
                <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {section.terms && (
        <section className="nt-terms">
          <div className="nt-block-label">Key terms</div>
          <dl>
            {section.terms.map(([term, def], i) => (
              <div className="nt-term" key={i}><dt>{term}</dt><dd>{def}</dd></div>
            ))}
          </dl>
        </section>
      )}

      {section.relations?.length > 0 && (
        <section className="nt-rels">
          <div className="nt-block-label">How these connect</div>
          {section.relations.map(([from, to, summary], i) => (
            <div className="nt-rel" key={i}>
              <div className="nt-rel-pair"><span>{from}</span><span aria-hidden="true">→</span><span>{to}</span></div>
              <p>{summary}</p>
            </div>
          ))}
        </section>
      )}
    </div>
  )
}

/** The section's quiz, taken inline below the footer. Passing marks the section complete. */
function SectionQuiz({ graphId, section, onPassed, onClose }) {
  const [saveError, setSaveError] = useState('')
  const total = section.quiz.length
  const need = section.passMark ?? total

  function finish({ answers, correct, total: count }) {
    setSaveError('')
    const byIndex = Object.fromEntries((answers || []).map((answer, i) => [i, answer]))
    saveNoteQuizProgress(graphId, section.id, answers?.length || 0, byIndex, correct, count, correct * 10, true)
      .catch((e) => setSaveError(`Your score was not saved: ${e.message}`))
    if (correct >= need) onPassed()
  }

  return (
    <div className="nt-quiz">
      <QuizRunner
        inline
        title="Close quiz"
        questions={section.quiz}
        onExit={onClose}
        onFinish={finish}
        need={need}
        passTitle="Section complete"
        passText="The section is marked complete in the contents."
        failText={`Get ${need} of ${total} right to complete the section. Reread the section and try again.`}
        exitLabel="Close quiz"
      />
      {saveError && <div className="notice-error" role="alert">{saveError}</div>}
    </div>
  )
}

function ContextPanel({ section, open, onClose }) {
  return (
    <aside className={`nt-side nt-ctx${open ? '' : ' off'}`} inert={open ? undefined : ''} aria-label="Section context">
      <div className="nt-side-inner">
        <div className="nt-side-bar">
          <span className="nt-side-label">On this page</span>
          <button className="nt-side-x" onClick={onClose} aria-label="Close context">×</button>
        </div>
        {section?.concepts?.length > 0 && (
          <div className="nt-ctx-block">
            <div className="nt-side-label">Concepts</div>
            <div className="nt-concepts">
              {section.concepts.map((c) => <span className="nt-concept" key={c}>{c}</span>)}
            </div>
          </div>
        )}
        <div className="nt-ctx-block">
          <div className="nt-side-label">Sources</div>
          {section?.sources?.length ? section.sources.map((s, i) => (
            <div className="nt-src" key={i}><Icon d={I.sources} size={15} /><span>{s.file}</span></div>
          )) : <p className="nt-muted">{section ? 'No source documents recorded for this section.' : 'Loading…'}</p>}
        </div>
      </div>
    </aside>
  )
}

function DetailView({ graphId, note, index, section, writing, writeError, coolLeft, planning, saveError,
                      onRetry, onBack, onOpenSection, onToggleDone, onRegenerate }) {
  const [railOn, setRailOn] = useState(wide)
  const [ctxOn, setCtxOn] = useState(wide)
  const [quizOpen, setQuizOpen] = useState(false)
  const [readPct, setReadPct] = useState(0)
  const center = useRef(null)
  const quizBox = useRef(null)

  // A new section starts at the top with its quiz closed.
  useEffect(() => {
    if (center.current) center.current.scrollTop = 0
    setReadPct(0)
    setQuizOpen(false)
  }, [note.id, index])

  useEffect(() => {
    if (quizOpen) quizBox.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [quizOpen])

  function onScroll(e) {
    const el = e.currentTarget
    const max = el.scrollHeight - el.clientHeight
    setReadPct(max > 0 ? Math.min(100, Math.round((el.scrollTop / max) * 100)) : 100)
  }

  const groups = useMemo(() => groupsFor(note), [note])
  const stub = note.sections[index]
  const day = validDay(section?.day ?? stub?.day)
  const eyebrow = note.kind === 'plan' && day
    ? `Day ${day} of ${note.days} · Section ${index + 1} of ${note.total}`
    : `Section ${index + 1} of ${note.total}`
  const done = Boolean(stub?.done)
  const hasQuiz = Boolean(section?.quiz && section.quizId)
  const closeOnSmall = () => { if (!wide()) setRailOn(false) }

  return (
    <div className="pane nt-detail nt-view">
      <div className="nt-detail-top">
        <button className="nt-back" onClick={onBack}><Icon d={I.back} /> Notes</button>
        <div className="nt-detail-title" title={note.title}>
          {note.title}
          {note.status === 'stale' && <span className="nt-tag stale">Older graph version</span>}
        </div>
        <div className="nt-detail-actions">
          {note.status === 'stale' && (
            <button className="nt-btn small primary" onClick={onRegenerate} disabled={planning}>
              {planning ? 'Regenerating…' : 'Regenerate'}
            </button>
          )}
          <button className={`nt-toggle${railOn ? ' on' : ''}`} onClick={() => setRailOn((v) => !v)} aria-pressed={railOn}>
            <Icon d={I.contents} size={15} /><span className="lbl">Contents</span>
          </button>
          <button className={`nt-toggle${ctxOn ? ' on' : ''}`} onClick={() => setCtxOn((v) => !v)} aria-pressed={ctxOn}>
            <Icon d={I.sources} size={15} /><span className="lbl">Sources</span>
          </button>
        </div>
        <div className="nt-read-progress" aria-hidden="true"><span style={{ width: `${readPct}%` }} /></div>
      </div>

      <div className={`nt-shell${railOn ? '' : ' rail-off'}${ctxOn ? '' : ' ctx-off'}`}>
        <nav className={`nt-side nt-rail${railOn ? '' : ' off'}`} inert={railOn ? undefined : ''} aria-label="Contents">
          <div className="nt-side-inner">
            <div className="nt-side-bar">
              <span className="nt-side-label">Contents</span>
              <button className="nt-side-x" onClick={() => setRailOn(false)} aria-label="Close contents">×</button>
            </div>
            <div className="nt-rail-meta">{kindLabel(note)} · {plural(note.docCount, 'document')}</div>
            <Progress done={note.done} total={note.total} />

            {groups.map((g) => {
              const groupDone = g.items.filter((s) => s.done).length
              return (
                <div className={`nt-group${g.label ? ' day' : ''}`} key={g.label ?? 'all'}>
                  {g.label && (
                    <div className="nt-group-head">
                      <span>{g.label}</span>
                      <span className="nt-group-count">{groupDone}/{g.items.length}</span>
                    </div>
                  )}
                  <ol className="nt-rail-list">
                    {g.items.map((s) => (
                      <li key={s.id} className={`nt-rail-item${s.index === index ? ' active' : ''}${s.done ? ' done' : ''}`}>
                        <button className="nt-mark" onClick={() => onToggleDone(s.index, !s.done)}
                                aria-label={s.done ? `Mark ${s.title} not done` : `Mark ${s.title} done`} aria-pressed={s.done}>
                          <Icon d={I.check} size={12} />
                        </button>
                        <button className="nt-rail-title" onClick={() => { onOpenSection(s.index); closeOnSmall() }}
                                aria-current={s.index === index ? 'true' : undefined}>
                          {s.title}
                        </button>
                      </li>
                    ))}
                  </ol>
                </div>
              )
            })}
          </div>
        </nav>

        <div className="nt-center" ref={center} onScroll={onScroll}>
          <article className="nt-center-inner" key={`${note.id}-${index}`}>
            <header className="nt-sec-head">
              <div className="nt-eyebrow">{eyebrow}</div>
              <h2 className="nt-sec-title">{section?.title || stub?.title}</h2>
              {section?.lead && <p className="nt-lead"><Rich parts={section.lead} /></p>}
            </header>

            {writing && (
              <div className="nt-writing" role="status">
                <div className="nt-writing-head"><span className="nt-spinner" aria-hidden="true" /><b>Writing this section</b></div>
                <p>Reading the source passages behind these concepts and drafting the notes. The first time takes twenty to thirty seconds. After that it opens instantly.</p>
                <span className="skel w90" /><span className="skel w70" /><span className="skel w90" /><span className="skel w55" />
              </div>
            )}

            {writeError && !writing && (
              <div className="notice-error" role="alert">
                <span>{writeError}</span>
                {coolLeft > 0 && <span className="nt-cool"> Asking again now would be refused for {coolLeft}s.</span>}
                <button className="nt-btn small" onClick={onRetry} disabled={coolLeft > 0}>
                  {coolLeft > 0 ? `Try again in ${coolLeft}s` : 'Try again'}
                </button>
              </div>
            )}

            {section && <SectionBody section={section} />}

            <footer className="nt-foot">
              <button className={`nt-done-btn${done ? ' on' : ''}`} onClick={() => onToggleDone(index, !done)}
                      disabled={!section} aria-pressed={done}>
                <span className="nt-done-check"><Icon d={I.check} size={14} /></span>
                {done ? 'Completed' : 'Mark complete'}
              </button>
              <div className="nt-foot-right">
                {hasQuiz && (
                  <button className={`nt-btn${quizOpen ? '' : ' primary'}`} onClick={() => setQuizOpen((v) => !v)} aria-expanded={quizOpen}>
                    {quizOpen ? 'Close quiz' : 'Quiz this section'}
                  </button>
                )}
                {index > 0 && (
                  <button className="nt-btn" onClick={() => onOpenSection(index - 1)} aria-label="Previous section">
                    <Icon d={I.back} size={15} />
                  </button>
                )}
                {index < note.total - 1 && (
                  <button className="nt-btn" onClick={() => onOpenSection(index + 1)}>
                    Next <Icon d={I.next} size={15} />
                  </button>
                )}
              </div>
            </footer>
            {saveError && <div className="notice-error" role="alert">{saveError}</div>}

            {hasQuiz && quizOpen && (
              <div ref={quizBox}>
                <SectionQuiz
                  key={section.id}
                  graphId={graphId}
                  section={section}
                  onClose={() => setQuizOpen(false)}
                  onPassed={() => { if (!done) onToggleDone(index, true) }}
                />
              </div>
            )}
          </article>
        </div>

        <ContextPanel section={section} open={ctxOn} onClose={() => setCtxOn(false)} />
      </div>
    </div>
  )
}

/* ================================================================= shell */

export default function NotesPane({ readyDocs = [], graphId }) {
  const [notes, setNotes] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [view, setView] = useState('list')
  const [openId, setOpenId] = useState(null)
  const [index, setIndex] = useState(0)

  // Loaded, not derived: fetching the body writes it, slowly the first time.
  const [section, setSection] = useState(null)
  const [writing, setWriting] = useState(false)
  const [writeError, setWriteError] = useState('')
  const [planning, setPlanning] = useState(false)
  // Bumped by Try again: setting the same index would not re-run the effect.
  const [retryTick, setRetryTick] = useState(0)
  const [coolUntil, setCoolUntil] = useState(0)
  const [, tick] = useState(0)

  const note = notes.find((n) => n.id === openId)

  useEffect(() => {
    let live = true
    if (!graphId) {
      setNotes([])
      setLoading(false)
      return () => { live = false }
    }
    setLoading(true)
    listNotes(graphId)
      .then((rows) => live && setNotes(rows))
      .catch((e) => live && setError(e.message))
      .finally(() => live && setLoading(false))
    return () => { live = false }
  }, [graphId])

  // One fetch per section opened; `live` stops a late answer landing on another section.
  useEffect(() => {
    if (view !== 'detail' || !note) return undefined
    const ac = new AbortController()
    let live = true
    setSection(null)
    setWriteError('')
    setSaveError('')
    setCoolUntil(0)
    setWriting(true)
    getSection(graphId, note.id, index, { signal: ac.signal })
      .then((s) => live && setSection(s))
      .catch((e) => {
        if (!live || e?.name === 'AbortError') return
        setWriteError(e.message)
        if (e.cooldownSeconds) setCoolUntil(Date.now() + e.cooldownSeconds * 1000)
      })
      .finally(() => live && setWriting(false))
    // The server keeps writing after an abort; its own guard stops a second write starting.
    return () => { live = false; ac.abort() }
  }, [graphId, view, openId, index, retryTick]) // eslint-disable-line react-hooks/exhaustive-deps

  function openDetail(id) {
    setOpenId(id)
    setIndex(0)
    setView('detail')
  }

  function applyDone(i, done) {
    setNotes((rows) => rows.map((n) => {
      if (n.id !== openId) return n
      const sections = n.sections.map((s, k) => (k === i ? { ...s, done } : s))
      return { ...n, sections, done: sections.filter((s) => s.done).length }
    }))
    if (i === index) setSection((s) => (s ? { ...s, done } : s))
  }

  // Shown straight away, saved in the background, put back if the save fails.
  async function toggleDone(i, done) {
    setSaveError('')
    applyDone(i, done)
    try {
      await setSectionDone(graphId, openId, i, done)
    } catch (e) {
      applyDone(i, !done)
      setSaveError(`Could not save your progress: ${e.message}`)
    }
  }

  useEffect(() => {
    if (!coolUntil) return undefined
    const t = setInterval(() => tick((n) => n + 1), 1000)
    return () => clearInterval(t)
  }, [coolUntil])
  const coolLeft = coolUntil ? Math.max(0, Math.ceil((coolUntil - Date.now()) / 1000)) : 0

  async function generate(opts) {
    setPlanning(true)
    setError('')
    try {
      const id = await createNote(graphId, opts)
      setNotes(await listNotes(graphId))
      if (id) openDetail(id)
    } catch (e) {
      setError(e.message)
    } finally {
      setPlanning(false)
    }
  }

  async function regenerate(noteToRegenerate) {
    const currentIds = new Set(readyDocs.map((doc) => String(doc.id)))
    const remainingIds = (noteToRegenerate.documentIds || []).filter((id) => currentIds.has(String(id)))
    if (!remainingIds.length) {
      setError('There are no remaining documents to regenerate these notes from.')
      return
    }
    await generate({
      docs: remainingIds.map((id) => ({ id })),
      split: noteToRegenerate.kind === 'plan',
      days: noteToRegenerate.days,
    })
  }

  if (view === 'create') {
    return <CreateView readyDocs={readyDocs} planning={planning} error={error} onCancel={() => setView('list')} onGenerate={generate} />
  }

  if (view === 'detail' && note) {
    return (
      <DetailView
        graphId={graphId}
        note={note}
        index={index}
        section={section}
        writing={writing}
        writeError={writeError}
        coolLeft={coolLeft}
        planning={planning}
        saveError={saveError}
        onRetry={() => setRetryTick((n) => n + 1)}
        onBack={() => setView('list')}
        onOpenSection={setIndex}
        onToggleDone={toggleDone}
        onRegenerate={() => regenerate(note)}
      />
    )
  }

  return (
    <ListView
      notes={notes}
      loading={loading}
      error={error}
      planning={planning}
      canCreate={readyDocs.length > 0}
      onOpen={openDetail}
      onCreate={() => setView('create')}
      onRegenerate={regenerate}
    />
  )
}
