import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchQuiz, fetchQuizResults, fetchSectionQuizzes, generateDocumentQuiz, saveQuizResult,
} from "../services/api.js";
import QuizRunner, { BackLink, QuizReview } from "./QuizRunner.jsx";
import { displayName } from "../utils/documentTitle.js";
// Drawn for this project in the app's accent colour; see the SVG's header.
import landingArt from "../assets/quiz-landing.svg";

// Pass marks are never computed here. A section quiz carries pass_mark and a
// document topic carries pass_marks for each difficulty filter, both from
// quiz_service.pass_mark, so the screen and the saved result always agree.
const RING_RADIUS = 28;
const RING_VIEWBOX = 66;
const DIFFICULTIES = ["all", "easy", "medium", "hard"];

/** The server's stored section quizzes in the shape the list renders. */
const toNoteQuizzes = ({ notes = [] }) => notes.map((n) => ({
  id: n.title,
  title: n.title,
  sections: (n.sections || [])
    .filter((s) => s.questions?.length)
    .map((s) => ({
      quizId: s.quiz_id, title: s.title, quiz: s.questions,
      passMark: s.pass_mark,
    })),
})).filter((n) => n.sections.length);

/** "Sep 30". Fixed to English month names so it reads the same everywhere. */
const shortDate = (seconds) => (seconds
  ? new Date(seconds * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" })
  : "");

/** "3 of 5 correct · Sep 30", plus the attempt count only when there is more than one. */
export function scoreLine(r) {
  if (!r) return "";
  const parts = [`${r.best.correct} of ${r.best.total} correct`, shortDate(r.last_at)];
  if (r.attempts > 1) parts.push(`${r.attempts} attempts`);
  return parts.filter(Boolean).join(" · ");
}

/**
 * Quiz tab. Opens on two cards, one per source, each opening its own list:
 *
 *   By document — one quiz per uploaded document, generated only when its
 *   "Generate quiz" button is pressed. Opening the tab generates nothing.
 *
 *   From notes — the quiz each written revision-note section came with.
 *   These are written by the notes write call, never here, and listed from
 *   the server's stored copy, so they outlast the in-memory notes.
 *
 * Every finished attempt is saved on the server with the answers picked on
 * each first try; the scores shown are the server's summaries of those.
 */
export default function QuizPane({ onToast }) {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [reason, setReason] = useState(null);
  const [error, setError] = useState("");
  const [results, setResults] = useState({}); // quizId -> server summary
  const [noteQuizzes, setNoteQuizzes] = useState([]);
  const [excluded, setExcluded] = useState([]);
  const [filter, setFilter] = useState("all");
  const [view, setView] = useState("home");   // home | documents | notes
  const [run, setRun] = useState(null);
  const [review, setReview] = useState(null);
  const [pollAfter, setPollAfter] = useState(4);
  const alive = useRef(true);

  const refreshDocs = useCallback(async () => {
    const data = await fetchQuiz();
    if (!alive.current) return;
    setDocs(data.documents || []);
    setReason(data.reason || null);
    if (data.poll_after_seconds) setPollAfter(data.poll_after_seconds);
  }, []);

  useEffect(() => {
    alive.current = true;
    (async () => {
      const [quiz, res, notes] = await Promise.allSettled([
        refreshDocs(), fetchQuizResults(), fetchSectionQuizzes(),
      ]);
      if (!alive.current) return;
      if (quiz.status === "rejected") setReason(quiz.reason?.message || "Could not load quizzes.");
      if (res.status === "fulfilled") setResults(res.value.results || {});
      if (notes.status === "fulfilled") setNoteQuizzes(toNoteQuizzes(notes.value));
      setLoading(false);
    })();
    return () => { alive.current = false; };
  }, [refreshDocs]);

  // Poll only while something is generating, and only as long as it is.
  const generating = docs.some((d) => d.status === "generating");
  useEffect(() => {
    if (!generating) return undefined;
    const t = setTimeout(() => { refreshDocs().catch(() => {}); }, pollAfter * 1000);
    return () => clearTimeout(t);
  }, [generating, docs, pollAfter, refreshDocs]);

  async function generate(doc) {
    setError("");
    setDocs((rows) => rows.map((d) => (d.document_id === doc.document_id ? { ...d, status: "generating" } : d)));
    try {
      const { document } = await generateDocumentQuiz(doc.document_id);
      setDocs((rows) => rows.map((d) => (d.document_id === doc.document_id ? document : d)));
    } catch (e) {
      setError(e.message);
      refreshDocs().catch(() => {});
    }
  }

  const topics = useMemo(
    () => docs.filter((d) => d.status === "ready" && d.topic)
      .map((d) => ({ ...d.topic, name: d.title || displayName(d.topic.name) })),
    [docs],
  );

  const questionsFor = useMemo(() => (topic) => {
    const qs = topic.questions || [];
    return filter === "all" ? qs : qs.filter((q) => q.d === filter);
  }, [filter]);

  const topicStats = useMemo(() => (topicId) => {
    const r = results[topicId];
    const topic = topics.find((t) => t.id === topicId);
    return {
      attempted: !!r,
      correct: r?.best.correct || 0,
      total: r?.best.total || topic?.questions.length || 0,
      mastered: !!r?.passed,
      line: scoreLine(r),
    };
  }, [results, topics]);

  const chosen = topics.filter((t) => !excluded.includes(t.id));

  const overall = useMemo(() => {
    const mastered = chosen.filter((t) => topicStats(t.id).mastered).length;
    return {
      topics: chosen.length, mastered,
      pct: chosen.length ? Math.round((mastered / chosen.length) * 100) : 0,
    };
  }, [chosen, topicStats]);

  const toggleTopic = (id) =>
    setExcluded((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  async function save(attempt, { quizId }) {
    try {
      const res = await saveQuizResult(attempt);
      if (alive.current) setResults((r) => ({ ...r, [quizId]: res.result }));
    } catch (e) {
      // Shown, not swallowed: a lost score is something the reader should know.
      if (alive.current) setError(`Your score was not saved: ${e.message}`);
      onToast?.("Score not saved");
    }
  }

  const startTopic = (topic) => {
    const questions = questionsFor(topic);
    if (!questions.length) return;
    setReview(null);
    setRun({ kind: "document", quizId: topic.id, title: topic.name, questions,
             need: topic.pass_marks?.[filter], key: Date.now() });
  };

  const startSection = (note, s) => {
    setReview(null);
    setRun({ kind: "section", quizId: s.quizId, title: s.title, group: note.title, questions: s.quiz,
             need: s.passMark, key: Date.now() });
  };

  function finish(r, { answers, correct, total }) {
    save({ quizId: r.quizId, kind: r.kind, answers, correct, total, title: r.title, group: r.group || null },
         { quizId: r.quizId });
    // A passing section attempt is what ticks the section in Revision notes;
    // the notes list reads it back from the saved results.
    if (correct >= (r.need ?? total)) {
      onToast?.(r.kind === "section" ? "Section complete" : "Topic passed");
    }
  }

  if (run) {
    const section = run.kind === "section";
    return (
      <QuizRunner
        key={run.key}
        title={run.title}
        questions={run.questions}
        onExit={() => setRun(null)}
        onFinish={(res) => finish(run, res)}
        need={run.need}
        passTitle={section ? "Section complete" : "Topic passed"}
        passText={section ? "The section is marked complete in your notes." : undefined}
        failText={section
          ? `Get ${run.need} of ${run.questions.length} right to complete the section. Another attempt keeps your best score.`
          : undefined}
        exitLabel={section ? "Back to quizzes" : "Back to topics"}
      />
    );
  }

  if (review) {
    return (
      <QuizReview
        title={review.section.title}
        questions={review.section.quiz}
        lastAnswers={results[review.section.quizId]?.last?.answers}
        onExit={() => setReview(null)}
        onRetake={() => startSection(review.note, review.section)}
      />
    );
  }

  if (loading) {
    return (
      <div className="study-wrap"><div className="study-col">
        <div className="qz-empty">Loading your quizzes…</div>
      </div></div>
    );
  }

  if (view === "home") {
    return (
      <div className="study-wrap"><div className="study-col">
        {error && <div className="nt-error">{error}</div>}
        <div className="qz-land">
          <div className="qz-land-banner">
            <div className="qz-land-copy">
              <h2 className="qz-land-heading">Choose a quiz</h2>
              <p className="qz-land-intro">Practise what you've studied, by document or by note section.</p>
            </div>
            <img className="qz-land-art" src={landingArt} alt="" aria-hidden="true" />
          </div>
          <LandingRow
            icon={<DocumentIcon />}
            title="By document"
            desc="A quiz for each document you upload, written when you ask for it."
            onClick={() => setView("documents")}
          />
          <LandingRow
            icon={<NoteIcon />}
            title="From notes"
            desc="The short quiz that comes with each revision-note section."
            onClick={() => setView("notes")}
          />
        </div>
      </div></div>
    );
  }

  const ringCircumference = 2 * Math.PI * RING_RADIUS;
  const back = <div className="qz-list-top"><BackLink onClick={() => setView("home")}>Quizzes</BackLink></div>;

  if (view === "notes") {
    return (
      <div className="study-wrap"><div className="study-col">
        {back}
        {error && <div className="nt-error">{error}</div>}
        <div className="qz-section-title">From notes</div>
        {noteQuizzes.length === 0 ? (
          <div className="qz-empty">
            <strong>No section quizzes yet</strong>
            <p style={{ marginTop: 6 }}>
              Each revision-note section comes with a short quiz once it has been written.
              Open a section in Revision notes to get one.
            </p>
          </div>
        ) : noteQuizzes.flatMap((note) => note.sections.map((s) => {
          const r = results[s.quizId];
          return (
            <QuizCard
              key={s.quizId}
              title={s.title}
              badge={r?.passed ? "Complete" : null}
              meta={note.title}
              progress={r ? Math.round((r.best.correct / r.best.total) * 100) : 0}
              line={`${s.quiz.length} questions`}
              sub={r ? scoreLine(r) : "Not attempted yet"}
              actions={(
                <>
                  <button className="btn-secondary" onClick={() => setReview({ note, section: s })}>Review</button>
                  <button className={`btn-start ${r ? "retry" : ""}`} onClick={() => startSection(note, s)}>
                    {r ? "Retake" : "Start"}
                  </button>
                </>
              )}
            />
          );
        }))}
      </div></div>
    );
  }

  return (
    <div className="study-wrap"><div className="study-col">
      {back}
      {error && <div className="nt-error">{error}</div>}
      <div className="qz-section-title">By document</div>

      {topics.length > 0 && (
        <>
          <div className="qz-head">
            <div className="qz-score">
              <div className="qz-ring">
                <svg viewBox={`0 0 ${RING_VIEWBOX} ${RING_VIEWBOX}`}>
                  <circle cx={RING_VIEWBOX / 2} cy={RING_VIEWBOX / 2} r={RING_RADIUS} fill="none" stroke="#EFEDE5" strokeWidth="7" />
                  <circle cx={RING_VIEWBOX / 2} cy={RING_VIEWBOX / 2} r={RING_RADIUS} fill="none" stroke="var(--accent)" strokeWidth="7"
                          strokeLinecap="round"
                          strokeDasharray={`${(overall.pct / 100) * ringCircumference} ${ringCircumference}`} />
                </svg>
                <div className="val">{overall.pct}%</div>
              </div>
              <div className="qz-score-body">
                <div className="lab">Progress</div>
                <div className="big">{overall.mastered} of {overall.topics} topics passed</div>
                <div className="sub">Two thirds correct on a topic marks it passed.</div>
              </div>
            </div>
          </div>

          <div className="qz-block-label">Topics you are studying</div>
          <div className="qz-topic-picker">
            {topics.map((t) => (
              <button key={t.id} className={`qz-pick ${excluded.includes(t.id) ? "" : "on"}`} onClick={() => toggleTopic(t.id)}>
                <span className="tick">✓</span>{t.name}
              </button>
            ))}
          </div>

          <div className="qz-block-label">
            Filter by difficulty
            <span className="hint">applies to study plan and topic quizzes</span>
          </div>
          <div className="study-toolbar-left">
            {DIFFICULTIES.map((f) => (
              <button key={f} className={`chip-btn ${filter === f ? "active" : ""}`} onClick={() => setFilter(f)}>
                {f === "all" ? "All levels" : f}
              </button>
            ))}
          </div>

          <div className="qz-block-label">Study plan</div>
          <div className="qz-plan">
            {chosen.map((t, idx) => {
              const s = topicStats(t.id);
              return (
                <button key={t.id} className={`qz-day ${s.mastered ? "done" : ""}`} onClick={() => startTopic(t)}>
                  <div className="d"><span>DAY {idx + 1}</span><span>{s.correct}/{s.total}</span></div>
                  <div className="t">{t.name}</div>
                  <div className="s">
                    <span className="qz-check">✓</span>
                    {s.mastered ? "Checkpoint cleared" : "Checkpoint quiz"}
                  </div>
                </button>
              );
            })}
          </div>
        </>
      )}

      {docs.length === 0 ? (
        <div className="qz-empty">
          <strong>No documents yet</strong>
          <p style={{ marginTop: 6 }}>{reason || "Upload a document and it will be listed here."}</p>
        </div>
      ) : docs.map((d) => (
        <DocumentCard
          key={d.document_id}
          doc={d}
          stats={topicStats(d.quiz_id)}
          questions={d.topic ? questionsFor(d.topic) : []}
          filter={filter}
          onGenerate={() => generate(d)}
          onStart={() => startTopic(topics.find((t) => t.id === d.quiz_id))}
        />
      ))}
    </div></div>
  );
}

function DocumentCard({ doc, stats, questions, filter, onGenerate, onStart }) {
  const ready = doc.status === "ready" && doc.topic;
  const failed = doc.status === "failed";
  const barPct = ready && stats.total ? Math.round((stats.correct / stats.total) * 100) : 0;
  const meta = [doc.week, `${doc.concepts} concepts`].filter(Boolean).join(" · ");

  let action;
  if (doc.status === "too_small") {
    action = <div className="sub">Too few concepts for a quiz</div>;
  } else if (doc.status === "generating") {
    action = <button className="btn-start" disabled>Generating…</button>;
  } else if (ready) {
    action = (
      <button className={`btn-start ${stats.attempted ? "retry" : ""}`} onClick={onStart} disabled={!questions.length}>
        {stats.mastered ? "Retry" : stats.attempted ? "Try again" : "Start"}
      </button>
    );
  } else if (failed) {
    action = <button className="btn-primary2" onClick={onGenerate}>Try again</button>;
  } else {
    action = <button className="btn-primary2" onClick={onGenerate}>Generate quiz</button>;
  }

  let line;
  let sub;
  if (ready) {
    line = `${questions.length} questions`;
    sub = stats.attempted ? stats.line : (filter === "all" ? "mixed difficulty" : filter);
  } else if (failed) {
    line = "Couldn't create this quiz right now. Try again in a minute.";
    sub = null;
  } else if (doc.status === "generating") {
    line = "Writing questions";
    sub = "This can take up to a minute.";
  } else {
    line = "Not generated yet";
    sub = "Generated only when you ask for it.";
  }

  return (
    <QuizCard
      title={doc.title || displayName(doc.filename)}
      badge={stats.mastered ? "Mastered" : null}
      meta={meta}
      progress={ready ? barPct : null}
      line={line}
      sub={sub}
      actions={action}
      tone={failed ? "failed" : ""}
    />
  );
}

/**
 * One quiz in a list. Both lists use it, so a document quiz and a section
 * quiz look the same and differ only in what they say and offer.
 */
function QuizCard({ title, badge, meta, progress, line, sub, actions, tone = "" }) {
  return (
    <div className={`qz-topic qz-card${tone ? ` ${tone}` : ""}`}>
      <div className="qz-topic-head">
        <div className="grow">
          <div className="qz-topic-name">
            {title}
            {badge && <span className="qz-mastered">{badge}</span>}
          </div>
          {meta && <div className="qz-topic-desc">{meta}</div>}
        </div>
      </div>
      {progress !== null && progress !== undefined && (
        <div className="qz-bar"><span style={{ width: `${progress}%` }} /></div>
      )}
      <div className="qz-list">
        <div className="qz-row">
          <div className="qname">
            {line}
            {sub && <div className="sub">{sub}</div>}
          </div>
          <div className="qz-card-actions">{actions}</div>
        </div>
      </div>
    </div>
  );
}

/** A landing row: icon tile, title and one line, and a round arrow. The whole row is the button. */
function LandingRow({ icon, title, desc, onClick }) {
  return (
    <button className="qz-land-row" onClick={onClick}>
      <span className="qz-land-icon" aria-hidden="true">{icon}</span>
      <span className="qz-land-text">
        <span className="qz-land-title">{title}</span>
        <span className="qz-land-desc">{desc}</span>
      </span>
      <span className="qz-land-go" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M5 12h13M13 6l6 6-6 6" />
        </svg>
      </span>
    </button>
  );
}

const iconProps = {
  width: 22, height: 22, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
  strokeWidth: 1.8, strokeLinecap: "round", strokeLinejoin: "round",
};

function DocumentIcon() {
  return (
    <svg {...iconProps}>
      <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
      <path d="M14 3v5h5M9 13h6M9 17h6" />
    </svg>
  );
}

function NoteIcon() {
  return (
    <svg {...iconProps}>
      <path d="M11 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5" />
      <path d="M17.5 3.5a2.1 2.1 0 0 1 3 3L12 15l-4 1 1-4z" />
    </svg>
  );
}
