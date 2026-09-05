import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import '../styles/studygraph.css'

/**
 * Landing page — the marketing screen from the StudyGraph design, ported to
 * React. The markup and class names are the prototype's, so studygraph.css
 * styles it exactly as designed; only the inline onclick handlers changed.
 */
export default function Landing() {
  const navigate = useNavigate()
  const [note, setNote] = useState('')
  const go = (to) => navigate(to)
  const toast = (msg) => { setNote(msg); setTimeout(() => setNote(''), 2200) }

  return (
    <div className="screen active" id="screen-marketing">
      <nav className="mkt-nav">
          <button className="wordmark2" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>StudyGraph</button>
          <div className="mkt-nav-links">
            <button className="mlink" onClick={() => go("/workspace")}>Product</button>
            <button className="mlink" onClick={() => document.getElementById("mktHow")?.scrollIntoView({ behavior: "smooth" })}>How it works</button>
            <button className="mlink" onClick={() => toast("Pricing coming soon")}>Pricing</button>
            <button className="mlink" onClick={() => go("/login")}>Log in</button>
          </div>
          <button className="mkt-nav-cta" onClick={() => go("/login")}>Sign up free</button>
        </nav>

        <div className="mkt-hero">
          <span className="mkt-eyebrow">Built for how you actually study</span>
          <h1>See how your knowledge connects.</h1>
          <p>StudyGraph reads what you upload, maps every concept inside it, and shows you the structure that emerges — live, not as a static diagram.</p>
          <div className="mkt-hero-actions">
            <button className="btn-primary-t" onClick={() => go("/workspace")}>Explore your graph</button>
            <button className="btn-secondary-t" onClick={() => go("/workspace")}>Watch it parse</button>
          </div>
        </div>

        <div className="mkt-panel-wrap">
          <div className="mkt-panel">
            <div className="mkt-panel-bar">
              <div className="mkt-panel-bar-left"><span className="live-dot"></span> Live simulation — Cell Biology 301</div>
              <div className="mkt-panel-bar-right"><span><span className="v" id="rNodes">0</span> concepts</span><span><span className="v" id="rEdges">0</span> connections</span></div>
            </div>
            <div className="mkt-panel-canvas-wrap">
              <canvas id="heroCanvas"></canvas>
              <div className="node-tip" id="heroTip"></div>
            </div>
            <div className="mkt-panel-footer">
              <div className="wk">Week <b id="heroWkLabel">1</b> of 14</div>
              <input type="range" className="scrub" id="heroScrub" min="0" max="13" value="0" step="1" />
            </div>
          </div>
        </div>

        <div className="mkt-trust">
          <div className="mkt-trust-label">Used by students at</div>
          <div className="mkt-trust-logos"><span>Berkeley</span><span>UMich</span><span>UT Austin</span><span>Cornell</span><span>UBC</span></div>
        </div>

        <section className="mkt-section" id="mktHow">
          <div className="mkt-section-head">
            <div className="mkt-section-eyebrow">The process</div>
            <h2>From a folder of PDFs to a map you can think with.</h2>
            <p>StudyGraph doesn't summarize your documents — it reads them the way you would, looking for the ideas that keep reappearing and the threads that tie them together.</p>
          </div>
          <div className="mkt-card-grid">
            <div className="mkt-card"><span className="num">01</span><h3>Upload what you have</h3><p>Lecture slides, textbook chapters, half-finished notes. StudyGraph reads all of it, however messy.</p></div>
            <div className="mkt-card"><span className="num">02</span><h3>Concepts get linked</h3><p>Every recurring idea becomes a node, with real edges for cause, contrast, and prerequisite.</p></div>
            <div className="mkt-card"><span className="num">03</span><h3>Turns into notes and quizzes</h3><p>Every graph can generate study notes or a quiz on demand, exportable straight to PDF.</p></div>
          </div>
        </section>

        <div className="mkt-testimonial">
          <div className="mkt-t-inner">
            <p>"I finally understood how three weeks of lectures actually connected to each other. It's the closest thing to seeing my own notes think."</p>
            <div className="mkt-t-person">
              <div className="mkt-t-avatar">JR</div>
              <div className="mkt-t-name"><b>Jordan Reyes</b><span>Junior, Molecular Biology</span></div>
            </div>
          </div>
        </div>

        <div className="cta-band">
          <h2>Stop re-reading. Start seeing what you know.</h2>
          <p>Free for students. Bring your first course reading list today.</p>
          <button className="cta-btn" onClick={() => go("/workspace")}>Build my first graph</button>
          <div className="foot-note">StudyGraph · a knowledge graph for the way you actually study</div>
        </div>
      {note && <div className="toast show">{note}</div>}
    </div>
  )
}
