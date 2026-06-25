import React, { useEffect, useRef, useState } from 'react'
import { askQuestion } from '../services/api'
import './ChatPanel.css'

export default function ChatPanel({ onClose, embedded = false }) {
  const [messages, setMessages] = useState([
    {
      role: 'assistant',
      content:
        "Hi! I'm StudyGraph. Upload your documents and generate a graph, then ask me anything about the content — I'll pull answers straight from your sources.",
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const endRef = useRef(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, busy])

  const send = async () => {
    const q = input.trim()
    if (!q || busy) return
    setMessages((m) => [...m, { role: 'user', content: q }])
    setInput('')
    setBusy(true)
    try {
      const data = await askQuestion(q)
      setMessages((m) => [
        ...m,
        { role: 'assistant', content: data.answer, sources: data.sources },
      ])
    } catch (e) {
      setMessages((m) => [
        ...m,
        {
          role: 'assistant',
          content:
            "Sorry — I couldn't get an answer just now. Make sure you've generated a graph from your documents.",
        },
      ])
    } finally {
      setBusy(false)
    }
  }

  const onKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className={embedded ? "chat-drawer chat-panel-embedded" : "chat-drawer"}>
      <div className="chat-head">
        <div className="chat-head-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
          <span>Ask StudyGraph</span>
        </div>
        {!embedded && <button className="chat-close" onClick={onClose}>×</button>}
      </div>

      <div className="chat-body">
        {messages.map((m, i) => (
          <div key={i} className={`bubble bubble-${m.role}`}>
            <div className="bubble-content">{renderMarkdown(m.content)}</div>
            {m.sources && m.sources.length > 0 && (
              <div className="bubble-sources">
                <div className="src-title">Sources</div>
                {m.sources.slice(0, 3).map((s, k) => (
                  <div key={k} className="src-item">
                    <span className="src-file">{s.fileName}</span>
                    <span className="src-page">p.{s.page_number}</span>
                    <div className="src-snip">"{s.snippet}…"</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {busy && (
          <div className="bubble bubble-assistant">
            <div className="typing">
              <span></span><span></span><span></span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      <div className="chat-input-row">
        <textarea
          className="chat-input"
          placeholder="Ask anything about your documents…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKey}
          rows={1}
        />
        <button className="btn btn-primary chat-send" onClick={send} disabled={busy || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  )
}

// minimal markdown-ish rendering for **bold** and bullets
function renderMarkdown(text) {
  if (!text) return null
  const lines = text.split('\n')
  return lines.map((line, i) => {
    // bold
    const parts = line.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).filter(Boolean)
    return (
      <div key={i} className="md-line">
        {parts.map((p, k) => {
          if (/^\*\*[^*]+\*\*$/.test(p)) return <strong key={k}>{p.slice(2, -2)}</strong>
          if (/^\*[^*]+\*$/.test(p)) return <em key={k}>{p.slice(1, -1)}</em>
          return <span key={k}>{p}</span>
        })}
      </div>
    )
  })
}
