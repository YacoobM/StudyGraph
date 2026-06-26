import React from "react";

/*
 * NodeDetails
 * Side panel shown when a graph node is selected.
 * connections: [{ nodeName, label, direction, strength }]
 */
export default function NodeDetails({ node, onClose, connections }) {
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
          <p
            style={{
              fontSize: "0.9rem",
              lineHeight: 1.5,
              marginBottom: "30px",
            }}
          >
            {node.description}
          </p>

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
