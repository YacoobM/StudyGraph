import React, { useEffect, useRef, useState } from 'react'
import { askQuestion } from '../services/api'
import './ChatPanel.css'

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

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

  const getDocumentUrl = (source, pageNumber) => {
    const filename = source?.fileName || source?.file
    if (!filename) return null
    const safeFileName = encodeURIComponent(filename)
    const targetPage = pageNumber || source?.page_number || 1
    return `${API_BASE_URL}/api/files/${safeFileName}#page=${targetPage}`
  }

  const openCitation = (source, pageNumber) => {
    const url = getDocumentUrl(source, pageNumber)
    if (!url) return
    window.open(url, '_blank', 'noopener,noreferrer')
  }

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
        { role: 'assistant', content: data.answer, sources: data.sources || [] },
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
            <div className="bubble-content">{renderMarkdown(m.content, m.sources || [], openCitation)}</div>
            {m.sources && m.sources.length > 0 && (
              <div className="bubble-sources">
                <div className="src-title">Sources</div>
                {m.sources.slice(0, 3).map((s, k) => (
                  <div
                    key={k}
                    className="src-item src-item-clickable"
                    role="button"
                    tabIndex={0}
                    onClick={() => openCitation(s, s.page_number)}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') openCitation(s, s.page_number) }}
                    title={`Open ${s.fileName} on page ${s.page_number}`}
                  >
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

function renderMarkdown(text, sources = [], openCitation) {
  if (!text) return null
  const lines = text.split('\n')
  return lines.map((line, i) => {
    const parts = line.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).filter(Boolean)
    return (
      <div key={i} className="md-line">
        {parts.map((p, k) => {
          if (/^\*\*[^*]+\*\*$/.test(p)) {
            return <strong key={k}>{renderInlineText(p.slice(2, -2), sources, openCitation)}</strong>
          }
          if (/^\*[^*]+\*$/.test(p)) {
            return <em key={k}>{renderInlineText(p.slice(1, -1), sources, openCitation)}</em>
          }
          return <span key={k}>{renderInlineText(p, sources, openCitation)}</span>
        })}
      </div>
    )
  })
}

function renderInlineText(text, sources = [], openCitation) {
  if (!text) return null
  const citationRegex = /\[(?:p|pp)\.\s*(\d+(?:-\d+)?)\]|\b(?:p|pp)\[(\d+(?:-\d+)?)\]/g
  const parts = []
  let lastIndex = 0
  let match

  while ((match = citationRegex.exec(text))) {
    const matchedText = match[0]
    const rangeText = match[1] || match[2]
    const pageNumber = rangeText?.split('-')[0]

    if (match.index > lastIndex) {
      parts.push(<span key={`text-${lastIndex}`}>{text.slice(lastIndex, match.index)}</span>)
    }

    const source = findCitationSource(sources, pageNumber)
    const href = source ? getDocumentUrl(source, pageNumber) : null
    parts.push(
      href ? (
        <a
          key={`citation-${match.index}`}
          className="citation-chip citation-link"
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          title={`Open ${source.fileName} on page ${pageNumber}`}
        >
          {matchedText}
        </a>
      ) : (
        <button
          key={`citation-${match.index}`}
          type="button"
          className="citation-chip"
          onClick={() => openCitation(source, pageNumber)}
          title="Open source"
        >
          {matchedText}
        </button>
      )
    )

    lastIndex = match.index + matchedText.length
  }

  if (lastIndex < text.length) {
    parts.push(<span key={`text-${lastIndex}`}>{text.slice(lastIndex)}</span>)
  }

  return <>{parts}</>
}

function findCitationSource(sources = [], pageNumber) {
  if (!sources.length) return null
  return sources.find((source) => String(source.page_number) === String(pageNumber)) || sources[0]
}
