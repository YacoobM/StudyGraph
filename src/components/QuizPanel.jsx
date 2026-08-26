import React, { useState } from 'react'
import './ChatPanel.css'

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export default function QuizPanel({ onClose, embedded = false }) {
  const [quizType, setQuizType] = useState('relationship') // 'relationship' | 'ai'
  const [questions, setQuestions] = useState([])
  const [quizId, setQuizId] = useState(null)
  const [selected, setSelected] = useState({})
  const [results, setResults] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  const startQuiz = async () => {
    setBusy(true)
    setError(null)
    setResults(null)
    setSelected({})
    try {
      const res = await fetch(`${API_BASE_URL}/api/quiz?count=5&type=${quizType}`)
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Failed to generate quiz')
      }
      const data = await res.json()
      setQuizId(data.quiz_id)
      setQuestions(data.questions)
    } catch (e) {
      setError(e.message)
      setQuestions([])
    } finally {
      setBusy(false)
    }
  }

  const pickOption = (questionId, optionIndex) => {
    if (results) return
    setSelected((s) => ({ ...s, [questionId]: optionIndex }))
  }

  const submitQuiz = async () => {
    if (!quizId) return
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/api/quiz/${quizId}/submit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answers: selected }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.detail || 'Failed to submit quiz')
      }
      const data = await res.json()
      setResults(data)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const resultFor = (questionId) => results?.results.find((r) => r.question_id === questionId)

  const allAnswered = questions.length > 0 && questions.every((q) => selected[q.id] !== undefined)

  const showTypeSwitch = questions.length === 0 && !busy

  return (
    <div className={embedded ? 'chat-drawer chat-panel-embedded' : 'chat-drawer'}>
      <div className="chat-head">
        <div className="chat-head-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M9 11l3 3L22 4" />
            <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
          </svg>
          <span>Quiz</span>
        </div>
        {!embedded && <button className="chat-close" onClick={onClose}>×</button>}
      </div>

      <div className="chat-body">
        {showTypeSwitch && (
          <div className="bubble bubble-assistant">
            <div className="bubble-content">
              <div style={{ marginBottom: '10px' }}>
                Choose a quiz type, then generate.
              </div>
              <div style={{ display: 'flex', gap: '8px' }}>
                <button
                  type="button"
                  className={`btn ${quizType === 'relationship' ? 'btn-primary' : 'btn-ghost'}`}
                  onClick={() => setQuizType('relationship')}
                >
                  Relationship
                </button>
                <button
                  type="button"
                  className={`btn ${quizType === 'ai' ? 'btn-primary' : 'btn-ghost'}`}
                  onClick={() => setQuizType('ai')}
                >
                  AI Generated
                </button>
              </div>
              <div style={{ marginTop: '8px', fontSize: '0.8rem', color: 'var(--gv-text-secondary)' }}>
                {quizType === 'relationship'
                  ? 'Built instantly from the graph — tests how concepts connect.'
                  : 'Written by the model from your source material — takes a few seconds.'}
              </div>
            </div>
          </div>
        )}

        {error && (
          <div className="bubble bubble-assistant">
            <div className="bubble-content">{error}</div>
          </div>
        )}

        {busy && questions.length === 0 && (
          <div className="bubble bubble-assistant">
            <div className="typing">
              <span></span><span></span><span></span>
            </div>
          </div>
        )}

        {questions.map((q, i) => {
          const result = resultFor(q.id)
          return (
            <div key={q.id} className="bubble bubble-assistant">
              <div className="bubble-content">
                <strong>{i + 1}. {q.question}</strong>
                <div style={{ marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {q.options.map((opt, idx) => {
                    const isSelected = selected[q.id] === idx
                    const isCorrect = result && idx === result.correct_index
                    const isWrongPick = result && isSelected && !result.correct
                    return (
                      <button
                        key={idx}
                        type="button"
                        className="btn"
                        style={{
                          textAlign: 'left',
                          border: isSelected ? '2px solid #6366f1' : '1px solid var(--gv-border, #333)',
                          background: isCorrect ? 'rgba(34,197,94,0.15)' : isWrongPick ? 'rgba(239,68,68,0.15)' : 'transparent',
                        }}
                        onClick={() => pickOption(q.id, idx)}
                        disabled={!!results}
                      >
                        {opt}
                      </button>
                    )
                  })}
                </div>
                {result && (
                  <div style={{ marginTop: '8px', fontSize: '0.85rem', color: 'var(--gv-text-secondary)' }}>
                    {result.explanation || 'No explanation available.'}
                  </div>
                )}
              </div>
            </div>
          )
        })}

        {results && (
          <div className="bubble bubble-assistant">
            <div className="bubble-content">
              <strong>Score: {results.score} / {results.total}</strong>
            </div>
          </div>
        )}
      </div>

      <div className="chat-input-row">
        {questions.length === 0 ? (
          <button className="btn btn-primary chat-send" onClick={startQuiz} disabled={busy}>
            {busy ? 'Generating…' : 'Start Quiz'}
          </button>
        ) : results ? (
          <button className="btn btn-primary chat-send" onClick={startQuiz} disabled={busy}>
            New Quiz
          </button>
        ) : (
          <button className="btn btn-primary chat-send" onClick={submitQuiz} disabled={busy || !allAnswered}>
            Submit
          </button>
        )}
      </div>
    </div>
  )
}