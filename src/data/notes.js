/**
 * Notes data for the Notes tab.
 *
 * The four functions at the bottom are the whole surface the components use:
 * listNotes, createNote, getSection, setSectionDone. They were mock data; they
 * are now real calls against /api/notes. Everything between here and there is
 * the adapter that turns what the backend returns into the shape the
 * components already render, so swapping the source did not mean rewriting
 * NotesPane's markup.
 *
 * They are async now, which the mock versions were not. That is the one thing
 * the swap could not hide: a network call cannot be read during render, so
 * NotesPane loads notes in an effect and shows a busy state while a section
 * is being written.
 *
 * Two backend facts the UI has to live with:
 *
 *   Sections are planned empty. A body is written on first open and takes
 *   twenty to thirty seconds, so getSection is the slow call, not listNotes.
 *
 *   Notes live in the backend process, not a database. They disappear when it
 *   restarts, and section completion is not stored at all — setSectionDone
 *   stays local here until there is somewhere to put it.
 */

import {
  planNote,
  fetchNotes,
  fetchNote,
  writeSection,
} from "../services/api.js";

/**
 * Completion, held client-side because the backend does not record it.
 * Keyed on note id and section index rather than section id, since that is
 * what setSectionDone is handed, and it survives a refetch of the note.
 */
const completion = new Map();

const doneKey = (noteId, index) => `${noteId}:${index}`;

/**
 * The last note list seen, so getSection can turn a section index into the
 * section id the write endpoint wants without a second round trip.
 */
const seen = new Map();

/**
 * Writes running right now, and sections that just refused.
 *
 * The backend holds its own in-flight guard and that is what protects the
 * quota — a request the browser abandons keeps generating there, so nothing
 * here can be relied on to stop a call. These two exist so the UI does not
 * ask for work it already knows is running or refused, and so a reader who
 * clicks again is told why rather than watching another spinner.
 *
 * The cooldown length comes from the 503's Retry-After when the model server
 * named one; WRITE_COOLDOWN_MS is only the fallback for a refusal that did
 * not say.
 */
const inFlight = new Map(); // sectionId -> promise
const coolUntil = new Map(); // sectionId -> epoch ms
const WRITE_COOLDOWN_MS = 60000;

/* ---------------------------------------------------------------- adapters */

function createdLabel(seconds) {
  if (!seconds) return "";
  const mins = Math.round((Date.now() - seconds * 1000) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.round(hrs / 24);
  return days === 1 ? "yesterday" : days < 7 ? `${days} days ago` : "last week";
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
  };
}

const isDone = (noteId, index, section) =>
  completion.get(doneKey(noteId, index)) ?? Boolean(section.done);

/**
 * A body paragraph: a heading plus the marked spans that make up its prose.
 *
 * The spans arrive as {text, bold, key}, which is what Rich already renders —
 * bold for the first mention of a concept or a figure that matters, key for
 * the one sentence worth keeping. A paragraph written before spans existed
 * comes through as a plain string, so it is wrapped rather than dropped.
 */
function toParagraph(p) {
  if (typeof p === "string") return { heading: "", parts: [{ text: p }] };
  return { heading: p?.heading || "", parts: spacedSpans(p?.parts || []) };
}

/**
 * Puts back the spaces the model drops between spans.
 *
 * The spans are meant to concatenate into the paragraph, and mostly they carry
 * their own leading space — but not always, which reads as "either way.
 * Initially" losing its gap and running together. A space goes in only where
 * neither side already has one and the next span does not open with
 * punctuation, so a comma or full stop still hugs the word before it.
 */
const OPENS_WITH_PUNCTUATION = /^[\s,.;:!?)\]}%'’"”…-]/;

function spacedSpans(parts) {
  return parts.map((span, i) => {
    const text = String(span?.text ?? "");
    if (i === 0) return { ...span, text };
    const previous = String(parts[i - 1]?.text ?? "");
    const needsGap =
      previous &&
      !/\s$/.test(previous) &&
      text &&
      !OPENS_WITH_PUNCTUATION.test(text);
    return { ...span, text: needsGap ? ` ${text}` : text };
  });
}

/**
 * A written section, flattened into the blocks SectionBody renders.
 *
 * The renames are real differences, not noise: the backend calls a table's
 * headers `columns` and returns terms and relations as objects, while the
 * components read `cols` and take both as tuples. Any block the model left
 * null stays null and simply is not rendered — an unwritten table and a
 * section whose concepts have no links between them are both ordinary.
 */
