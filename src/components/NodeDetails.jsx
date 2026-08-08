import React from "react";
import ConceptEvolutionDetails from "./ConceptEvolutionDetails";

/*
 * NodeDetails
 * Side panel shown when a graph node is selected. Shows the cheap
 * "what is this concept" description (one grounded summary from its
 * earliest-appearance week) plus sources/connections — all either free or
 * a single LLM call.
 *
 * "View Full Evolution" expands the expensive tier (every week's summary +
 * every week-to-week comparison + the mini timeline subgraph) inline, right
 * in this same panel, on that one click — it used to navigate to a separate
 * /evolution page and ask for the same thing again via a second "Show
 * Concept Evolution" button; that page (with its own independent full-graph
 * fetch and layout) is gone, folded into here, so there's one action, one
 * click, and no separate graph to lose on the way back.
 */
export default function NodeDetails({
  node, onClose, connections,
  conceptDescription, detailsLoading,
  evolution, evolutionLoading, onShowEvolution,
  onRegenerate, regenerating,
  onOpenSource,
}) {
  const sources = Array.isArray(node?.source) ? node.source : []

  return (
    <div className={`kg-side-panel ${node ? "open" : ""}`}>
      {node && (
        <>
          <div className="panel-header">
            <h2 className="panel-title">{node?.name || 'Concept'}</h2>
            <button className="close-btn" onClick={onClose}>
              ×
            </button>
          </div>

          <div className="section-title">
            DESCRIPTION
            {!detailsLoading && conceptDescription && (
              <button
                className="regenerate-btn"
                onClick={onRegenerate}
                disabled={regenerating}
                title="Re-generate this description from the source material"
                style={{ marginLeft: "auto" }}
              >
                {regenerating ? "Regenerating…" : "⟳ Regenerate"}
              </button>
            )}
          </div>
          {detailsLoading ? (
            <p style={{ color: 'var(--gv-text-secondary)'}}>Loading details...</p>
          ) : (
            <>
              {conceptDescription?.is_fallback && (
                <p className="fallback-warning">
                  ⚠ Groq couldn't summarize this one — showing a raw excerpt from the source instead. Try Regenerate.
                </p>
              )}
              <p style={{ fontSize: "0.9rem", lineHeight: 1.5, marginBottom: "16px" }}>
                {/* A grounded definition of what the concept IS, from its earliest
                    appearance — one cheap LLM call, distinct from the full
                    evolution breakdown below. */}
                {conceptDescription?.summary || node.description}
              </p>
            </>
          )}

          {node.appears_in_weeks > 1 && !detailsLoading && !evolution && (
            <button
              className="view-evolution-btn"
              disabled={evolutionLoading}
              onClick={() => onShowEvolution(node.name)}
            >
              {evolutionLoading ? 'Loading concept evolution…' : 'View Full Evolution →'}
            </button>
          )}

          {evolution && <ConceptEvolutionDetails evolution={evolution} />}

          <div className="section-title">
            SOURCES
            {sources.length > 1 && (
              <span className="source-multi-tag" style={{ marginLeft: "auto" }}>
                Found in {sources.length} docs
              </span>
            )}
          </div>
          <div className="source-grid">
            {sources.map((src, i) => (
              <div
                key={i}
                className="source-item source-item-clickable"
                onClick={() => onOpenSource(src, node.name)}
                title="Open this source"
              >
                <span className="source-file">{src.file}</span>
                <span className="source-page">
                  {src.pageLabel ? `Pg. ${src.pageLabel}` : "Page unknown"}
                </span>
              </div>
            ))}
          </div>

          <div className="section-title">
            CONNECTIONS ({connections.length})
          </div>
          <div>
            {connections.map((conn, idx) => (
              <div key={idx} className="connection-card">
                <div className="conn-info">
                  <h4>{conn.nodeName}</h4>
                  <p>
                    <span>{conn.direction}</span> {conn.label}
                  </p>
                  {conn.context ? <p style={{ marginTop: '6px', color: 'var(--gv-text-secondary)' }}>{conn.context}</p> : conn.summary ? <p style={{ marginTop: '6px', color: 'var(--gv-text-secondary)' }}>{conn.summary}</p> : null}
                </div>
                <div className={`conn-badge ${conn.strength || "medium"}`}>
                  {(conn.strength || "MEDIUM").toUpperCase()}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
