import React, { useEffect, useMemo, useState } from 'react'
import { fetchQuiz, saveQuizResult } from '../services/api.js'

/**
 * Topic-based quiz system, ported from the prototype.
 *
 * The model is the prototype's and the details matter:
 *  - questions are grouped into TOPICS the student picks
 *  - progress is counted per TOPIC, not per quiz
 *  - points are easy 10 / medium 20 / hard 30
 *  - a quiz passes at two thirds correct, and a re-attempt keeps the best score
 *  - a topic is mastered when every quiz in it has been passed
 *  - the study plan gives each day one topic and a checkpoint quiz
 *
 * Questions come from GET /api/quiz, which generates them from the concepts
 * extracted out of the uploaded documents — so they are about the student's own
 * material, not a fixed sample set. Uploading a new document regenerates them.
 *
 * Attempts are posted to /api/quiz/results, which keeps the best score.
 */

const PTS = { easy: 10, medium: 20, hard: 30 }

const maxPointsFor = (q) => q.questions.reduce((s, x) => s + (PTS[x.d] || 0), 0)

export default function QuizPane({ onToast }) {
  const [topics, setTopics] = useState([])
  const [allQuizzes, setAllQuizzes] = useState([])
  const [loading, setLoading] = useState(true)
  const [reason, setReason] = useState(null)
  const [picked, setPicked] = useState([])
  const [results, setResults] = useState({})     // quizId -> {correct,total,points,passed}
  const [filter, setFilter] = useState('all')
  const [run, setRun] = useState(null)           // active attempt

  const quizzesFor = useMemo(
    () => (id) => allQuizzes.filter((q) => q.topic === id),
    [allQuizzes],
  )

  // Generated from the uploaded documents, so this refetches whenever the
  // pane is opened rather than caching a stale set.
  useEffect(() => {
    let alive = true
    // The backend may call an LLM to write these. Bound the wait so a slow or
    // rate-limited provider shows a message instead of a permanent spinner.
    const deadline = setTimeout(() => {
      if (alive) {
        setLoading(false)
        setReason('Question generation is taking longer than expected. '
                + 'The backend may be rate-limited — try again in a moment.')
      }
    }, 75000)
    ;(async () => {
      try {
        const data = await fetchQuiz()
        if (!alive) return
        setTopics(data.topics || [])
        setAllQuizzes(data.quizzes || [])
        setPicked((data.topics || []).map((t) => t.id))
        setReason(data.reason || null)
      } catch (e) {
        if (alive) setReason(e.message)
      } finally {
        clearTimeout(deadline)
        if (alive) setLoading(false)
      }
    })()
    return () => { alive = false; clearTimeout(deadline) }
  }, [])

  const topicStats = useMemo(() => (id) => {
    const qs = quizzesFor(id)
    const done = qs.filter((q) => results[q.id]?.passed).length
    const pts = qs.reduce((s, q) => s + (results[q.id]?.points || 0), 0)
    const max = qs.reduce((s, q) => s + maxPointsFor(q), 0)
    return { total: qs.length, done, pts, max, mastered: done === qs.length && qs.length > 0 }
  }, [results, quizzesFor])

  const overall = useMemo(() => {
    const chosen = topics.filter((t) => picked.includes(t.id))
    const mastered = chosen.filter((t) => topicStats(t.id).mastered).length
    const pts = chosen.reduce((s, t) => s + topicStats(t.id).pts, 0)
    const max = chosen.reduce((s, t) => s + topicStats(t.id).max, 0)
    return {
      topics: chosen.length, mastered, pts, max,
      pct: chosen.length ? Math.round((mastered / chosen.length) * 100) : 0,
    }
  }, [picked, topics, topicStats])

  const toggleTopic = (id) =>
    setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]))

  const startQuiz = (quiz) =>
    setRun({ quiz, i: 0, picked: null, checked: false, correct: 0, points: 0, done: false })

  function pickChoice(i) {
    setRun((r) => (r.checked ? r : { ...r, picked: i }))
  }

  function checkAnswer() {
    setRun((r) => {
      if (r.picked === null) return r
      const q = r.quiz.questions[r.i]
      const right = r.picked === q.a
      return {
        ...r,
        checked: true,
        correct: r.correct + (right ? 1 : 0),
        points: r.points + (right ? (PTS[q.d] || 0) : 0),
      }
    })
  }

  function nextQuestion() {
    setRun((r) => {
      if (r.i + 1 < r.quiz.questions.length) {
        return { ...r, i: r.i + 1, picked: null, checked: false }
      }
      const total = r.quiz.questions.length
      const passed = r.correct / total >= 2 / 3
      setResults((prevAll) => {
        const prev = prevAll[r.quiz.id]
        // a re-attempt keeps the better score
        if (prev && prev.points >= r.points) return prevAll
        return { ...prevAll, [r.quiz.id]: { correct: r.correct, total, points: r.points, passed } }
      })
      saveQuizResult(r.quiz.id, r.correct, total, r.points).catch(() => {
        /* the score still shows locally; a failed save is not worth blocking on */
      })
      if (passed && onToast) onToast('Quiz passed')
      return { ...r, done: true }
    })
  }

  /* ---------------- runner ---------------- */
  if (run && !run.done) {
    const { quiz, i } = run
    const topic = topics.find((t) => t.id === quiz.topic)
    const q = quiz.questions[i]
    const pct = Math.round((i / quiz.questions.length) * 100)
    const ok = run.picked === q.a

    return (
      <div className="study-wrap"><div className="study-col">
        <div className="qz-run-top">
          <button className="qz-back" onClick={() => setRun(null)}>← {topic?.name}</button>
          <span className="qz-run-prog">Question {i + 1} of {quiz.questions.length}</span>
        </div>
        <div className="qz-run-bar"><span style={{ width: `${pct}%` }} /></div>

        <div className="qz-run-card">
          <span className={`diff-tag ${q.d}`}>{q.d} · {PTS[q.d]} pts</span>
          <div className="qz-run-q">{q.q}</div>

          {q.o.map((text, idx) => {
            let cls = ''
            if (run.checked) {
              if (idx === q.a) cls = 'right'
              else if (idx === run.picked) cls = 'wrong'
            } else if (idx === run.picked) cls = 'sel'
            return (
              <button key={idx} className={`qz-choice ${cls}`} disabled={run.checked}
                      onClick={() => pickChoice(idx)}>
                <span className="radio">✓</span>{text}
              </button>
            )
          })}

          {run.checked && (
            <div className={`qz-feedback ${ok ? '' : 'miss'}`}>
              <b>{ok ? 'Correct.' : 'Not quite.'}</b> {q.why}
            </div>
          )}

          <div className="qz-run-foot">
            {run.checked ? (
              <button className="btn-primary2" onClick={nextQuestion}>
                {i + 1 < quiz.questions.length ? 'Next question' : 'See results'}
              </button>
            ) : (
              <button className="btn-primary2" onClick={checkAnswer}
                      disabled={run.picked === null}
                      style={run.picked === null ? { opacity: 0.45, cursor: 'default' } : undefined}>
                Check answer
              </button>
            )}
          </div>
        </div>
      </div></div>
    )
  }

  /* ---------------- result ---------------- */
  if (run && run.done) {
    const total = run.quiz.questions.length
    const passed = run.correct / total >= 2 / 3
    const s = topicStats(run.quiz.topic)
    const remaining = quizzesFor(run.quiz.topic).filter((q) => !results[q.id]?.passed)

    return (
      <div className="study-wrap"><div className="study-col">
        <div className="qz-result">
          <div className="medal">{passed ? '🏆' : '📚'}</div>
          <h3>{passed ? 'Quiz passed' : 'Not quite there'}</h3>
          <p>
            {passed
              ? 'That material is confirmed covered — it now counts towards the topic.'
              : 'You need two thirds correct to clear this quiz. Another attempt keeps your best score.'}
          </p>
          <div className="qz-result-stats">
            <div><div className="n">{run.correct}/{total}</div><div className="l">Correct</div></div>
            <div><div className="n">{run.points}</div><div className="l">Points</div></div>
            <div><div className="n">{s.done}/{s.total}</div><div className="l">Topic quizzes</div></div>
          </div>
          <div className="qz-result-actions">
            <button className="btn-primary2" onClick={() => setRun(null)}>Back to topics</button>
            {!passed && (
              <button className="btn-ghost2" onClick={() => startQuiz(run.quiz)}>Try again</button>
            )}
            {passed && remaining.length > 0 && (
              <button className="btn-ghost2" onClick={() => startQuiz(remaining[0])}>
                Next quiz in this topic
              </button>
            )}
          </div>
        </div>
      </div></div>
    )
  }

  /* ---------------- overview ---------------- */
  if (loading) {
    return (
      <div className="study-wrap"><div className="study-col">
        <div className="qz-empty">
          Building questions from your documents…
          <p style={{ marginTop: 6, fontSize: 12.5, opacity: 0.75 }}>
            First run calls the model once per week of material, so this can
            take up to a minute. Later visits are instant.
          </p>
        </div>
      </div></div>
    )
  }

  if (!allQuizzes.length) {
    return (
      <div className="study-wrap"><div className="study-col">
        <div className="qz-empty">
          <strong>No questions yet</strong>
          <p style={{ marginTop: 6 }}>
            {reason || 'Upload a document and StudyGraph will write questions from it.'}
          </p>
        </div>
      </div></div>
    )
  }

  const C = 2 * Math.PI * 28
  const chosen = topics.filter((t) => picked.includes(t.id))

  return (
    <div className="study-wrap"><div className="study-col">
      {/* progress ring + headline numbers */}
      <div className="qz-head">
        <div className="qz-score">
          <div className="qz-score-body">
            <svg className="qz-ring" viewBox="0 0 68 68">
              <circle cx="34" cy="34" r="28" fill="none" stroke="#EFEDE5" strokeWidth="7" />
              <circle cx="34" cy="34" r="28" fill="none" stroke="var(--accent)" strokeWidth="7"
                      strokeLinecap="round" transform="rotate(-90 34 34)"
                      strokeDasharray={`${(overall.pct / 100) * C} ${C}`} />
            </svg>
            <div>
              <div className="qz-topic-name">
                {overall.mastered} of {overall.topics} topics mastered
              </div>
              <div className="qz-topic-desc">
                A topic counts once every quiz inside it has been passed.
              </div>
            </div>
          </div>
        </div>
        <div className="qz-points">
          <div className="n mono">{overall.pts}</div>
          <div className="l">of {overall.max} points</div>
        </div>
      </div>

      {/* which topics to study */}
      <div className="qz-block-label">Topics you are studying</div>
      <div className="qz-topic-picker">
        {topics.map((t) => (
          <button key={t.id} className={`qz-pick ${picked.includes(t.id) ? 'on' : ''}`}
                  onClick={() => toggleTopic(t.id)}>
            <span className="tick">✓</span>{t.name}
          </button>
        ))}
      </div>

      {/* the study plan, one topic per day */}
      <div className="qz-block-label">Study plan</div>
      <div className="qz-plan">
        {chosen.map((t) => {
          const s = topicStats(t.id)
          return (
            <button key={t.id} className={`qz-day ${s.mastered ? 'done' : ''}`}
                    onClick={() => document.getElementById(`topic-${t.id}`)
                      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
              <div className="d"><span>DAY {t.day}</span><span>{s.done}/{s.total}</span></div>
              <div className="t">{t.name}</div>
              <div className="s">
                <span className="qz-check">✓</span>
                {s.mastered ? 'Checkpoint cleared' : 'Checkpoint quiz'}
              </div>
            </button>
          )
        })}
      </div>

      {/* difficulty filter */}
      <div className="qz-filters">
        {['all', 'easy', 'medium', 'hard'].map((f) => (
          <button key={f} className={`qz-filter ${filter === f ? 'on' : ''}`}
                  onClick={() => setFilter(f)}>
            {f === 'all' ? 'All levels' : f}
          </button>
        ))}
      </div>

      {/* the quizzes themselves */}
      {chosen.map((t) => {
        const s = topicStats(t.id)
        const rows = quizzesFor(t.id).filter((q) => filter === 'all' || q.difficulty === filter)
        return (
          <section className="qz-topic" id={`topic-${t.id}`} key={t.id}>
            <div className="qz-topic-head">
              <div>
                <h3>{t.name} {s.mastered && <span className="qz-mastered">Mastered</span>}</h3>
                <p>{t.desc}</p>
              </div>
              <div className="qz-topic-meta mono">{s.pts}/{s.max} pts</div>
            </div>

            {rows.length === 0 ? (
              <div className="qz-empty">No {filter} quizzes in this topic.</div>
            ) : <div className="qz-list">{rows.map((q) => {
              const r = results[q.id]
              return (
                <div className="qz-row" key={q.id}>
                  <span className={`diff-tag ${q.difficulty}`}>{q.difficulty}</span>
                  <span className="qz-topic-name">{q.title}</span>
                  <span className="qz-topic-desc mono">
                    {r ? `${r.correct}/${r.total} · ${r.points} pts` : `${q.questions.length} questions`}
                  </span>
                  <button className="btn-ghost2" onClick={() => startQuiz(q)}>
                    {r?.passed ? 'Retry' : r ? 'Try again' : 'Start'}
                  </button>
                </div>
              )
            })}</div>}
          </section>
        )
      })}

      {chosen.length === 0 && (
        <div className="qz-empty">Pick at least one topic to see its quizzes.</div>
      )}
    </div></div>
  )
}
