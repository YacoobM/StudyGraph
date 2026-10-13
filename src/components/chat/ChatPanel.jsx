import { useEffect, useRef, useState } from "react";
import { askGraphQuestion, getGraphDocumentSignedUrl } from "../../services/api";
import "./ChatPanel.css";
import { ArrowUp } from "lucide-react";

const API_BASE_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const OPENABLE_TYPES = new Set([
  "pdf",
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "svg",
  "txt",
  "md",
]);

/**
 * Helper methd to get the file extension
 */
const getFileExtension = (filename = "") =>
  filename.split(".").pop()?.toLowerCase();

/**
 * Some files like DOCX and pptx cannot be opened on browser
 * @param {*} source - cited source
 * @returns  -boolean of whether file can be opened or not
 */
const isOpenableFile = (source) => {
  const filename = source?.fileName || source?.file || "";
  const extension = getFileExtension(filename);

  return OPENABLE_TYPES.has(extension);
};

/**
 * Builds a URL for the DocumentViewer page that opens the PDF at a specific page
 * with optional text highlighting.
 *
 * @param {Object} source - Source object with fileName and page_number
 * @param {number} pageNumber - Page number to open
 * @param {string} highlightText - Optional text to highlight on the page
 * @returns {string} URL for the DocumentViewer page
 */
const getDocumentUrl = (source, pageNumber) => {
  const filename = source?.fileName || source?.file;
  if (!filename) return null;

  const extension = getFileExtension(filename);
  const safeFileName = encodeURIComponent(filename);
  const fileUrl = `${API_BASE_URL}/api/files/${safeFileName}`;

  if (extension === "pdf") {
    const page = pageNumber || source?.page_number || 1;
    return `${fileUrl}#page=${page}`;
  }

  return fileUrl;
};

function TaggedAnswer({ answer, concepts_mentioned, onConceptClick }) {

  if (!concepts_mentioned || !Array.isArray(concepts_mentioned)){
    console.log(answer)
    return <p>{answer}</p>
  }

  //split the answer into parts, and tag
  //concepts as they're found
  let parts = [answer];
  
  concepts_mentioned.forEach((concept) => {
    
    parts = parts.flatMap((part) => {
      if (typeof part !== "string") return [part];

      //case insensitive regex to find concept
      const regex = new RegExp(
        `\\b${concept.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`,
        "gi",
      );

      const matches = part.split(regex);

      if (matches.length == 1) return [part];

      return matches.reduce((acc, match, i) => {
        console.log(concept);
        acc.push(match); //push the plain text
        if (i < matches.length - 1) {
          //if not the last part
          acc.push(
            //matched concept name
            <button
              key={`${concept}-${i}`}
              className="concept-tag"
              onClick={() => onConceptClick(concept)}
            >
              {part.match(regex)[i]}
            </button>,
          );
        }
        return acc;
      }, []);
    });
  });

  return <p>{parts}</p>
}

