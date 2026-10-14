import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import LandingGraphDemo from './LandingGraphDemo.jsx'
import './Landing.css'

/**
 * Landing page.
 *
 * Everything here is static product explanation plus the isolated graph
 * demonstration in LandingGraphDemo — no API calls, no auth state, no
 * production data.
 */

const STEPS = [
  {
    n: '01',
    title: 'Upload',
    body: 'Lecture notes, slides, PDFs and other learning materials.',
    icon: (
      <>
        <path d="M12 16V4" /><path d="m7 9 5-5 5 5" />
        <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
      </>
    ),
  },
  {
    n: '02',
    title: 'Understand',
    body: 'AI identifies the important concepts and the relationships between them.',
    icon: (
      <>
        <circle cx="12" cy="12" r="8" /><path d="M12 8v4l2.5 2.5" />
      </>
    ),
  },
  {
    n: '03',
    title: 'Connect',
    body: 'Related knowledge is joined into a single personal knowledge graph.',
    icon: (
      <>
        <circle cx="6" cy="7" r="2.5" /><circle cx="18" cy="8" r="2.5" />
        <circle cx="11" cy="17" r="2.5" />
        <path d="M8.2 8.2 9.6 15" /><path d="M16 10.1 12.8 15.4" /><path d="M8.5 7.3 15.5 7.7" />
      </>
    ),
  },
  {
    n: '04',
    title: 'Explore',
    body: 'Inspect a concept, follow its connections and ask questions about your own material.',
    icon: (
      <>
        <circle cx="11" cy="11" r="6.5" /><path d="m16 16 4 4" />
      </>
    ),
  },
]

const FEATURES = [
  {
    label: 'Explore',
    title: 'An interactive concept graph',
    body: 'Search your concepts, inspect definitions and relationships, and trace where ideas came from. Move through graph versions to see how your knowledge grows with each upload.',
    icon: (
      <>
        <circle cx="6" cy="7" r="2.5" /><circle cx="18" cy="6" r="2.5" />
        <circle cx="12" cy="18" r="2.5" /><circle cx="19" cy="17" r="2" />
        <path d="m8.4 6.8 7.1-.6M7.5 9.2l3.2 6.5m2.8.2 4.2 1M16.7 8.2l-3.4 7.5" />
      </>
    ),
  },
  {
    label: 'Ask',
    title: 'Answers grounded in your sources',
    body: 'Ask questions about the material in a graph. StudyGraph responds with source passages and page references, so you can check an answer against the documents it came from.',
    icon: (
      <>
        <path d="M21 15a4 4 0 0 1-4 4H7l-4 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4z" />
        <path d="M8 10h8M8 14h5" />
      </>
    ),
  },
  {
    label: 'Study',
    title: 'Study notes shaped around your material',
    body: 'Choose the documents you want to cover and generate structured notes from them. Keep them together or spread the material across a study plan of up to ten days.',
    icon: (
      <>
        <path d="M7 3.5h7l4 4V20a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 20V5a1.5 1.5 0 0 1 1-1.5Z" />
        <path d="M14 4v4h4M9 12h6M9 16h6" />
      </>
    ),
  },
  {
    label: 'Practice',
    title: 'Quizzes that save your progress',
    body: 'Practice with quizzes based on your documents and notes. Your answers and results are saved, so you can pick up where you left off and see which topics you have mastered.',
    icon: (
      <>
        <path d="M8 4.5h8M9 3h6a2 2 0 0 1 2 2v1H7V5a2 2 0 0 1 2-2Z" />
        <path d="M7 5H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-1" />
        <path d="m8 13 2.5 2.5L16 10" />
      </>
    ),
  },
]

function StepIcon({ children }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  )
}

