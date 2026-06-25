import React from 'react'
import { Link } from 'react-router-dom'
import './Landing.css'

export default function Landing() {
  return (
    <div className="landing">
      {/* animated background */}
      <div className="landing-bg">
        <svg className="bg-graph" viewBox="0 0 1200 800" preserveAspectRatio="xMidYMid slice">
          <defs>
            <radialGradient id="glow" cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor="#5dd4c4" stopOpacity="0.35" />
              <stop offset="100%" stopColor="#5dd4c4" stopOpacity="0" />
            </radialGradient>
          </defs>
          <circle cx="200" cy="200" r="300" fill="url(#glow)" />
          <circle cx="900" cy="600" r="280" fill="url(#glow)" opacity="0.6" />
          {/* graph illustration */}
          <g stroke="#2c3440" strokeWidth="1.5" fill="none" opacity="0.8">
            <line x1="180" y1="320" x2="340" y2="240" />
            <line x1="340" y1="240" x2="520" y2="320" />
            <line x1="520" y1="320" x2="680" y2="220" />
            <line x1="680" y1="220" x2="840" y2="360" />
            <line x1="340" y1="240" x2="280" y2="440" />
            <line x1="520" y1="320" x2="600" y2="500" />
            <line x1="280" y1="440" x2="600" y2="500" />
            <line x1="600" y1="500" x2="840" y2="360" />
            <line x1="900" y1="500" x2="840" y2="360" />
          </g>
          <g>
            <circle cx="180" cy="320" r="14" fill="#f9a8d4" className="node-pulse" />
            <circle cx="340" cy="240" r="18" fill="#5dd4c4" className="node-pulse" style={{ animationDelay: '0.3s' }} />
            <circle cx="520" cy="320" r="14" fill="#a3e635" className="node-pulse" style={{ animationDelay: '0.6s' }} />
            <circle cx="680" cy="220" r="14" fill="#fbbf24" className="node-pulse" style={{ animationDelay: '0.9s' }} />
            <circle cx="840" cy="360" r="18" fill="#60a5fa" className="node-pulse" style={{ animationDelay: '1.2s' }} />
            <circle cx="280" cy="440" r="12" fill="#fb923c" className="node-pulse" style={{ animationDelay: '0.5s' }} />
            <circle cx="600" cy="500" r="14" fill="#a78bfa" className="node-pulse" style={{ animationDelay: '0.7s' }} />
            <circle cx="900" cy="500" r="12" fill="#ec4899" className="node-pulse" style={{ animationDelay: '1.0s' }} />
          </g>
        </svg>
      </div>

      {/* nav */}
      <header className="landing-nav">
        <div className="brand">
          <span className="brand-mark">
            <svg width="28" height="28" viewBox="0 0 64 64">
              <circle cx="32" cy="18" r="9" fill="#5dd4c4"/>
              <circle cx="14" cy="46" r="7" fill="#f9a8d4"/>
              <circle cx="50" cy="46" r="7" fill="#a3e635"/>
              <line x1="32" y1="27" x2="14" y2="40" stroke="#5b6473" strokeWidth="2"/>
              <line x1="32" y1="27" x2="50" y2="40" stroke="#5b6473" strokeWidth="2"/>
              <line x1="21" y1="46" x2="43" y2="46" stroke="#5b6473" strokeWidth="2"/>
            </svg>
          </span>
          <span className="brand-text">StudyGraph</span>
        </div>
        <nav className="nav-links">
          <a href="#features">Features</a>
          <a href="#how">How it works</a>
          <a href="#about">About</a>
          <Link to="/login" className="btn btn-primary nav-cta">Sign in</Link>
        </nav>
      </header>

      {/* hero */}
      <main className="hero">
        <div className="hero-eyebrow">
          <span className="dot dot-success" /> Neo4j-style · powered by RAG
        </div>

        <h1 className="hero-title">
          Turn your <span className="accent-text">study notes</span>
          <br /> into a living
          <span className="accent-underline"> knowledge graph.</span>
        </h1>

        <p className="hero-sub">
          Drop in your lecture slides, PDFs, or notes. StudyGraph extracts the key concepts,
          maps every relationship, and lets you ask questions across all of it —
          like having a personal research assistant for your coursework.
        </p>

        <div className="hero-cta">
          <Link to="/login" className="btn btn-primary btn-lg">
            Start building →
          </Link>
          <Link to="/login" className="btn btn-ghost btn-lg">
            Try the demo
          </Link>
        </div>

        <div className="hero-stats">
          <div>
            <div className="stat-num">PDF · PPTX · DOCX</div>
            <div className="stat-label">Supported formats</div>
          </div>
          <div className="stat-divider" />
          <div>
            <div className="stat-num">16+</div>
            <div className="stat-label">Entity types</div>
          </div>
          <div className="stat-divider" />
          <div>
            <div className="stat-num">RAG</div>
            <div className="stat-label">Q&amp;A engine</div>
          </div>
        </div>
      </main>

      {/* features */}
      <section id="features" className="features">
        <h2 className="section-title">Built for studying.</h2>
        <div className="feature-grid">
          <div className="feature-card">
            <div className="feature-icon" style={{ background: 'rgba(93,212,196,0.15)' }}>📤</div>
            <h3>Upload anything</h3>
            <p>Drag &amp; drop your lecture decks, PDFs, or text. Everything is parsed in seconds.</p>
          </div>
          <div className="feature-card">
            <div className="feature-icon" style={{ background: 'rgba(163,230,53,0.15)' }}>🧠</div>
            <h3>Extract concepts</h3>
            <p>The backend identifies algorithms, terms, rules, and how they relate across chunks.</p>
          </div>
          <div className="feature-card">
            <div className="feature-icon" style={{ background: 'rgba(249,168,212,0.15)' }}>🕸️</div>
            <h3>Visualize the graph</h3>
            <p>Explore your knowledge as an interactive force-directed graph — click any node for details.</p>
          </div>
          <div className="feature-card">
            <div className="feature-icon" style={{ background: 'rgba(96,165,250,0.15)' }}>💬</div>
            <h3>Ask anything</h3>
            <p>Built-in RAG chat. Ask a question and get answers grounded in your own documents.</p>
          </div>
        </div>
      </section>

      {/* how */}
      <section id="how" className="how">
        <h2 className="section-title">Three steps. That's it.</h2>
        <div className="how-steps">
          <div className="how-step">
            <span className="step-num">01</span>
            <h4>Connect &amp; upload</h4>
            <p>Sign in, then drop your documents into the builder.</p>
          </div>
          <div className="how-step">
            <span className="step-num">02</span>
            <h4>Generate graph</h4>
            <p>Hit "Generate Graph" — entities and relationships appear instantly.</p>
          </div>
          <div className="how-step">
            <span className="step-num">03</span>
            <h4>Study smarter</h4>
            <p>Browse the graph, click nodes, or chat with the Q&amp;A panel.</p>
          </div>
        </div>
      </section>

      <footer className="landing-foot" id="about">
        <span>StudyGraph · A Neo4j-inspired knowledge builder for students.</span>
        <span className="foot-credit">Built for FIT3155 · 2026</span>
      </footer>
    </div>
  )
}
