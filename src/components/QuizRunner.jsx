import React, { useState } from "react";

/**
 * Takes one quiz question by question, then shows the score. Only the first try at each
 * question counts; `need` is the server's pass mark (every question without one).
 */

/** The back link used across the quiz views: arrow plus label, easy to hit. */
export function BackLink({ onClick, children }) {
  return (
    <button className="qz-back" onClick={onClick}>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M19 12H6M11 6l-6 6 6 6" />
      </svg>
      {children}
    </button>
  );
}

/** Result-screen marks, drawn in lines to match the app rather than emoji. */
function ResultIcon({ passed }) {
  return (
    <svg className="qz-result-icon" width="40" height="40" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"
         aria-hidden="true">
      {passed ? (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M8 12.5l2.6 2.6L16 9.6" />
        </>
      ) : (
        <>
          <path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H11v15H5.5A1.5 1.5 0 0 1 4 17.5z" />
          <path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H13v15h5.5a1.5 1.5 0 0 0 1.5-1.5z" />
        </>
      )}
    </svg>
  );
}

/** An option's marker: a dot while picked, a tick or cross only once checked. */
function Marker({ cls }) {
  let mark = null;
  if (cls === "right") mark = "✓";
  else if (cls === "wrong") mark = "✕";
  else if (cls === "sel") mark = <span className="qz-dot" />;
  return <span className="radio" aria-hidden="true">{mark}</span>;
}

// `wrong` is the option just checked and found wrong, or null; `tries`
// counts checks, so only the first one scores.
const freshQuestion = { picked: null, wrong: null, tries: 0, state: "asking", gotIt: false, nudge: false };
const fresh = () => ({ i: 0, ...freshQuestion, answers: [], correct: 0, done: false });

export default function QuizRunner({
  title, questions, onExit, onFinish, need: needProp,
  passTitle = "Topic passed", passText = "That material is confirmed covered.",
  failText, exitLabel = "Back to topics", backLabel, inline = false,
}) {
  const [run, setRun] = useState(fresh);
  const need = needProp ?? questions.length;

  const wrap = (children) => (inline
    ? <div className="qz-inline">{children}</div>
    : <div className="study-wrap"><div className="study-col">{children}</div></div>);

  const q = questions[run.i];

  function pickChoice(idx) {
    // After a miss the options stay live: a new pick clears the mark.
    setRun((r) => (r.state === "revealed" ? r
      : { ...r, picked: idx, wrong: null, state: "asking", nudge: false }));
  }

  function checkAnswer() {
    if (run.picked === null) {
      setRun((r) => ({ ...r, nudge: true }));
      return;
    }
    setRun((r) => {
      const right = r.picked === q.a;
      const first = r.tries === 0;
      return {
        ...r,
        answers: first ? [...r.answers, r.picked] : r.answers,
        correct: r.correct + (first && right ? 1 : 0),
        tries: r.tries + 1,
        wrong: right ? null : r.picked,
        state: right ? "revealed" : "wrong",
        gotIt: right,
      };
    });
  }

  const showAnswer = () => setRun((r) => ({ ...r, state: "revealed", gotIt: false }));

  function nextQuestion() {
    if (run.i + 1 < questions.length) {
      setRun((r) => ({ ...r, i: r.i + 1, ...freshQuestion }));
      return;
    }
    // Outside the state updater: an updater may run twice in StrictMode, and
    // this saves the attempt.
    onFinish?.({ answers: run.answers, correct: run.correct, total: questions.length });
    setRun((r) => ({ ...r, done: true }));
  }

  if (!run.done) {
    const pct = Math.round((run.i / questions.length) * 100);
    const revealed = run.state === "revealed";

    return wrap(
      <>
        {/* Inline, the host's button closes the quiz, so there is no second close control. */}
        <div className="qz-run-top">
          {inline ? <span /> : <BackLink onClick={onExit}>{backLabel || title}</BackLink>}
          <span className="qz-run-prog">Question {run.i + 1} of {questions.length}</span>
        </div>
        <div className="qz-run-bar"><span style={{ width: `${pct}%` }} /></div>

        <div className="qz-run-card">
          <span className={`diff-tag ${q.d}`}>{q.d}</span>
          <div className="qz-run-q">{q.q}</div>

          {run.nudge && <div className="qz-nudge" role="alert">Choose an answer first</div>}

          {q.o.map((text, idx) => {
            let cls = "";
            if (revealed && idx === q.a) cls = "right";
            else if (idx === run.wrong) cls = "wrong";
            else if (idx === run.picked) cls = "sel";
            return (
              <button key={idx} className={`qz-choice ${cls}`} disabled={revealed} onClick={() => pickChoice(idx)}>
                <Marker cls={cls} />{text}
              </button>
            );
          })}

          {revealed && (
            <div className="qz-feedback">
              <b>{run.gotIt ? "Correct." : "Answer."}</b> {q.why}
            </div>
          )}

          <div className="qz-run-foot">
            {!revealed && run.tries > 0 && (
              <button className="btn-secondary" onClick={showAnswer}>Show answer</button>
            )}
            {!revealed && run.state !== "wrong" && (
              <button className="btn-primary2" onClick={checkAnswer}>Check answer</button>
            )}
            {revealed && (
              <button className="btn-primary2" onClick={nextQuestion}>
                {run.i + 1 < questions.length ? "Next question" : "See results"}
              </button>
            )}
          </div>
        </div>
      </>
    );
  }

  const total = questions.length;
  const passed = run.correct >= need;
  return wrap(
    <div className="qz-result">
      <ResultIcon passed={passed} />
      <h3>{passed ? passTitle : "Not quite there"}</h3>
      <p>{passed ? passText : (failText || `Get ${need} of ${total} right to pass. Another attempt keeps your best score.`)}</p>
      <div className="qz-result-stats">
        <div><div className="n">{run.correct}/{total}</div><div className="l">Correct first try</div></div>
      </div>
      <div className="qz-result-actions">
        {!inline && <button className="btn-primary2" onClick={onExit}>{exitLabel}</button>}
        <button className={inline ? "btn-primary2" : "btn-secondary"} onClick={() => setRun(fresh())}>Try again</button>
      </div>
    </div>
  );
}

/** A finished quiz for reading: each question with its answer, explanation and last pick. */
export function QuizReview({ title, questions, lastAnswers, onExit, onRetake }) {
  return (
    <div className="study-wrap"><div className="study-col">
      <div className="qz-run-top">
        <BackLink onClick={onExit}>From notes</BackLink>
        <span className="qz-run-prog">{title}</span>
      </div>
      {questions.map((q, n) => {
        const mine = lastAnswers?.[n];
        const answered = mine !== undefined && mine !== null;
        return (
          <div className="qz-run-card qz-review-card" key={q.id || n}>
            <span className={`diff-tag ${q.d}`}>Question {n + 1} · {q.d}</span>
            <div className="qz-run-q">{q.q}</div>
            {q.o.map((text, idx) => {
              const cls = idx === q.a ? "right" : (answered && idx === mine ? "wrong" : "");
              return (
                <div key={idx} className={`qz-choice ${cls}`}>
                  <Marker cls={cls} />{text}
                  {answered && idx === mine && <span className="qz-yours">your last answer</span>}
                </div>
              );
            })}
            <div className="qz-feedback"><b>Why.</b> {q.why}</div>
          </div>
        );
      })}
      <div className="qz-result-actions">
        <button className="btn-primary2" onClick={onRetake}>Retake</button>
        <button className="btn-secondary" onClick={onExit}>Back</button>
      </div>
    </div></div>
  );
}
