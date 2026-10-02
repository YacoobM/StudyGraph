/**
 * Notes tab data: adapts /api/notes responses to the shapes NotesPane renders. Notes live in
 * the backend process; a section body is written on first open, which is the slow call.
 */

import { planNote, fetchNotes, fetchNote, writeSection } from '../services/api.js'

/** Completion held client-side, keyed on note id and section index so it survives a refetch. */
const completion = new Map()

const doneKey = (noteId, index) => `${noteId}:${index}`

/** The last note list seen, so getSection maps an index to a section id without a refetch. */
const seen = new Map()

/**
 * Writes in flight and sections that just refused, so the UI does not re-ask. The backend's own
 * guard protects the quota; Retry-After sets the cooldown, WRITE_COOLDOWN_MS is the fallback.
 */
const inFlight = new Map()    // sectionId -> promise
const coolUntil = new Map()   // sectionId -> epoch ms
const WRITE_COOLDOWN_MS = 60000

/* ---------------------------------------------------------------- adapters */

function createdLabel(seconds) {
  if (!seconds) return ''
  const mins = Math.round((Date.now() - seconds * 1000) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} min ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs} hr ago`
  const days = Math.round(hrs / 24)
  return days === 1 ? 'yesterday' : days < 7 ? `${days} days ago` : 'last week'
}

/** A note as the list cards and the contents rail want it. */
function toNote(raw) {
  return {
    id: raw.id,
    title: raw.title,
    kind: raw.kind,
    days: raw.days,
    docCount: (raw.documents || []).length,
    created: createdLabel(raw.created_at),
    total: raw.total,
    done: (raw.sections || []).filter((s, i) => isDone(raw.id, i, s)).length,
    sections: (raw.sections || []).map((s, i) => ({
      id: s.id,
      title: s.title,
      day: s.day,
      done: isDone(raw.id, i, s),
    })),
  }
}

const isDone = (noteId, index, section) =>
  completion.get(doneKey(noteId, index)) ?? Boolean(section.done)

/** A body paragraph: a heading plus {text, bold, key} spans; an old plain string is wrapped. */
function toParagraph(p) {
  if (typeof p === 'string') return { heading: '', parts: [{ text: p }] }
  return { heading: p?.heading || '', parts: spacedSpans(p?.parts || []) }
}

/** Puts back spaces the model drops between spans; none goes before punctuation. */
const OPENS_WITH_PUNCTUATION = /^[\s,.;:!?)\]}%'’"”…-]/

function spacedSpans(parts) {
  return parts.map((span, i) => {
    const text = String(span?.text ?? '')
    if (i === 0) return { ...span, text }
    const previous = String(parts[i - 1]?.text ?? '')
    const needsGap = previous
      && !/\s$/.test(previous)
      && text
      && !OPENS_WITH_PUNCTUATION.test(text)
    return { ...span, text: needsGap ? ` ${text}` : text }
  })
}

/**
 * A written section as the blocks SectionBody renders: columns become cols, terms and
 * relations become tuples, and blocks the model left null stay null.
 */
function toSection(raw, noteId, index) {
  const body = raw.body || {}
  return {
    id: raw.id,
    title: raw.title,
    day: raw.day,
    week: null,                       // the backend does not carry weeks here
    done: isDone(noteId, index, raw),
    concepts: raw.concepts || [],
    sources: (raw.sources || []).map((file) => ({ file })),
    // Only links with a summary are shown: a bare extracted label is often wrong.
    relations: (raw.relations || [])
      .filter((e) => (e.summary || '').trim())
      .map((e) => [e.from, e.to, e.summary.trim()]),

    lead: body.lead || '',
    body: (body.body || []).map(toParagraph),
    example: body.example
      ? { title: body.example.title, paragraphs: body.example.paragraphs || [] }
      : null,
    table: body.table
      ? { cols: body.table.columns || [], rows: body.table.rows || [] }
      : null,
    terms: body.terms?.length
      ? body.terms.map((t) => [t.term, t.definition])
      : null,

    // The section quiz from the same call as the body; null when absent or it failed the checks.
    quiz: body.quiz?.length ? body.quiz : null,
    quizId: body.quiz_id || null,
    // Correct answers that complete the section, from quiz_service.pass_mark.
    passMark: body.quiz_pass_mark ?? null,

    // Nothing produces these yet; the components already skip them when null.
    evolution: null,
    bridge: null,

    // Carried through for inspection; label_violations lists relation labels used as words.
    meta: {
      chunksUsed: body.chunks_used ?? 0,
      labelViolations: body.label_violations || [],
      sources: body.sources || [],
    },
  }
}

/* -------------------------------------------------------------------- api */

/** Every note, newest first, with enough of each section for the list cards; no bodies. */
export async function listNotes() {
  const { notes = [] } = await fetchNotes()
  const adapted = notes.map(toNote)
  adapted.forEach((n) => seen.set(n.id, n))
  return adapted
}

/** One section, written on first open (the slow call) and stored after, so safe to call again. */
export async function getSection(noteId, index, { signal } = {}) {
  let note = seen.get(noteId)
  if (!note) {
    const fetched = await fetchNote(noteId)
    note = toNote(fetched.note)
    seen.set(noteId, note)
  }

  const stub = note.sections[index]
  if (!stub) return null

  const left = (coolUntil.get(stub.id) || 0) - Date.now()
  if (left > 0) {
    const err = new Error('That section could not be written a moment ago.')
    err.cooldownSeconds = Math.ceil(left / 1000)
    throw err
  }

  let pending = inFlight.get(stub.id)
  if (!pending) {
    pending = writeSection(stub.id, { signal })
      .then((res) => {
        coolUntil.delete(stub.id)
        return res
      })
      .catch((e) => {
        // Abandoning a write is not a failure of it, so it starts no cooldown.
        if (e?.name !== 'AbortError') {
          const secs = e?.retryAfter || WRITE_COOLDOWN_MS / 1000
          coolUntil.set(stub.id, Date.now() + secs * 1000)
          e.cooldownSeconds = secs
        }
        throw e
      })
      .finally(() => {
        if (inFlight.get(stub.id) === pending) inFlight.delete(stub.id)
      })
    inFlight.set(stub.id, pending)
  }

  const { section } = await pending
  return toSection(section, noteId, index)
}

/** Marks a section complete or not; held locally over server data and lost on reload. */
export function setSectionDone(noteId, index, done) {
  completion.set(doneKey(noteId, index), Boolean(done))
  const note = seen.get(noteId)
  if (note?.sections[index]) {
    note.sections[index].done = Boolean(done)
    note.done = note.sections.filter((s) => s.done).length
  }
}

/** Plans a note over the picked documents and returns its id; sections come back empty. */
export async function createNote({ docs, split, days }) {
  const ids = (docs || []).map((d) => d?.id).filter((id) => id != null)
  if (!ids.length) return null

  const { note } = await planNote(ids, split ? days : null)
  const adapted = toNote(note)
  seen.set(adapted.id, adapted)
  return adapted.id
}