function toSection(raw, noteId, index) {
  const body = raw.body || {};
  return {
    id: raw.id,
    title: raw.title,
    day: raw.day,
    week: null, // the backend does not carry weeks here
    done: isDone(noteId, index, raw),
    concepts: raw.concepts || [],
    sources: (raw.sources || []).map((file) => ({ file })),
    // Only links with a summary are shown. A bare label from extraction or
    // fusion is often wrong ("abstract data type is a type of data
    // structure"), and the summary is the only part that says what the link is.
    relations: (raw.relations || [])
      .filter((e) => (e.summary || "").trim())
      .map((e) => [e.from, e.to, e.summary.trim()]),

    lead: body.lead || "",
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

    // Nothing produces these yet; the components already skip them when null.
    evolution: null,
    bridge: null,

    // Carried through for anyone who wants to see how the body was made.
    // A populated label_violations means the writer used a relation label as
    // a word and the one rewrite did not clear it — worth knowing, not worth
    // refusing to show the section over.
    meta: {
      chunksUsed: body.chunks_used ?? 0,
      labelViolations: body.label_violations || [],
      sources: body.sources || [],
    },
  };
}

/* -------------------------------------------------------------------- api */

/**
 * Every note, newest first, with just enough of each section to draw the list
 * cards and the contents rail. Bodies are not included — the detail view asks
 * for one section at a time through getSection.
 */
export async function listNotes(graphId) {
  const { notes = [] } = await fetchNotes(graphId);
  const adapted = notes.map(toNote);
  adapted.forEach((n) => seen.set(n.id, n));
  return adapted;
}

/**
 * One section, with its body written if it has not been already.
 *
 * This is the slow call. The write endpoint generates on first open and
 * serves what it stored on every open after, so this is safe to call again
 * whenever the reader moves between sections.
 */
export async function getSection(graphId, noteId, index, { signal } = {}) {
  let note = seen.get(noteId);
  if (!note) {
    const fetched = await fetchNote(graphId, noteId);
    note = toNote(fetched.note);
    seen.set(noteId, note);
  }

  const stub = note.sections[index];
  if (!stub) return null;

  const left = (coolUntil.get(stub.id) || 0) - Date.now();
  if (left > 0) {
    const err = new Error("That section could not be written a moment ago.");
    err.cooldownSeconds = Math.ceil(left / 1000);
    throw err;
  }

  let pending = inFlight.get(stub.id);
  if (!pending) {
    pending = writeSection(graphId, noteId, stub.id, { signal })
      .then((res) => {
        coolUntil.delete(stub.id);
        return res;
      })
      .catch((e) => {
        // Abandoning a write is not a failure of it, so it starts no cooldown.
        if (e?.name !== "AbortError") {
          const secs = e?.retryAfter || WRITE_COOLDOWN_MS / 1000;
          coolUntil.set(stub.id, Date.now() + secs * 1000);
          e.cooldownSeconds = secs;
        }
        throw e;
      })
      .finally(() => {
        if (inFlight.get(stub.id) === pending) inFlight.delete(stub.id);
      });
    inFlight.set(stub.id, pending);
  }

  const { section } = await pending;
  return toSection(section, noteId, index);
}

/**
 * Marks one section complete or incomplete.
 *
 * Local, and deliberately still synchronous: the backend has nowhere to put
 * this yet, so it is held in `completion` and applied over whatever the
 * server returns. It is lost on reload, like the notes themselves.
 */
export function setSectionDone(noteId, index, done) {
  completion.set(doneKey(noteId, index), Boolean(done));
  const note = seen.get(noteId);
  if (note?.sections[index]) {
    note.sections[index].done = Boolean(done);
    note.done = note.sections.filter((s) => s.done).length;
  }
}

/**
 * Plans a note over the picked documents and returns its id.
 *
 * `docs` are rows as the Documents tab holds them; only the id is sent. The
 * sections come back empty — each body is written when it is first opened.
 */
export async function createNote(graphId, { docs, split, days }) {
  const ids = (docs || []).map((d) => d?.id).filter((id) => id != null);
  if (!ids.length) return null;

  const { note } = await planNote(graphId, ids, split ? days : null);
  const adapted = toNote(note);
  seen.set(adapted.id, adapted);
  return adapted.id;
}
