/**
 * Notes tab data: adapts /api/notes responses to the shapes NotesPane renders. Notes live in
 * the backend process; a section body is written on first open, which is the slow call.
 */

import {
  planNote,
  fetchNotes,
  fetchNote,
  updateNoteSection,
  writeSection,
} from "../services/api.js";

/** Completion set in this session, shown straight away while the save is in flight. */
const completion = new Map()

const doneKey = (noteId, index) => `${noteId}:${index}`;

/** The last note list seen, so getSection maps an index to a section id without a refetch. */
const seen = new Map()

/**
 * Writes in flight and sections that just refused, so the UI does not re-ask. The backend's own
 * guard protects the quota; Retry-After sets the cooldown, WRITE_COOLDOWN_MS is the fallback.
 */
const inFlight = new Map(); // sectionId -> promise
const coolUntil = new Map(); // sectionId -> epoch ms
const WRITE_COOLDOWN_MS = 60000;

/* ---------------------------------------------------------------- adapters */

// Supabase sends ISO strings; older rows sent epoch seconds.
function toMillis(value) {
  if (typeof value === "number") return value * 1000;
  const parsed = Date.parse(value || "");
  return Number.isFinite(parsed) ? parsed : null;
}

function createdLabel(value) {
  const at = toMillis(value);
  if (!at) return "";
  const mins = Math.round((Date.now() - at) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const days = Math.round(hrs / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return new Date(at).toLocaleDateString("en-AU", { day: "numeric", month: "short" });
}

/** A usable day number, or null. Stored plans from before the fix can hold null. */
export const validDay = (value) => (Number.isInteger(value) && value >= 1 ? value : null);

/** A note as the list cards and the contents rail want it. */
function toNote(raw) {
  const sections = raw.sections || [];
  // Only treat it as a day plan when every section has a real day.
  const isPlan = raw.kind === "plan" && sections.length > 0 && sections.every((s) => validDay(s.day));
  return {
    id: raw.id,
    title: raw.title,
    kind: isPlan ? "plan" : "full",
    status: raw.status || "current",
    days: isPlan ? Math.max(...sections.map((s) => s.day)) : null,
    documentIds: raw.document_ids || [],
    docCount: (raw.documents || []).length,
    created: createdLabel(raw.created_at),
    total: raw.total,
    done: (raw.sections || []).filter((s, i) => isDone(raw.id, i, s)).length,
    sections: (raw.sections || []).map((s, i) => ({
      id: s.id,
      title: s.title,
      day: isPlan ? s.day : null,
      done: isDone(raw.id, i, s),
    })),
  };
}

const isDone = (noteId, index, section) =>
  completion.get(doneKey(noteId, index)) ?? Boolean(section.done);

/** A body paragraph: a heading plus {text, bold, key} spans; an old plain string is wrapped. */
function toParagraph(p) {
  if (typeof p === "string") return { heading: "", parts: [{ text: p }] };
  return { heading: p?.heading || "", parts: spacedSpans(p?.parts || []) };
}

/** Puts back spaces the model drops between spans; none goes before punctuation. */
const OPENS_WITH_PUNCTUATION = /^[\s,.;:!?)\]}%'’"”…-]/

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
 * A written section as the blocks SectionBody renders: columns become cols, terms and
 * relations become tuples, and blocks the model left null stay null.
 */
function toSection(raw, noteId, index) {
  const body = raw.body || {};
  return {
    id: raw.id,
    title: raw.title,
    day: validDay(raw.day),
    done: isDone(noteId, index, raw),
    concepts: raw.concepts || [],
    sources: (raw.sources || []).map((file) => ({ file })),
    // Only links with a summary are shown: a bare extracted label is often wrong.
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

    // The section quiz from the same call as the body; null when absent or it failed the checks.
    quiz: body.quiz?.length ? body.quiz : null,
    quizId: body.quiz_id || null,
    // Correct answers that complete the section, from quiz_service.pass_mark.
    passMark: body.quiz_pass_mark ?? null,

  };
}

/* -------------------------------------------------------------------- api */

/** Every note, newest first, with enough of each section for the list cards; no bodies. */
export async function listNotes(graphId) {
  const { notes = [] } = await fetchNotes(graphId)
  const adapted = notes.map(toNote)
  adapted.forEach((n) => seen.set(n.id, n))
  return adapted
}

/** One section, written on first open (the slow call) and stored after, so safe to call again. */
export async function getSection(graphId, noteId, index, { signal } = {}) {
  let note = seen.get(noteId)
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

/** Marks a section complete or not and saves it. Throws when the save fails. */
export async function setSectionDone(graphId, noteId, index, done) {
  const key = doneKey(noteId, index);
  const note = seen.get(noteId);
  const stub = note?.sections[index];
  const before = { cached: completion.get(key), stub: stub?.done };
  const apply = (value) => {
    if (stub) {
      stub.done = Boolean(value);
      note.done = note.sections.filter((s) => s.done).length;
    }
  };
  completion.set(key, Boolean(done));
  apply(done);
  if (!stub) return;
  try {
    await updateNoteSection(graphId, noteId, stub.id, { done });
  } catch (e) {
    if (before.cached === undefined) completion.delete(key);
    else completion.set(key, before.cached);
    apply(before.stub);
    throw e;
  }
}

/** Plans a note over the picked documents and returns its id; sections come back empty. */
export async function createNote(graphId, { docs, split, days }) {
  const ids = (docs || []).map((d) => d?.id).filter((id) => id != null)
  if (!ids.length) return null

  const { note } = await planNote(graphId, ids, split ? days : null);
  // Regeneration replaces the sections under the same note id. Clear the
  // client-side completion cache so the new sections start at 0% progress.
  const noteId = String(note.id);
  const prefix = `${noteId}:`;
  for (const key of completion.keys()) {
    if (key.startsWith(prefix)) completion.delete(key);
  }
  const adapted = toNote(note);
  seen.set(adapted.id, adapted);
  return adapted.id;
}
