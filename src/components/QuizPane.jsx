import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchQuiz, fetchNotes, regenerateGraphQuiz, saveQuizProgress, saveNoteQuizProgress,
} from "../services/api.js";
import QuizRunner, { BackLink, QuizReview } from "./QuizRunner.jsx";
import { displayName } from "../utils/documentTitle.js";
// Drawn for this project in the app's accent colour; see the SVG's header.
import landingArt from "../assets/quiz-landing.svg";

// Pass marks come from the server (quiz_service.pass_mark), never computed here.
const RING_RADIUS = 28;
const RING_VIEWBOX = 66;
const DIFFICULTIES = ["all", "easy", "medium", "hard"];

function normalizeProgress(progress) {
  if (!progress) return null;
  const rawAnswers = progress.answers || [];
  const answers = Array.isArray(rawAnswers)
    ? rawAnswers
    : Object.keys(rawAnswers)
      .sort((a, b) => Number(a) - Number(b))
      .map((key) => rawAnswers[key]);
  return {
    currentIndex: Number(progress.current_index ?? progress.currentIndex ?? 0),
    answers,
    correct: Number(progress.correct || 0),
    total: Number(progress.total || answers.length || 0),
    passed: Boolean(progress.passed),
    status: progress.status,
    updated_at: progress.updated_at,
    created_at: progress.created_at,
  };
}

function resultFromProgress(progress) {
  if (!progress) return null;
  return {
    best: { correct: progress.correct, total: progress.total },
    current_index: progress.currentIndex,
    last_at: progress.updated_at || progress.created_at,
    attempts: 1,
    passed: progress.passed,
    status: progress.status,
    last: { answers: progress.answers },
  };
}

/** The server's stored section quizzes in the shape the list renders. */
const toNoteQuizzes = ({ notes = [] }) => notes.map((n) => ({
  id: n.title,
  title: n.title,
  stale: n.status === "stale",
  sections: (n.sections || [])
    .map((s) => {
      const body = s.body || {};
      return {
        quizId: s.quiz_id || body.quiz_id,
        title: s.title,
        quiz: s.questions || body.quiz || [],
        passMark: s.pass_mark ?? body.quiz_pass_mark,
        sectionId: s.id,
        stale: n.status === "stale",
        progress: normalizeProgress(s.quiz_progress),
      };
    })
    .filter((s) => s.quizId && s.quiz?.length)
})).filter((n) => n.sections.length);

/** "Sep 30". Fixed to English month names so it reads the same everywhere. */
const shortDate = (seconds) => (seconds
  ? new Date(typeof seconds === "number" ? seconds * 1000 : seconds).toLocaleDateString("en-US", { month: "short", day: "numeric" })
  : "");

/** "3 of 5 correct · Sep 30", plus the attempt count only when there is more than one. */
export function scoreLine(r) {
  if (!r) return "";
  const parts = [`${r.best.correct} of ${r.best.total} correct`, shortDate(r.last_at)];
  if (r.attempts > 1) parts.push(`${r.attempts} attempts`);
  return parts.filter(Boolean).join(" · ");
}

/**
 * Quiz tab: document quizzes (generated only on request) and the quizzes written with note
 * sections, listed from the server's stored copy. Attempts are saved on the server.
 */
