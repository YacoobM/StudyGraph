import React, { useEffect, useMemo, useRef, useState } from 'react'
import { listNotes, createNote, getSection, setSectionDone } from '../data/notes.js'
import { saveQuizResult } from '../services/api.js'
import QuizRunner from './QuizRunner.jsx'

/**
 * Notes tab: list, create and detail views on local state. A day plan groups the rail by day;
 * a full note lists its sections in order. Data comes from ../data/notes.js.
 */

/* --------------------------------------------------------------- helpers */

/** Renders a paragraph: a plain string, or strings mixed with bold and key marks (no HTML). */
function Rich({ parts }) {
  const list = typeof parts === "string" ? [parts] : parts || [];
  return (
    <>
      {list.map((p, i) => {
        if (typeof p === 'string') return <React.Fragment key={i}>{p}</React.Fragment>
        // The highlight is the outer element, so it runs unbroken across a bold term.
        if (p.key) {
          return (
            <span className="nt-key" key={i}>
              {p.bold ? <b>{p.text}</b> : p.text}
            </span>
          );
        }
        if (p.bold) return <b key={i}>{p.text}</b>;
        return <React.Fragment key={i}>{p.text}</React.Fragment>;
      })}
    </>
  );
}

/** Progress ring. Reuses .qz-ring from the quiz overview. */
function Ring({ pct, small }) {
  const r = 28;
  const c = 2 * Math.PI * r;
  const w = small ? 8 : 6;
  return (
    <div className={`qz-ring nt-ring${small ? " sm" : ""}`}>
      <svg viewBox="0 0 68 68">
        <circle className="bg" cx="34" cy="34" r={r} strokeWidth={w} />
        <circle
          className="fg"
          cx="34"
          cy="34"
          r={r}
          strokeWidth={w}
          strokeDasharray={c}
          strokeDashoffset={c * (1 - pct / 100)}
        />
      </svg>
      <div className="val">{pct}%</div>
    </div>
  );
}

const Arrow = () => (
  <svg
    width="15"
    height="15"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2.8"
    strokeLinecap="round"
    strokeLinejoin="round"
  >
    <path d="M19 12H6M11 6l-6 6 6 6" />
  </svg>
);

const pctOf = (done, total) => (total ? Math.round((done / total) * 100) : 0);

const kindLabel = (n) =>
  n.kind === "plan" ? `${n.days}-day plan` : "Full notes";

// Badge text: extension dropped and CSS truncates the rest; the full name is in the title.

/** The rail's contents: a plan grouped by day, a full note as one run. */
function groupsFor(note) {
  const numbered = note.sections.map((s, index) => ({ ...s, index }));
  if (note.kind !== "plan") return [{ label: null, items: numbered }];

  const out = [];
  numbered.forEach((s) => {
    const label = `Day ${s.day}`;
    if (!out.length || out[out.length - 1].label !== label)
      out.push({ label, items: [] });
    out[out.length - 1].items.push(s);
  });
  return out;
}

/* ============================================================ list view */