export default function ChatPanel({
  onClose,
  embedded = false,
  onConceptClick,
  graphId = null,
  width = 380,
  onResize,
}) {
  const [messages, setMessages] = useState([
    {
      role: "assistant",
      content:
        "Hi! I'm StudyGraph. Upload your documents and generate a graph, then ask me anything about the content — I'll pull answers straight from your sources.",
        concepts_mentioned:[],
    },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);
  const resizeStart = useRef(null);
  const maxChatWidth = () => Math.max(
    320,
    Math.min(640, window.innerWidth - (window.innerWidth > 900 ? 272 : 0) - 280),
  );
  const effectiveWidth = Math.max(320, Math.min(maxChatWidth(), width));

  const startResize = (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    resizeStart.current = {
      x: event.clientX,
      width: event.currentTarget.parentElement.getBoundingClientRect().width,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };
  const resize = (event) => {
    if (!resizeStart.current) return;
    onResize?.(Math.max(320, Math.min(
      maxChatWidth(),
      resizeStart.current.width + resizeStart.current.x - event.clientX,
    )));
  };
  const stopResize = () => {
    resizeStart.current = null;
  };

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, busy]);

  //close the chat panel
  useEffect(() => {
    const handleExit = (event) => {
      if (event.key === "Escape") onClose?.();
    };

    window.addEventListener("keydown", handleExit);
    return () => window.removeEventListener("keydown", handleExit);
  }, [onClose]);

  useEffect(() => {
    console.log('All messages:', messages)
    if (messages.length > 0) {
      const lastMessage = messages[messages.length - 1]
      console.log('Last message:', lastMessage)
    }
  }, [messages])

  const openCitation = async (source, pageNumber) => {
    const filename = source?.fileName || source?.file || "";
    const extension = getFileExtension(filename);

    if (!isOpenableFile(source)) {
      window.alert(
        `This source is a .${extension} file and cannot be opened in a browser tab.`,
      );
      return;
    }

    let url = getDocumentUrl(source);
    if (graphId && source?.document_id) {
      try {
        const signed = await getGraphDocumentSignedUrl(graphId, source.document_id);
        url = signed.url;
        if (extension === "pdf") {
          const page = pageNumber || source?.page_number || 1;
          url = `${url}#page=${page}`;
        }
      } catch (error) {
        window.alert(`Could not open ${filename}: ${error.message}`);
        return;
      }
    }

    if (url) {
      window.open(url, "_blank", "noopener,noreferrer");
    }
  };

  const send = async () => {
    const q = input.trim();
    if (!q || busy) return;
    setMessages((m) => [...m, { role: "user", content: q }]);
    setInput("");
    setBusy(true);
    try {
      const data = await askGraphQuestion(graphId, q)
      console.log('API response:', data);
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content: data.answer,
          sources: data.sources || [],
          concepts_mentioned: data.concepts_mentioned || [],
          subgraph: data.subgraph || null,
        },
      ]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        {
          role: "assistant",
          content:
            "Sorry — I couldn't get an answer just now. Make sure you've generated a graph from your documents.",
          concepts_mentioned:[],
        },
      ]);
    } finally {
      setBusy(false);
    }
  };


  const onKey = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <aside className="chat-panel" role="complementary" aria-label="StudyGraph chat" style={{ "--chat-width": `${effectiveWidth}px` }}>
      {!embedded && (
        <div
          className="chat-resize"
          role="separator"
          aria-label="Resize chat panel"
          aria-orientation="vertical"
          aria-valuemin={320}
          aria-valuemax={maxChatWidth()}
          aria-valuenow={effectiveWidth}
          tabIndex={0}
          onPointerDown={startResize}
          onPointerMove={resize}
          onPointerUp={stopResize}
          onPointerCancel={stopResize}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              onResize?.(Math.min(maxChatWidth(), effectiveWidth + 16));
            }
            if (event.key === "ArrowRight") {
              event.preventDefault();
              onResize?.(Math.max(320, effectiveWidth - 16));
            }
          }}
        />
      )}
      <div className="chat-head">
        <div className="chat-head-title">
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          </svg>
          <span>Ask StudyGraph</span>
        </div>
        {!embedded && (
          <button className="chat-close" onClick={onClose}>
            ×
          </button>
        )}
      </div>

      <div className="chat-body">
        {messages.map((m, i) => (
          <div key={i} className={`bubble bubble-${m.role}`}>
            <div className="bubble-content">
              {m.concepts_mentioned && m.concepts_mentioned.length > 0 ? (
                <TaggedAnswer
                  answer={m.content}
                  concepts_mentioned={
                    m.concepts_mentioned
                  }
                  onConceptClick={onConceptClick}
                />
              ) : (
                renderMarkdown(m.content, m.sources || [], openCitation)
              )}
            </div>
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
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ")
                        openCitation(s, s.page_number);
                    }}
                    title={`Open ${s.fileName} on page ${s.page_number}`}
                  >
                    <span className="src-file">{s.fileName}</span>
                    {!["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(
                      getFileExtension(s.fileName || s.file),
                    ) && <span className="src-page">p.{s.page_number}</span>}
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
              <span></span>
              <span></span>
              <span></span>
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
        <button
          className="btn btn-primary chat-send"
          onClick={send}
          disabled={busy || !input.trim()}
        >
          <ArrowUp size={20} />
        </button>
      </div>
    </aside>
  );
}

function renderMarkdown(text, sources = [], openCitation) {
  if (!text) return null;
  const lines = text.split("\n");
  return lines.map((line, i) => {
    const parts = line.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).filter(Boolean);
    return (
      <div key={i} className="md-line">
        {parts.map((p, k) => {
          if (/^\*\*[^*]+\*\*$/.test(p)) {
            return (
              <strong key={k}>
                {renderInlineText(p.slice(2, -2), sources, openCitation)}
              </strong>
            );
          }
          if (/^\*[^*]+\*$/.test(p)) {
            return (
              <em key={k}>
                {renderInlineText(p.slice(1, -1), sources, openCitation)}
              </em>
            );
          }
          return (
            <span key={k}>{renderInlineText(p, sources, openCitation)}</span>
          );
        })}
      </div>
    );
  });
}

function renderInlineText(text, sources = [], openCitation) {
  if (!text) return null;
  const citationRegex =
    /\[(?:p|pp)\.\s*(\d+(?:-\d+)?)\]|\b(?:p|pp)\[(\d+(?:-\d+)?)\]/g;
  const parts = [];
  let lastIndex = 0;
  let match;

  while ((match = citationRegex.exec(text))) {
    const matchedText = match[0];
    const rangeText = match[1] || match[2];
    const pageNumber = rangeText?.split("-")[0];

    if (match.index > lastIndex) {
      parts.push(
        <span key={`text-${lastIndex}`}>
          {text.slice(lastIndex, match.index)}
        </span>,
      );
    }

    const source = findCitationSource(sources, pageNumber);
    const href =
      source && isOpenableFile(source)
        ? getDocumentUrl(source, pageNumber, source?.snippet)
        : null;
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
      ),
    );

    lastIndex = match.index + matchedText.length;
  }

  if (lastIndex < text.length) {
    parts.push(<span key={`text-${lastIndex}`}>{text.slice(lastIndex)}</span>);
  }

  return <>{parts}</>;
}

function findCitationSource(sources = [], pageNumber) {
  if (!sources.length) return null;
  return (
    sources.find(
      (source) => String(source.page_number) === String(pageNumber),
    ) || sources[0]
  );
}
