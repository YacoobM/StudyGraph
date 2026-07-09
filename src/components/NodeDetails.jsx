import React from "react";
import EvolutionGraph from './EvolutionGraph'

/*
 * NodeDetails
 * Side panel shown when a graph node is selected. For concepts that span
 * multiple weeks, also shows the full evolution breakdown (timeline,
 * week-to-week comparisons, weekly summaries) inline — no navigation away
 * from the graph that's currently open.
 * connections: [{ nodeName, label, direction, strength }]
 */
export default function NodeDetails({ node, onClose, connections, conceptDetails, conceptSubgraph, detailsLoading }) {
  const isMultiWeek = node?.appears_in_weeks > 1

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
            <p style={{ fontSize: "0.9rem", lineHeight: 1.5, marginBottom: "30px" }}>
              {/* A grounded definition of what the concept IS, from its earliest
                  appearance — distinct from the evolution comparison below, which
                  is about how it changed, not what it means. */}
              {conceptDetails?.weeks_data?.[0]?.summary || node.description}
            </p>
          )}

          {isMultiWeek && !detailsLoading && conceptDetails && (
            <>
              {/* With exactly 2 weeks, "first vs. last" IS "Week 1 vs Week 2" —
                  showing both would just repeat the same text twice. Only worth
                  a separate section once there's a real span to summarize. */}
              {conceptDetails.consecutive_comparisons?.length > 1 && (
                <>
                  <div className="section-title">OVERALL EVOLUTION</div>
                  <p style={{ fontSize: "0.9rem", lineHeight: 1.5 }}>{conceptDetails.comparison}</p>
                </>
              )}

              <div className="section-title" style={{ marginTop: '20px' }}>EVOLUTION TIMELINE</div>
              <div className="subgraph-container">
                {conceptSubgraph ? (
                  <EvolutionGraph nodes={conceptSubgraph.nodes} edges={conceptSubgraph.edges} onNodeClick={() => {}} />
                ) : (
                  <div style={{ color: 'var(--gv-text-secondary)', padding: '12px' }}>No timeline data.</div>
                )}
              </div>

              {conceptDetails.consecutive_comparisons?.map((comp, i) => (
                <div key={i} style={{ marginTop: '20px' }}>
                  <div className="section-title">{comp.from_week} → {comp.to_week}</div>
                  <p style={{ fontSize: "0.9rem", lineHeight: 1.5 }}>{comp.comparison}</p>
                </div>
              ))}

              <div className="section-title" style={{ marginTop: '20px' }}>WEEKLY SUMMARY</div>
              <ul className="week-summary-list">
                {conceptDetails.weeks_data?.map(week => (
                  <li key={week.week}>
                    <strong>{week.week}:</strong> {week.summary}
                  </li>
                ))}
              </ul>
            </>
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
