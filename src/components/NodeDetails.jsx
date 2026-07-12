import React from "react";

/*
 * NodeDetails
 * Side panel shown when a graph node is selected. Shows the cheap
 * "what is this concept" description (one grounded summary from its
 * earliest-appearance week) plus sources/connections — all either free or
 * a single LLM call. The full evolution breakdown (every week's summary,
 * every week-to-week comparison, the timeline) is deliberately NOT shown
 * here — for a multi-week concept, "View Full Evolution" navigates to the
 * dedicated Evolution page instead, so that expensive tier only ever
 * generates when explicitly asked for.
 * connections: [{ nodeName, label, direction, strength }]
 */
export default function NodeDetails({ node, onClose, connections, conceptDescription, detailsLoading, onViewEvolution }) {
  return (
    <div className={`kg-side-panel ${node ? "open" : ""}`}>
      {node && (
        <>
          <div className="panel-header">
            <h2 className="panel-title">{node.name}</h2>
            <button className="close-btn" onClick={onClose}>
              ×
            </button>
          </div>

          <div className="section-title">DESCRIPTION</div>
          {detailsLoading ? (
            <p style={{ color: 'var(--gv-text-secondary)'}}>Loading details...</p>
          ) : (
            <p style={{ fontSize: "0.9rem", lineHeight: 1.5, marginBottom: "16px" }}>
              {/* A grounded definition of what the concept IS, from its earliest
                  appearance — one cheap LLM call, distinct from the full
                  evolution breakdown, which lives on the Evolution page. */}
              {conceptDescription?.summary || node.description}
            </p>
          )}

          {node.appears_in_weeks > 1 && !detailsLoading && (
            <button
              className="view-evolution-btn"
              style={{
                marginBottom: "24px", background: "none", border: "1px solid var(--border)",
                borderRadius: "var(--radius-sm)", color: "var(--text-1)", padding: "6px 12px",
                fontSize: "0.85rem", cursor: "pointer",
              }}
              onClick={() => onViewEvolution(node.name)}
            >
              View Full Evolution →
            </button>
          )}

          <div className="section-title">
            SOURCES
            {node.source.length > 1 && (
              <span className="source-multi-tag" style={{ marginLeft: "auto" }}>
                Found in {node.source.length} docs
              </span>
            )}
          </div>
          <div className="source-grid">
            {node.source.map((src, i) => (
              <div key={i} className="source-item">
                <span className="source-file">{src.file}</span>
                <span className="source-page">
                  {src.page ? `Pg. ${src.page}` : "Page unknown"}
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
