import React, { useEffect, useMemo, useState } from "react";
import { fetchQuiz, saveQuizProgress } from "../services/api.js";

const PTS = { easy: 10, medium: 20, hard: 30 };
const MASTERY_THRESHOLD = 2 / 3;
const LOAD_TIMEOUT_MS = 75000;
const RING_RADIUS = 28;
const RING_VIEWBOX = 66;
const DIFFICULTIES = ["all", "easy", "medium", "hard"];

const maxPointsFor = (questions = []) =>
  questions.reduce((s, q) => s + (PTS[q.d] || 0), 0);

export default function QuizPane({ onToast, graphId }) {
  const [topics, setTopics] = useState([]);
  const [loading, setLoading] = useState(true);
  const [reason, setReason] = useState(null);
  const [picked, setPicked] = useState([]);
  const [results, setResults] = useState({}); // topicId -> {correct,total,points,passed}
  const [filter, setFilter] = useState("all");
  const [run, setRun] = useState(null);
  const [resume, setResume] = useState(null);

  useEffect(() => {
    let alive = true;
    const deadline = setTimeout(() => {
      if (alive) {
        setLoading(false);
        setReason(
          "Question generation is taking longer than expected. Try again in a moment.",
        );
      }
    }, LOAD_TIMEOUT_MS);
    (async () => {
      try {
        const data = await fetchQuiz(graphId);
        if (!alive) return;
        setTopics(data.topics || []);
        setPicked((data.topics || []).map((t) => t.id));
        setReason(data.reason || null);
        if (data.result && data.topics?.[0]) {
          setResults({
            [data.topics[0].id]: {
              correct: data.result.correct || 0,
              total: data.result.total || data.topics[0].questions.length,
              points: data.result.points || 0,
              passed: !!data.result.passed,
            },
          });
        }
        if (data.resume) {
          setResume(data.resume);
          const topic = (data.topics || []).find((t) => t.id === data.resume.quiz_set_id);
          if (topic) {
            const answers = data.resume.answers || {};
            const resumeIndex = data.resume.current_index || 0;
            const resumeAnswers = data.resume.answers || {};
            const hasCurrentAnswer = Object.prototype.hasOwnProperty.call(resumeAnswers, resumeIndex);
            setRun({ topic, questions: topic.questions, i: resumeIndex,
              picked: hasCurrentAnswer ? resumeAnswers[resumeIndex] : null,
              checked: hasCurrentAnswer, correct: data.resume.correct || 0,
              points: data.resume.points || 0, answers: resumeAnswers, done: false });
          }
        }
      } catch (e) {
        if (alive) setReason(e.message);
      } finally {
        clearTimeout(deadline);
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
      clearTimeout(deadline);
    };
  }, [graphId]);

  const questionsFor = useMemo(
    () => (topic) => {
      const qs = topic.questions || [];
      return filter === "all" ? qs : qs.filter((q) => q.d === filter);
    },
    [filter],
  );

  const topicStats = useMemo(
    () => (topicId) => {
      const r = results[topicId];
      const topic = topics.find((t) => t.id === topicId);
      const max = topic ? maxPointsFor(topic.questions) : 0;
      return {
        attempted: !!r,
        correct: r?.correct || 0,
        total: r?.total || topic?.questions.length || 0,
        pts: r?.points || 0,
        max,
        mastered: !!r?.passed,
      };
    },
    [results, topics],
  );

  const overall = useMemo(() => {
    const chosen = topics.filter((t) => picked.includes(t.id));
    const mastered = chosen.filter((t) => topicStats(t.id).mastered).length;
    const pts = chosen.reduce((s, t) => s + topicStats(t.id).pts, 0);
    const max = chosen.reduce((s, t) => s + topicStats(t.id).max, 0);
    return {
      topics: chosen.length,
      mastered,
      pts,
      max,
      pct: chosen.length ? Math.round((mastered / chosen.length) * 100) : 0,
    };
  }, [picked, topics, topicStats]);

  const toggleTopic = (id) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  const startQuiz = (topic) => {
    const questions = questionsFor(topic);
    if (!questions.length) return;
    const saved = resume && resume.quiz_set_id === topic.id ? resume : null;
    const savedIndex = saved?.current_index || 0;
    const savedAnswers = saved?.answers || {};
    const hasSavedAnswer = Object.prototype.hasOwnProperty.call(savedAnswers, savedIndex);
    setRun({
      topic,
      questions,
      i: savedIndex,
      picked: hasSavedAnswer ? savedAnswers[savedIndex] : null,
      checked: hasSavedAnswer,
      correct: saved?.correct || 0,
      points: saved?.points || 0,
      answers: savedAnswers,
      done: false,
    });
  };

  function pickChoice(i) {
    setRun((r) => (r.checked ? r : { ...r, picked: i }));
  }

  function checkAnswer() {
    if (!run || run.picked === null || run.checked) return;
    const q = run.questions[run.i];
    const right = run.picked === q.a;
    const answers = { ...(run.answers || {}), [run.i]: run.picked };
    const correct = run.correct + (right ? 1 : 0);
    const points = run.points + (right ? PTS[q.d] || 0 : 0);
    setRun({ ...run, checked: true, correct, points, answers });
    setResults((all) => ({
      ...all,
      [run.topic.id]: { correct, total: run.questions.length, points, passed: correct / run.questions.length >= MASTERY_THRESHOLD },
    }));
    saveQuizProgress(graphId, run.topic.id, run.i, answers, correct, run.questions.length, points).catch(() => {});
  }

  function nextQuestion() {
    setRun((r) => {
      if (r.i + 1 < r.questions.length) {
        const next = { ...r, i: r.i + 1, picked: null, checked: false };
        saveQuizProgress(graphId, r.topic.id, next.i, r.answers || {}, r.correct, r.questions.length, r.points).catch(() => {});
        return next;
      }
      const total = r.questions.length;
      const passed = r.correct / total >= MASTERY_THRESHOLD;
      setResults((prevAll) => {
        const prev = prevAll[r.topic.id];
        if (prev && prev.points >= r.points) return prevAll;
        return {
          ...prevAll,
          [r.topic.id]: { correct: r.correct, total, points: r.points, passed },
        };
      });
      saveQuizProgress(graphId, r.topic.id, r.i, r.answers || {}, r.correct, total, r.points, true).catch(() => {});
      setResume(null);
      if (passed && onToast) onToast("Topic passed");
      return { ...r, done: true };
    });
  }

  async function leaveQuiz() {
    if (run && !run.done) {
      setResume({
        quiz_set_id: run.topic.id,
        current_index: run.i,
        answers: run.answers || {},
        correct: run.correct,
        points: run.points,
      });
      await saveQuizProgress(
        graphId,
        run.topic.id,
        run.i,
        run.answers || {},
        run.correct,
        run.questions.length,
        run.points,
      ).catch(() => {});
    }
    setRun(null);
  }

  if (run && !run.done) {
    const { topic, questions, i } = run;
    const q = questions[i];
    const pct = Math.round((i / questions.length) * 100);
    const ok = run.picked === q.a;

    return (
      <div className="study-wrap">
        <div className="study-col">
          <div className="qz-run-top">
            <button className="qz-back" onClick={leaveQuiz}>
              ← {topic.name}
            </button>
            <span className="qz-run-prog">
              Question {i + 1} of {questions.length}
            </span>
          </div>
          <div className="qz-run-bar">
            <span style={{ width: `${pct}%` }} />
          </div>

          <div className="qz-run-card">
            <span className={`diff-tag ${q.d}`}>
              {q.d} · {PTS[q.d]} pts
            </span>
            <div className="qz-run-q">{q.q}</div>

            {q.o.map((text, idx) => {
              let cls = "";
              if (run.checked) {
                if (idx === q.a) cls = "right";
                else if (idx === run.picked) cls = "wrong";
              } else if (idx === run.picked) cls = "sel";
              return (
                <button
                  key={idx}
                  className={`qz-choice ${cls}`}
                  disabled={run.checked}
                  onClick={() => pickChoice(idx)}
                >
                  <span className="radio">✓</span>
                  {text}
                </button>
              );
            })}

            {run.checked && (
              <div className={`qz-feedback ${ok ? "" : "miss"}`}>
                <b>{ok ? "Correct." : "Not quite."}</b> {q.why}
              </div>
            )}

            <div className="qz-run-foot">
              {run.checked ? (
                <button className="btn-primary2" onClick={nextQuestion}>
                  {i + 1 < questions.length ? "Next question" : "See results"}
                </button>
              ) : (
                <button
                  className="btn-primary2"
                  onClick={checkAnswer}
                  disabled={run.picked === null}
                  style={
                    run.picked === null
                      ? { opacity: 0.45, cursor: "default" }
                      : undefined
                  }
                >
                  Check answer
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (run && run.done) {
    const total = run.questions.length;
    const passed = run.correct / total >= MASTERY_THRESHOLD;

    return (
      <div className="study-wrap">
        <div className="study-col">
          <div className="qz-result">
            <div className="medal">{passed ? "🏆" : "📚"}</div>
            <h3>{passed ? "Topic passed" : "Not quite there"}</h3>
            <p>
              {passed
                ? "That material is confirmed covered."
                : "You need two thirds correct to pass. Another attempt keeps your best score."}
            </p>
            <div className="qz-result-stats">
              <div>
                <div className="n">
                  {run.correct}/{total}
                </div>
                <div className="l">Correct</div>
              </div>
              <div>
                <div className="n">{run.points}</div>
                <div className="l">Points</div>
              </div>
            </div>
            <div className="qz-result-actions">
              <button className="btn-primary2" onClick={() => setRun(null)}>
                Back to topics
              </button>
              <button
                className="btn-secondary"
                onClick={() => startQuiz(run.topic)}
              >
                Try again
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="study-wrap">
        <div className="study-col">
          <div className="qz-empty">
            Building questions from your documents…
            <p style={{ marginTop: 6, fontSize: 12.5, opacity: 0.75 }}>
              First run can take up to a minute. Later visits are instant.
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (!topics.length) {
    return (
      <div className="study-wrap">
        <div className="study-col">
          <div className="qz-empty">
            <strong>No questions yet</strong>
            <p style={{ marginTop: 6 }}>
              {reason ||
                "Upload a document and StudyGraph will write questions from it."}
            </p>
          </div>
        </div>
      </div>
    );
  }

  const ringCircumference = 2 * Math.PI * RING_RADIUS;
  const chosen = topics.filter((t) => picked.includes(t.id));

  return (
    <div className="study-wrap">
      <div className="study-col">
        <div className="qz-head">
          <div className="qz-score">
            <div className="qz-ring">
              <svg viewBox={`0 0 ${RING_VIEWBOX} ${RING_VIEWBOX}`}>
                <circle
                  cx={RING_VIEWBOX / 2}
                  cy={RING_VIEWBOX / 2}
                  r={RING_RADIUS}
                  fill="none"
                  stroke="#EFEDE5"
                  strokeWidth="7"
                />
                <circle
                  cx={RING_VIEWBOX / 2}
                  cy={RING_VIEWBOX / 2}
                  r={RING_RADIUS}
                  fill="none"
                  stroke="var(--accent)"
                  strokeWidth="7"
                  strokeLinecap="round"
                  strokeDasharray={`${(overall.pct / 100) * ringCircumference} ${ringCircumference}`}
                />
              </svg>
              <div className="val">{overall.pct}%</div>
            </div>
            <div className="qz-score-body">
              <div className="lab">Progress</div>
              <div className="big">
                {overall.mastered} of {overall.topics} topics passed
              </div>
              <div className="sub">
                Two thirds correct on a topic marks it passed.
              </div>
            </div>
          </div>
          <div className="qz-points">
            <div className="lab">Points</div>
            <div className="big">{overall.pts}</div>
            <div className="sub">of {overall.max} pts</div>
          </div>
        </div>

        <div className="qz-block-label">Topics you are studying</div>
        <div className="qz-topic-picker">
          {topics.map((t) => (
            <button
              key={t.id}
              className={`qz-pick ${picked.includes(t.id) ? "on" : ""}`}
              onClick={() => toggleTopic(t.id)}
            >
              <span className="tick">✓</span>
              {t.name}
            </button>
          ))}
        </div>

        <div className="qz-block-label">
          Filter by difficulty
          <span className="hint">applies to study plan and topic quizzes</span>
        </div>
        <div className="study-toolbar-left">
          {DIFFICULTIES.map((f) => (
            <button
              key={f}
              className={`chip-btn ${filter === f ? "active" : ""}`}
              onClick={() => setFilter(f)}
            >
              {f === "all" ? "All levels" : f}
            </button>
          ))}
        </div>

        <div className="qz-block-label">Study plan</div>
        <div className="qz-plan">
          {chosen.map((t, idx) => {
            const s = topicStats(t.id);
            return (
              <button
                key={t.id}
                className={`qz-day ${s.mastered ? "done" : ""}`}
                onClick={() => startQuiz(t)}
              >
                <div className="d">
                  <span>DAY {idx + 1}</span>
                  <span>
                    {s.correct}/{s.total}
                  </span>
                </div>
                <div className="t">{t.name}</div>
                <div className="s">
                  <span className="qz-check">✓</span>
                  {s.mastered ? "Checkpoint cleared" : "Checkpoint quiz"}
                </div>
              </button>
            );
          })}
        </div>

        {chosen.length === 0 && (
          <div className="qz-empty">
            Pick at least one topic to see its quiz.
          </div>
        )}

        {chosen.map((t) => {
          const s = topicStats(t.id);
          const barPct = s.max ? Math.round((s.pts / s.max) * 100) : 0;
          const questions = questionsFor(t);
          return (
            <div className="qz-topic" key={t.id}>
              <div className="qz-topic-head">
                <div className="grow">
                  <div className="qz-topic-name">
                    {t.name}
                    {s.mastered && (
                      <span className="qz-mastered">Mastered</span>
                    )}
                  </div>
                  <div className="qz-topic-desc">{t.desc}</div>
                </div>
                <div className="qz-topic-meta">
                  <div className="pts">{s.pts}</div>
                  <div className="of">of {s.max} pts</div>
                </div>
              </div>
              <div className="qz-bar">
                <span style={{ width: `${barPct}%` }} />
              </div>
              <div className="qz-list">
                <div className="qz-row">
                  <div className="qname">
                    {questions.length} questions
                    <div className="sub">
                      {filter === "all" ? "mixed difficulty" : filter}
                    </div>
                  </div>
                  {s.attempted && (
                    <div className="score">
                      {s.correct}/{s.total}
                    </div>
                  )}
                  <button
                    className={`btn-start ${s.attempted ? "retry" : ""}`}
                    onClick={() => startQuiz(t)}
                    disabled={!questions.length}
                  >
                    {s.mastered ? "Retry" : s.attempted ? "Try again" : "Start"}
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
