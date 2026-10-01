import React, { useEffect, useRef, useState } from 'react'

/**
 * In-page upload: a drop zone that takes several files at once, the queue
 * that shows each file's progress, and the dialog that holds both over the
 * graph. The actual uploading is done by Workspace (one file at a time,
 * because the backend runs a single extraction pipeline).
 */

export const ACCEPT = '.pdf,.docx,.pptx,.txt,.md'

export function UploadDrop({ onFiles, tone = 'light', title, hint, compact = false }) {
  const [over, setOver] = useState(false)
  const input = useRef(null)
  const take = (list) => {
    const files = Array.from(list || [])
    if (files.length) onFiles(files)
  }
  return (
    <div
      className={`up-drop up-${tone}${over ? ' over' : ''}${compact ? ' compact' : ''}`}
      onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); setOver(true) }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); e.stopPropagation(); setOver(false); take(e.dataTransfer?.files) }}
    >
      <svg className="up-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
           strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M12 16V4" /><path d="m7 9 5-5 5 5" />
        <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
      </svg>
      <h3>{title || 'Drop your documents here'}</h3>
      <p>{hint || 'PDF, DOCX, PPTX, TXT or MD. You can choose several at once.'}</p>
      <button type="button" className="up-choose" onClick={() => input.current?.click()}>
        Choose files
      </button>
      <input ref={input} type="file" multiple accept={ACCEPT} hidden
             onChange={(e) => { take(e.target.files); e.target.value = '' }} />
    </div>
  )
}

const LABEL = {
  queued: 'Waiting', uploading: 'Uploading', reading: 'Reading',
  done: 'Added', partial: 'Partly read', failed: 'Failed',
}
const PILL = {
  queued: 'queued', uploading: 'working', reading: 'working',
  done: 'ready', partial: 'failed', failed: 'failed',
}

export function UploadQueue({ items }) {
  if (!items.length) return null
  return (
    <ul className="up-queue" aria-live="polite">
      {items.map((it) => (
        <li key={it.key} className="up-item">
          <div className="up-item-main">
            <span className="up-item-name" title={it.name}>{it.name}</span>
            {it.message && <span className="up-item-msg">{it.message}</span>}
          </div>
          <span className={`pill ${PILL[it.status]}`}><span className="pd" />{LABEL[it.status]}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * Destination picker: "No graph" plus every graph, grouped under
 * "Subject · Topic" (a <select> cannot nest groups two deep).
 */
export function GraphSelect({ subjects, value, onChange, label = 'Add to', id }) {
  return (
    <label className="up-dest" htmlFor={id}>
      <span>{label}</span>
      <select id={id} value={value || ''} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">No graph (All documents)</option>
        {subjects.flatMap((s) => s.topics.filter((t) => t.graphs.length).map((t) => (
          <optgroup key={t.id} label={`${s.name} · ${t.name}`}>
            {t.graphs.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </optgroup>
        )))}
      </select>
    </label>
  )
}

export default function UploadDialog({
  open, onClose, subjects, destination, onDestination, onFiles, items, onClearFinished,
}) {
  const card = useRef(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    card.current?.querySelector('select, button')?.focus()
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  const active = items.some((i) => ['queued', 'uploading', 'reading'].includes(i.status))
  const finished = items.some((i) => ['done', 'partial', 'failed'].includes(i.status))

  return (
    <div className="up-overlay" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose() }}>
      <div className="up-card" role="dialog" aria-modal="true" aria-labelledby="up-title" ref={card}>
        <div className="up-head">
          <h2 id="up-title">Add documents</h2>
          <button type="button" className="up-close" aria-label="Close" onClick={onClose}>×</button>
        </div>

        <GraphSelect id="up-dest" subjects={subjects} value={destination} onChange={onDestination} />

        <UploadDrop onFiles={onFiles} />

        <UploadQueue items={items} />

        <div className="up-foot">
          <span className="up-note">
            {active
              ? 'Files are read one at a time. You can close this — uploads keep going.'
              : 'The graph updates as each file finishes.'}
          </span>
          {finished && !active && (
            <button type="button" className="row-btn" onClick={onClearFinished}>Clear list</button>
          )}
        </div>
      </div>
    </div>
  )
}