function ListView({ notes, loading, error, planning, onOpen, onCreate, onRegenerate }) {
  return (
    <div className="pane pane-scroll">
      <div className="nt-col">
        <div className="nt-head">
          <div className="nt-title">Notes</div>
          <button className="btn-primary2" onClick={onCreate}>
            ＋ New notes
          </button>
        </div>

        {error && <div className="nt-error">{error}</div>}

        {loading ? (
          <div className="empty tall">
            <p>Loading your notes…</p>
          </div>
        ) : notes.length === 0 ? (
          <div className="empty tall">
            <h3>No notes yet</h3>
            <p>
              Notes are written from the documents you have added. Make a set to
              start.
            </p>
          </div>
        ) : (
          notes.map((n) => (
            <div className="nt-card" key={n.id}>
              <button className="nt-card-main nt-card-open" onClick={() => onOpen(n.id)}>
                <div className="nt-card-top">
                  <span className={`nt-tag ${n.kind}`}>{kindLabel(n)}</span>
                  {n.status === "stale" && <span className="nt-tag stale">Stale</span>}
                  <span className="nt-card-date">{n.created}</span>
                </div>
                <div className="nt-card-name">{n.title}</div>
                <div className="nt-card-sub">
                  {n.docCount} document{n.docCount === 1 ? "" : "s"} · {n.done}{" "}
                  of {n.total} sections done
                </div>
              </button>
              <Ring pct={pctOf(n.done, n.total)} />
              {n.status === "stale" && (
                <button
                  className="btn-primary2 nt-card-regenerate"
                  onClick={() => onRegenerate(n)}
                  disabled={planning}
                >
                  {planning ? "Regenerating…" : "Regenerate"}
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/* ========================================================== create view */

function CreateView({ readyDocs, planning, error, onCancel, onGenerate }) {
  const [picked, setPicked] = useState(() => readyDocs.map((d) => d.id));
  const [split, setSplit] = useState(false);
  const [days, setDays] = useState(3);
  const [flagged, setFlagged] = useState(false);
  const docBlock = useRef(null);

  const allPicked = picked.length > 0 && picked.length === readyDocs.length;

  function toggle(id) {
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
    setFlagged(false);
  }

  function selectAll() {
    setPicked(allPicked ? [] : readyDocs.map((d) => d.id));
    setFlagged(false);
  }

  // Always clickable. With nothing picked it flags the block that needs an
  // answer and brings it into view, which says more than a dead button does.
  function generate() {
    if (!picked.length) {
      setFlagged(true);
      docBlock.current?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    onGenerate({
      docs: readyDocs.filter((d) => picked.includes(d.id)),
      split,
      days,
    });
  }

  return (
    <div className="pane pane-scroll">
      <div className="nt-col">
        <button className="nt-back" onClick={onCancel}>
          <Arrow /> Notes
        </button>
        <div className="nt-head nt-head-tight">
          <div className="nt-title">New notes</div>
        </div>

        <div
          className={`nt-block${flagged && !picked.length ? " needs" : ""}`}
          ref={docBlock}
        >
          <div className="nt-label-row">
            <div className="nt-label">Documents to cover</div>
            {readyDocs.length > 0 && (
              <button className="nt-link" onClick={selectAll}>
                {allPicked ? "Clear all" : "Select all"}
              </button>
            )}
          </div>

          {readyDocs.length === 0 ? (
            <div className="nt-pick-empty">
              No documents are ready yet. Add one in the Documents tab and it
              will show up here once StudyGraph has finished reading it.
            </div>
          ) : (
            <div className="nt-pick-list">
              {readyDocs.map((d) => {
                const on = picked.includes(d.id);
                const name = d.name || d.filename;
                return (
                  <button
                    className={`nt-pick${on ? " on" : ""}`}
                    key={d.id}
                    onClick={() => toggle(d.id)}
                    aria-pressed={on}
                  >
                    <span className="nt-cbx">✓</span>
                    <span className="nt-pick-main">
                      <span className="nt-pick-name">{name}</span>
                      <span className="nt-pick-sub">
                        {d.nodes ?? d.concepts ?? 0} concepts
                      </span>
                    </span>
                    {d.week && <span className="nt-week">{d.week}</span>}
                  </button>
                );
              })}
            </div>
          )}

          <div className="nt-field-msg">
            <span className="dot" />
            Pick at least one document to generate notes from.
          </div>
        </div>

        <div className="nt-block">
          <button
            className={`nt-switch-row${split ? " on" : ""}`}
            onClick={() => setSplit((s) => !s)}
            aria-pressed={split}
          >
            <span className="nt-switch" />
            <span>
              <span className="nt-switch-title">Split into days</span>
              <span className="nt-switch-desc">
                Off gives one set of notes ordered by document. On spreads the
                same material over a fixed number of days.
              </span>
            </span>
          </button>

          {split && (
            <div className="nt-days">
              <div className="nt-stepper">
                <button onClick={() => setDays((d) => Math.max(1, d - 1))}>
                  −
                </button>
                <div className="val mono">{days}</div>
                <button onClick={() => setDays((d) => Math.min(10, d + 1))}>
                  ＋
                </button>
              </div>
              <div className="nt-days-note">days</div>
            </div>
          )}
        </div>

        <div className="nt-actions">
          <div className="nt-summary">
            {picked.length ? (
              <>
                <b>{picked.length}</b> document{picked.length > 1 ? "s" : ""}
                {split ? (
                  <>
                    {" "}
                    over <b>{days}</b> days
                  </>
                ) : null}
              </>
            ) : (
              "Nothing selected"
            )}
          </div>
          <div className="nt-action-btns">
            <button
              className="btn-start"
              onClick={onCancel}
              disabled={planning}
            >
              Cancel
            </button>
            {/* Planning runs an LLM call, so this button says it is working. */}
            <button className="btn-primary2" onClick={generate} disabled={planning}>
              {planning ? 'Planning…' : 'Generate'}
            </button>
          </div>
        </div>

        {planning && (
          <div className="nt-working">
            Reading the graph and grouping it into sections. This takes a few
            seconds.
          </div>
        )}
        {error && <div className="nt-error">{error}</div>}
      </div>
    </div>
  );
}

/* ========================================================== detail view */

function SectionBody({ section }) {
  return (
    <div className="nt-body">
      {/* Each paragraph has its own heading so the section can be scanned. */}
      {section.body?.map((p, i) => (
        <div className="nt-para" key={i}>
          {p.heading && <h4 className="nt-para-head">{p.heading}</h4>}
          <p>
            <Rich parts={p.parts} />
          </p>
        </div>
      ))}

      {section.example && (
        <div className="nt-eg">
          <div className="nt-eg-t">{section.example.title}</div>
          {section.example.paragraphs.map((p, i) => (
            <p key={i}>
              <Rich parts={p} />
            </p>
          ))}
        </div>
      )}

      {section.table && (
        <>
          <div className="nt-block-label">Side by side</div>
          <div className="nt-tbl-wrap">
            <table className="nt-tbl">
              <thead>
                <tr>
                  {section.table.cols.map((c, i) => (
                    <th key={i}>{c}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {section.table.rows.map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j}>{cell}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {section.terms && (
        <>
          <div className="nt-block-label">Terms</div>
          {section.terms.map(([term, def], i) => (
            <div className="nt-term" key={i}>
              <div className="nt-term-name">{term}</div>
              <div className="nt-term-def">{def}</div>
            </div>
          ))}
        </>
      )}

      {section.relations?.length > 0 && (
        <>
          <div className="nt-block-label">Connections</div>
          {section.relations.map(([from, to, summary], i) => (
            <div className="nt-rel" key={i}>
              <span className="insp-pill">{from}</span>
              <span className="insp-pill">{to}</span>
              <p className="nt-rel-summary">{summary}</p>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/**
 * The section's quiz, taken inline below the footer row. Passing marks the section
 * complete; the pass mark is the server's.
 */
function SectionQuiz({ section, noteTitle, onPassed, onClose }) {
  const [saveError, setSaveError] = useState('')
  const total = section.quiz.length
  const need = section.passMark ?? total

  function finish({ answers, correct, total }) {
    setSaveError('')
    saveQuizResult({
      quizId: section.quizId, kind: 'section', answers, correct, total,
      title: section.title, group: noteTitle,
    }).catch((e) => setSaveError(`Your score was not saved: ${e.message}`))
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
      {saveError && <div className="nt-error">{saveError}</div>}
    </div>
  )
}

function ContextPanel({ section: loaded, open, onClose }) {
  // Empty until the section arrives: the rail knows a section exists before
  // its concepts and sources are fetched, so every block below is optional.
  const section = loaded || {};

  // File names only: pageLabel spans nearly the whole document at the current chunk size.
  // A closed pane stays in the DOM to animate shut, so it is made inert to leave the tab order.
  return (
    <aside
      className={`inspector nt-ctx${open ? "" : " off"}`}
      inert={open ? undefined : ""}
    >
      <div className="nt-ctx-inner">
        <div className="nt-pane-bar">
          <span className="nt-ctx-label">Context</span>
          <button className="nt-pane-x" onClick={onClose} title="Close">
            ✕
          </button>
        </div>

        {section.concepts?.length > 0 && (
          <div className="nt-ctx-block">
            <div className="nt-ctx-label">On this page</div>
            <div className="nt-concepts">
              {section.concepts.map((c) => (
                <span className="insp-pill" key={c}>
                  {c}
                </span>
              ))}
            </div>
          </div>
        )}

        {section.sources?.length > 0 && (
          <div className="nt-ctx-block">
            <div className="nt-ctx-label">Sources</div>
            {section.sources.map((s, i) => (
              <div className="nt-src-card" key={i}>
                <div className="nt-src-file">{s.file}</div>
              </div>
            ))}
          </div>
        )}

        {section.bridge && (
          <div className="nt-ctx-block">
            <div className="nt-ctx-label">Across documents</div>
            <div className="nt-bridge">
              <div className="nt-bridge-head">
                <span className="swatch" />
                Found by merging
              </div>
              <p>
                <Rich parts={section.bridge} />
              </p>
            </div>
          </div>
        )}

        {section.evolution?.length > 0 && (
          <div className="nt-ctx-block">
            <div className="nt-ctx-label">How this developed</div>
            <div className="evo-log">
              {section.evolution.map((e, i) => (
                <div className="evo-log-item past" key={i}>
                  <div className="evo-dot-col">
                    <span className="evo-dot" />
                    <span className="evo-line" />
                  </div>
                  <div className="evo-body">
                    <div className="evo-wk">{e.when}</div>
                    <div className="evo-label">{e.label}</div>
                    <div className="evo-detail">{e.detail}</div>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

function DetailView({ note, index, section, writing, writeError, coolLeft, planning, onRetry,
                      onBack, onOpenSection, onToggleDone, onRegenerate }) {
  const [railOn, setRailOn] = useState(true)
  const [ctxOn, setCtxOn] = useState(true)
  const center = useRef(null)
  const quizBox = useRef(null)
  const [quizOpen, setQuizOpen] = useState(false)

  // Moving to another section starts at the top, with its quiz closed.
  // Marking one done does not touch this, so the reader keeps their place.
  useEffect(() => {
    if (center.current) center.current.scrollTop = 0
    setQuizOpen(false)
  }, [note.id, index])

  // Opening the quiz brings it into view: it opens below the footer row,
  // which can sit at the very bottom of the scroll area.
  useEffect(() => {
    if (quizOpen) quizBox.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [quizOpen])

  const groups = useMemo(() => groupsFor(note), [note])
  // With one document every badge would read the same, so they earn their
  // place only once a note spans more than one.
  const pct = pctOf(note.done, note.total);
  // The rail knows every section from the note, so it stays usable while the
  // body of the open one is still being written.
  const stub = note.sections[index]
  const day = section?.day ?? stub?.day
  const eyebrow = note.kind === 'plan'
    ? `DAY ${day} OF ${note.days}`
    : `SECTION ${index + 1} OF ${note.total}`
  const hasNext = index < note.total - 1
  const hasQuiz = Boolean(section?.quiz && section.quizId)

  return (
    <div className="pane nt-detail">
      <div className="nt-detail-top">
        <button className="nt-back" onClick={onBack}>
          <Arrow /> Notes
        </button>
        <div className="nt-detail-title">
          {note.title}
          {note.status === "stale" && <span className="nt-tag stale">Stale</span>}
        </div>
        <div className="nt-detail-actions">
          {note.status === "stale" && (
            <button className="btn-primary2" onClick={onRegenerate} disabled={planning}>
              {planning ? "Regenerating…" : "Regenerate notes"}
            </button>
          )}
          <button
            className={`nt-icon-btn${railOn ? " on" : ""}`}
            onClick={() => setRailOn((v) => !v)}
            aria-pressed={railOn}
          >
            ▤ <span className="lbl">Contents</span>
          </button>
          <button
            className={`nt-icon-btn${ctxOn ? " on" : ""}`}
            onClick={() => setCtxOn((v) => !v)}
            aria-pressed={ctxOn}
          >
            ◫ <span className="lbl">Sources</span>
          </button>
        </div>
      </div>

      <div
        className={`nt-shell${railOn ? "" : " rail-off"}${ctxOn ? "" : " ctx-off"}`}
      >
        <div className="nt-rail" inert={railOn ? undefined : ""}>
          <div className="nt-rail-inner">
            <div className="nt-pane-bar">
              <span className="nt-ctx-label">Contents</span>
              <button
                className="nt-pane-x"
                onClick={() => setRailOn(false)}
                title="Close"
              >
                ✕
              </button>
            </div>
            <div className="nt-rail-note-title">{note.title}</div>
            <div className="nt-rail-note-meta">
              {kindLabel(note)} · {note.docCount} document
              {note.docCount === 1 ? "" : "s"}
            </div>

            {groups.map((g) => (
              <div key={g.label ?? "all"}>
                {g.label && <div className="nt-rail-group">{g.label}</div>}
                {g.items.map((s) => (
                  <div
                    className={`nt-rail-item${s.index === index ? " active" : ""}${s.done ? " done" : ""}`}
                    key={s.id}
                    onClick={() => onOpenSection(s.index)}
                  >
                    <button
                      className="qz-check nt-mark"
                      onClick={(e) => {
                        e.stopPropagation();
                        onToggleDone(s.index, !s.done);
                      }}
                      title={s.done ? "Mark not done" : "Mark done"}
                    >
                      ✓
                    </button>
                    <div className="nt-rail-title">{s.title}</div>
                  </div>
                ))}
              </div>
            ))}

            <div className="nt-rail-foot">
              <Ring pct={pct} small />
              <div className="txt">
                {note.done} of {note.total}
                <br />
                complete
              </div>
            </div>
          </div>
        </div>

        <div className="nt-center" ref={center}>
          <div className="nt-center-inner">
            <div className="nt-sec-head">
              <div className="nt-eyebrow">
                {eyebrow}
                {section?.week ? ` · ${section.week}` : ""}
              </div>
              <div className="nt-sec-title">
                {section?.title || stub?.title}
              </div>
              {section && (
                <div className="nt-lead">
                  <Rich parts={section.lead} />
                </div>
              )}
            </div>

            {/* Writing a section is a real wait, so say what is happening. */}
            {writing && (
              <div className="nt-writing">
                <span className="nt-writing-dot" />
                <div>
                  <b>Writing this section.</b>
                  <p>
                    Reading the source passages behind {stub ? "these" : "its"}{" "}
                    concepts and drafting the notes. This takes twenty to thirty
                    seconds the first time; opening it again is instant.
                  </p>
                </div>
              </div>
            )}

            {writeError && !writing && (
              <div className="nt-error">
                {writeError}
                {coolLeft > 0 && (
                  <span className="nt-cool">
                    Asking again now would only be refused — {coolLeft}s left.
                  </span>
                )}
                <button
                  className="btn-start"
                  onClick={onRetry}
                  disabled={coolLeft > 0}
                >
                  {coolLeft > 0 ? `Try again in ${coolLeft}s` : "Try again"}
                </button>
              </div>
            )}

            {section && <SectionBody section={section} />}

            <div className="nt-foot">
              <button
                className={`nt-done-btn${section?.done ? " on" : ""}`}
                onClick={() => onToggleDone(index, !section?.done)}
                disabled={!section}
              >
                <span className="qz-check">✓</span>
                {section?.done ? "Done" : "Mark done"}
              </button>
              <div className="nt-foot-right">
                {hasQuiz && (
                  <button className={quizOpen ? 'btn-secondary' : 'btn-primary2'}
                          onClick={() => setQuizOpen((v) => !v)} aria-expanded={quizOpen}>
                    {quizOpen ? 'Close quiz' : 'Quiz this section'}
                  </button>
                )}
                {hasNext && (
                  <button className="btn-start" onClick={() => onOpenSection(index + 1)}>Next</button>
                )}
              </div>
            </div>

            {hasQuiz && quizOpen && (
              <div ref={quizBox}>
                <SectionQuiz
                  key={section.id}
                  section={section}
                  noteTitle={note.title}
                  onClose={() => setQuizOpen(false)}
                  onPassed={() => { if (!section.done) onToggleDone(index, true) }}
                />
              </div>
            )}
          </div>
        </div>

        <ContextPanel
          section={section}
          open={ctxOn}
          onClose={() => setCtxOn(false)}
        />
      </div>
    </div>
  );
}

/* ================================================================= shell */

export default function NotesPane({ readyDocs = [], graphId }) {
  const [notes, setNotes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [view, setView] = useState("list");
  const [openId, setOpenId] = useState(null);
  const [index, setIndex] = useState(0);

  // Loaded, not derived: fetching the body writes it, slowly the first time.
  const [section, setSection] = useState(null)
  const [writing, setWriting] = useState(false)
  const [writeError, setWriteError] = useState('')
  const [planning, setPlanning] = useState(false)

  // Bumped by Try again: setting the same index again would not re-run the effect.
  const [retryTick, setRetryTick] = useState(0)
  // Epoch ms until which this section refuses to be asked again.
  const [coolUntil, setCoolUntil] = useState(0);
  const [, tick] = useState(0);

  const note = notes.find((n) => n.id === openId);

  useEffect(() => {
    let live = true;
    if (!graphId) {
      setNotes([]);
      setLoading(false);
      return () => {
        live = false;
      };
    }
    listNotes(graphId)
      .then((rows) => live && setNotes(rows))
      .catch((e) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [graphId]);

  // One fetch per section opened; `live` stops a late answer landing on another section.
  useEffect(() => {
    if (view !== "detail" || !note) return undefined;
    const ac = new AbortController();
    let live = true;
    setSection(null);
    setWriteError("");
    setCoolUntil(0);
    setWriting(true);
    getSection(graphId, note.id, index, { signal: ac.signal })
      .then((s) => live && setSection(s))
      .catch((e) => {
        if (!live || e?.name === "AbortError") return;
        setWriteError(e.message);
        if (e.cooldownSeconds)
          setCoolUntil(Date.now() + e.cooldownSeconds * 1000);
      })
      .finally(() => live && setWriting(false));
    // `live` stops a late answer landing on the section the reader moved to.
    // The abort is separate and does less than it looks: this backend does not
    // cancel a handler when the browser goes (measured), so the generation runs
    // on regardless — it frees the connection, and the server's own in-flight
    // guard is what stops a second one starting.
    return () => {
      live = false;
      ac.abort();
    };
  }, [graphId, view, openId, index, retryTick]); // eslint-disable-line react-hooks/exhaustive-deps

  function openDetail(id) {
    setOpenId(id);
    setIndex(0);
    setView("detail");
  }

  // Completion is client-side, so this updates what is on screen directly
  // rather than refetching a list the server would return unchanged.
  function toggleDone(i, done) {
    setSectionDone(openId, i, done);
    setNotes((rows) =>
      rows.map((n) => {
        if (n.id !== openId) return n;
        const sections = n.sections.map((s, k) =>
          k === i ? { ...s, done } : s,
        );
        return { ...n, sections, done: sections.filter((s) => s.done).length };
      }),
    );
    if (i === index) setSection((s) => (s ? { ...s, done } : s));
  }

  useEffect(() => {
    if (!coolUntil) return undefined;
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [coolUntil]);

  const coolLeft = coolUntil
    ? Math.max(0, Math.ceil((coolUntil - Date.now()) / 1000))
    : 0;

  async function generate(opts) {
    setPlanning(true);
    setError("");
    try {
      const id = await createNote(graphId, opts);
      setNotes(await listNotes(graphId));
      if (id) openDetail(id);
    } catch (e) {
      setError(e.message);
    } finally {
      setPlanning(false);
    }
  }

  async function regenerate(noteToRegenerate) {
    const currentIds = new Set(readyDocs.map((doc) => String(doc.id)));
    const remainingIds = (noteToRegenerate.documentIds || [])
      .filter((id) => currentIds.has(String(id)));
    if (!remainingIds.length) {
      setError("There are no remaining documents to regenerate these notes from.");
      return;
    }
    await generate({
      docs: remainingIds.map((id) => ({ id })),
      split: noteToRegenerate.kind === "plan",
      days: noteToRegenerate.days,
    });
  }

  if (view === "create") {
    return (
      <CreateView
        readyDocs={readyDocs}
        planning={planning}
        error={error}
        onCancel={() => setView("list")}
        onGenerate={generate}
      />
    );
  }

  if (view === "detail" && note) {
    return (
      <DetailView
        note={note}
        index={index}
        section={section}
        writing={writing}
        writeError={writeError}
        coolLeft={coolLeft}
        planning={planning}
        onRetry={() => setRetryTick((n) => n + 1)}
        onBack={() => setView("list")}
        onOpenSection={setIndex}
        onToggleDone={toggleDone}
        onRegenerate={() => regenerate(note)}
      />
    );
  }

  return (
    <ListView
      notes={notes}
      loading={loading}
      error={error}
      planning={planning}
      onOpen={openDetail}
      onCreate={() => setView("create")}
      onRegenerate={regenerate}
    />
  );
}