export default function Landing() {
  const navigate = useNavigate()
  const processRef = useRef(null)
  const [revealed, setRevealed] = useState(false)

  const jumpTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })

  /* Single reveal for the process strip — a one-shot observer, not a scroll
     listener, so nothing stays attached after it fires. */
  useEffect(() => {
    const el = processRef.current
    if (!el) return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      setRevealed(true)
      return
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setRevealed(true)
          observer.disconnect()
        }
      },
      { threshold: 0.25 },
    )
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <div className="lp">
      <a className="lp-skip" href="#lp-main">Skip to content</a>

      <header className="lp-nav">
        <div className="lp-nav-inner">
          <button type="button" className="lp-wordmark" aria-label="StudyGraph home"
                  onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
            <svg className="lp-brand-mark" viewBox="0 0 30 30" fill="none" aria-hidden="true">
              <path d="M8 9.5 20.5 7M8.5 10.5l4.5 10M20 8l-6.5 12M8 10l14 11" />
              <circle cx="7.5" cy="9" r="3" />
              <circle cx="21.5" cy="6.5" r="3" />
              <circle cx="13.5" cy="22" r="3" />
              <circle cx="22.5" cy="21.5" r="3" />
            </svg>
            <span>StudyGraph</span>
          </button>

          <nav className="lp-nav-links" aria-label="Sections">
            <button type="button" onClick={() => jumpTo('lp-features')}>Features</button>
            <button type="button" onClick={() => jumpTo('lp-how')}>How It Works</button>
            <button type="button" onClick={() => jumpTo('lp-demo')}>Demo</button>
          </nav>

          <div className="lp-nav-actions">
            <button type="button" className="lp-btn lp-btn-quiet"
                    onClick={() => navigate('/login')}>Sign in</button>
            <button type="button" className="lp-btn lp-btn-solid"
                    onClick={() => navigate('/login')}>Get started <span aria-hidden="true">↗</span></button>
          </div>
        </div>
      </header>

      <main id="lp-main">
        {/* ---------------- hero ---------------- */}
        <section className="lp-hero">
          <div className="lp-hero-copy">
            <span className="lp-eyebrow">Your materials, connected</span>
            <h1>Your ideas, connected.</h1>
            <p>
              Turn lecture notes, slides, and readings into a living map of
              concepts you can explore, question, and build on.
            </p>
            <div className="lp-hero-actions">
              <button type="button" className="lp-btn lp-btn-solid lp-btn-lg"
                      onClick={() => navigate('/login')}>Build your graph</button>
              <button type="button" className="lp-btn lp-btn-outline lp-btn-lg"
                      onClick={() => jumpTo('lp-how')}>How it works <span aria-hidden="true">↘</span></button>
            </div>
          </div>

          <div className="lp-hero-visual" id="lp-demo">
            <LandingGraphDemo />
          </div>

          <div className="lp-file-types" aria-label="Supported file types">
            <span>Bring your study materials</span>
            <b>PDF</b><b>DOCX</b><b>PPTX</b><b>TXT</b><b>MD</b>
          </div>
        </section>

        {/* ---------------- process ---------------- */}
        <section className="lp-section" id="lp-how">
          <div className="lp-section-head">
            <span className="lp-eyebrow">How StudyGraph works</span>
            <h2>Four steps, from a folder of documents to a map you can think with.</h2>
          </div>

          <div className="lp-process-box">
          <ol className={`lp-process${revealed ? ' is-in' : ''}`} ref={processRef}>
            {STEPS.map((s, i) => (
              <li className="lp-step" key={s.n} style={{ '--i': i }}>
                <div className="lp-step-marker">
                  <span className="lp-step-icon"><StepIcon>{s.icon}</StepIcon></span>
                  <span className="lp-step-num">{s.n}</span>
                </div>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
              </li>
            ))}
          </ol>
          </div>
        </section>

                {/* ---------------- finale: one gradient behind features, CTA and footer ---------------- */}
        <div className="lp-finale">
          <section className="lp-section lp-section-alt" id="lp-features">
            <div className="lp-feature-card">
              <div className="lp-section-head">
                <span className="lp-eyebrow">Inside StudyGraph</span>
                <h2>From your documents to your next study session.</h2>
                <p>
                  One workspace to explore your material, make sense of it, and
                  practice what you have learned.
                </p>
              </div>

              <div className="lp-benefits">
                {FEATURES.map((feature) => (
                  <article className="lp-benefit" key={feature.title}>
                    <div className="lp-feature-top">
                      <span className="lp-feature-icon"><StepIcon>{feature.icon}</StepIcon></span>
                      <span className="lp-feature-label">{feature.label}</span>
                    </div>
                    <h3>{feature.title}</h3>
                    <p>{feature.body}</p>
                  </article>
                ))}
              </div>
            </div>
          </section>

          <section className="lp-cta">
            <h2>Build your knowledge graph</h2>
            <p>Turn your course materials into connected knowledge.</p>
            <button type="button" className="lp-btn lp-btn-solid lp-btn-lg"
                    onClick={() => navigate('/login')}>Upload your first document</button>
          </section>

          <footer className="lp-foot">
            <div className="lp-foot-line">
              <span className="lp-wordmark lp-wordmark-sm">StudyGraph</span>
              <span className="lp-foot-sep" aria-hidden="true">·</span>
              <span>A knowledge graph for the way you actually study.</span>
            </div>
            <div className="lp-foot-credit">Developed by MCS03</div>
          </footer>
        </div>
      </main>
    </div>
  )
}