import React, { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import LandingGraphDemo from '../components/LandingGraphDemo.jsx'
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

const BENEFITS = [
  {
    title: 'See relationships between concepts',
    body: 'Every connection comes from your own material, so you can follow how one idea leads into the next instead of re-reading to find out.',
  },
  {
    title: 'Track how knowledge grows over time',
    body: 'Each new upload adds to the same graph. What you learned in week one stays where it was, and you can watch the map fill in around it.',
  },
  {
    title: 'Discover connections across materials',
    body: 'Concepts that appear in more than one document are joined together, which surfaces links between topics that were never taught side by side.',
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
          <button type="button" className="lp-wordmark"
                  onClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}>
            StudyGraph
          </button>

          <nav className="lp-nav-links" aria-label="Sections">
            <button type="button" onClick={() => jumpTo('lp-features')}>Features</button>
            <button type="button" onClick={() => jumpTo('lp-how')}>How It Works</button>
            <button type="button" onClick={() => jumpTo('lp-demo')}>Demo</button>
          </nav>

          <div className="lp-nav-actions">
            <button type="button" className="lp-btn lp-btn-quiet"
                    onClick={() => navigate('/login')}>Sign In</button>
            <button type="button" className="lp-btn lp-btn-solid"
                    onClick={() => navigate('/login')}>Get Started</button>
          </div>
        </div>
      </header>

      <main id="lp-main">
        {/* ---------------- hero ---------------- */}
        <section className="lp-hero">
          <div className="lp-hero-copy">
            <h1>Turn your learning materials into a connected knowledge graph.</h1>
            <p>
              Upload lecture notes, slides, PDFs and other learning materials.
              StudyGraph identifies the important concepts and the relationships
              between them, and turns them into an interactive personal
              knowledge graph you can explore.
            </p>
            <div className="lp-hero-actions">
              <button type="button" className="lp-btn lp-btn-solid lp-btn-lg"
                      onClick={() => navigate('/login')}>Start Building</button>
              <button type="button" className="lp-btn lp-btn-outline lp-btn-lg"
                      onClick={() => jumpTo('lp-how')}>See How It Works</button>
            </div>
          </div>

          <div className="lp-hero-visual" id="lp-demo">
            <LandingGraphDemo />
          </div>
        </section>

        {/* ---------------- process ---------------- */}
        <section className="lp-section" id="lp-how">
          <div className="lp-section-head">
            <span className="lp-eyebrow">How StudyGraph works</span>
            <h2>Four steps, from a folder of documents to a map you can think with.</h2>
          </div>

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
        </section>

        {/* ---------------- benefits ---------------- */}
        <section className="lp-section lp-section-alt" id="lp-features">
          <div className="lp-section-head">
            <span className="lp-eyebrow">Why a graph</span>
            <h2>Learn through connections.</h2>
            <p>
              A list of summaries tells you what each document said. A graph
              shows you how the material fits together.
            </p>
          </div>

          <div className="lp-benefits">
            {BENEFITS.map((b) => (
              <article className="lp-benefit" key={b.title}>
                <h3>{b.title}</h3>
                <p>{b.body}</p>
              </article>
            ))}
          </div>
        </section>

        {/* ---------------- final CTA ---------------- */}
        <section className="lp-cta">
          <h2>Build your knowledge graph</h2>
          <p>Turn your course materials into connected knowledge.</p>
          <button type="button" className="lp-btn lp-btn-solid lp-btn-lg"
                  onClick={() => navigate('/login')}>Upload your first document</button>
        </section>
      </main>

      <footer className="lp-foot">
        <span className="lp-wordmark lp-wordmark-sm">StudyGraph</span>
        <span>A knowledge graph for the way you actually study.</span>
      </footer>
    </div>
  )
}