export default function QuizPane({ onToast, graphId }) {
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
  const [drafts, setDrafts] = useState({});
  const alive = useRef(true);

  const refreshDocs = useCallback(async () => {
    const data = await fetchQuiz(graphId);
    if (!alive.current) return;
    const topics = data.topics || [];
    const saved = data.resume;
    const savedAnswers = saved?.answers
      ? (Array.isArray(saved.answers)
        ? saved.answers
        : Object.keys(saved.answers)
          .sort((a, b) => Number(a) - Number(b))
          .map((key) => saved.answers[key]))
      : [];
    const resume = saved ? {
      currentIndex: Number(saved.current_index || 0),
      answers: savedAnswers,
      correct: Number(saved.correct || 0),
    } : null;
    setDocs(topics.map((topic) => ({
      document_id: topic.id,
      quiz_id: topic.id,
      title: topic.name,
      status: topic.stale ? "stale" : (topic.status || "ready"),
      stale: Boolean(topic.stale),
      week: `Version ${topic.day}`,
      topic,
      resume,
    })));
    if (data.result && topics[0]?.id) {
      setResults({
        [topics[0].id]: {
          best: { correct: data.result.correct || 0, total: data.result.total || 0 },
          current_index: data.result.current_index || 0,
          last_at: data.result.updated_at || data.result.created_at,
          attempts: 1,
          passed: Boolean(data.result.passed),
        },
      });
    }
    setReason(data.reason || null);
  }, [graphId]);

  useEffect(() => {
    alive.current = true;
    (async () => {
      const [quiz, notes] = await Promise.allSettled([refreshDocs(), fetchNotes(graphId)]);
      if (!alive.current) return;
      if (quiz.status === "rejected") setReason(quiz.reason?.message || "Could not load quizzes.");
      if (notes.status === "fulfilled") setNoteQuizzes(toNoteQuizzes(notes.value));
      setLoading(false);
    })();
    return () => { alive.current = false; };
  }, [refreshDocs, graphId]);

  const topics = useMemo(
    () => docs.filter((d) => (d.status === "ready" || d.status === "stale") && d.topic && d.topic.questions?.length)
      .map((d) => ({ ...d.topic, name: d.title || displayName(d.topic.name), resume: d.resume })),
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
      answered: r?.current_index || 0,
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

  async function save(attempt, { quizId, sectionId, kind }, completed) {
    try {
      const answers = Object.fromEntries((attempt.answers || []).map((answer, index) => [index, answer]));
      const saveFn = kind === "section" ? saveNoteQuizProgress : saveQuizProgress;
      const res = await saveFn(
        graphId,
        kind === "section" ? sectionId : quizId,
        attempt.answers?.length || 0,
        answers,
        attempt.correct,
        attempt.total,
        attempt.correct * 10,
        completed,
      );
      if (alive.current) {
        setResults((r) => ({
          ...r,
          [quizId]: {
            best: { correct: res.result.correct, total: res.result.total },
            current_index: res.result.current_index || attempt.answers?.length || 0,
            last_at: res.result.updated_at || res.result.created_at,
            attempts: 1,
            passed: Boolean(res.passed),
            status: res.result.status,
            last: { answers: attempt.answers },
          },
        }));
      }
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
             need: topic.pass_marks?.[filter], key: Date.now(), resume: drafts[topic.id] || topic.resume });
  };

  const startSection = (note, s) => {
    setReview(null);
    setRun({ kind: "section", quizId: s.quizId, sectionId: s.sectionId, title: s.title, group: note.title, questions: s.quiz,
             need: s.passMark, key: Date.now(), resume: drafts[s.quizId] || s.progress });
  };

  function finish(r, { answers, correct, total }) {
    save({ quizId: r.quizId, kind: r.kind, answers, correct, total, title: r.title, group: r.group || null },
         { quizId: r.quizId, sectionId: r.sectionId, kind: r.kind }, true);
    setDrafts((current) => {
      const next = { ...current };
      delete next[r.quizId];
      return next;
    });
    // A passing section attempt is what ticks the section in Revision notes;
    // the notes list reads it back from the saved results.
    if (correct >= (r.need ?? total)) {
      onToast?.(r.kind === "section" ? "Section complete" : "Topic passed");
    }
  }

  async function regenerateGraph() {
    setError("");
    try {
      await regenerateGraphQuiz(graphId);
      await refreshDocs();
    } catch (e) {
      setError(e.message || "Could not regenerate this quiz.");
    }
  }

  function progress(r, { answers, correct, total, currentIndex }) {
    const draft = { currentIndex, answers, correct };
    setDrafts((current) => ({ ...current, [r.quizId]: draft }));
    save({ ...draft, total }, { quizId: r.quizId, sectionId: r.sectionId, kind: r.kind }, false);
  }

  if (run) {
    const section = run.kind === "section";
    return (
      <QuizRunner
        key={run.key}
        title={run.title}
        questions={run.questions}
        initialProgress={run.resume}
        onProgress={(progressState) => progress(run, progressState)}
        onExit={(progress) => {
          if (!progress.done && progress.answers.length) {
            const draft = {
              currentIndex: progress.i,
              answers: progress.answers,
              correct: progress.correct,
            };
            setDrafts((current) => ({ ...current, [run.quizId]: draft }));
            save(
              { ...progress, quizId: run.quizId, total: run.questions.length },
              { quizId: run.quizId, sectionId: run.sectionId, kind: run.kind },
              false,
            );
          }
          setRun(null);
        }}
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
            title="By graph version"
            desc="A quiz for the latest version of this graph."
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
          const r = results[s.quizId] || resultFromProgress(s.progress);
          const inProgress = r?.status === "in_progress";
          const attempted = r?.current_index || 0;
          const stale = note.stale || s.stale;
          return (
            <QuizCard
              key={s.quizId}
              title={s.title}
              badge={stale ? "Stale" : r?.passed ? "Complete" : null}
              badgeTone={stale ? "stale" : ""}
              meta={note.title}
              progress={stale ? null : r ? Math.round((attempted / s.quiz.length) * 100) : 0}
              line={`${s.quiz.length} questions`}
              sub={stale
                ? "Regenerate the notes before taking this quiz."
                : r ? `${attempted}/${s.quiz.length} questions attempted · ${scoreLine(r)}` : "Not attempted yet"}
              actions={stale ? (
                <button className="btn-secondary" disabled>Regenerate notes first</button>
              ) : (
                <>
                  <button className="btn-secondary" onClick={() => setReview({ note, section: s })}>Review</button>
                  <button className={`btn-start ${r && !inProgress ? "retry" : ""}`} onClick={() => startSection(note, s)}>
                    {inProgress ? "Continue" : r ? "Retake" : "Start"}
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
      <div className="qz-section-title">By graph version</div>

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
                <div className="d"><span>DAY {idx + 1}</span><span>{s.answered}/{s.total}</span></div>
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
          onGenerate={regenerateGraph}
          onStart={() => startTopic(topics.find((t) => t.id === d.quiz_id))}
        />
      ))}
    </div></div>
  );
}

function DocumentCard({ doc, stats, questions, filter, onGenerate, onStart }) {
  const stale = doc.status === "stale" || doc.topic?.stale;
  const ready = (doc.status === "ready" || stale) && doc.topic;
  const failed = doc.status === "failed";
  const barPct = ready && stats.total ? Math.round((stats.correct / stats.total) * 100) : 0;
  const meta = [doc.week, doc.concepts ? `${doc.concepts} concepts` : null].filter(Boolean).join(" · ");

  let action;
  if (doc.status === "too_small") {
    action = <div className="sub">Too few concepts for a quiz</div>;
  } else if (doc.status === "generating") {
    action = <button className="btn-start" disabled>Generating…</button>;
  } else if (stale) {
    action = <button className="btn-primary2" onClick={onGenerate}>Regenerate quiz</button>;
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
  if (stale) {
    line = `${questions.length} questions`;
    sub = "This quiz belongs to an older graph version.";
  } else if (ready) {
    line = `${questions.length} questions`;
    sub = stats.attempted
      ? `${stats.answered}/${stats.total} questions attempted · ${stats.line}`
      : (filter === "all" ? "mixed difficulty" : filter);
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
      badge={stale ? "Stale" : stats.mastered ? "Mastered" : null}
      badgeTone={stale ? "stale" : ""}
      meta={meta}
      progress={ready ? barPct : null}
      line={line}
      sub={sub}
      actions={action}
      tone={failed ? "failed" : ""}
    />
  );
}

/** One quiz in a list; document and section quizzes share it. */
function QuizCard({ title, badge, badgeTone = "", meta, progress, line, sub, actions, tone = "" }) {
  return (
    <div className={`qz-topic qz-card${tone ? ` ${tone}` : ""}`}>
      <div className="qz-topic-head">
        <div className="grow">
          <div className="qz-topic-name">
            {title}
            {badge && <span className={`qz-mastered${badgeTone ? ` ${badgeTone}` : ""}`}>{badge}</span>}
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
