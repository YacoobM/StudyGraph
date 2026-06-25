import React, { useEffect, useState } from 'react'
import { getGraph } from '../services/api'
import './GraphViewerPlaceholder.css'

/* ==========================================================================
 *  ⚠️  INTEGRATION POINT #2 — GRAPH VISUALIZATION  ⚠️
 * ==========================================================================
 *
 *  This component is a PLACEHOLDER. The teammate responsible for the graph
 *  visualization module should replace the marked section below with the
 *  real renderer (e.g. D3.js force-directed graph, vis-network, neovis.js,
 *  react-force-graph, etc.).
 *
 *  WHAT YOU GET
 *  ------------
 *  `graph` (fetched from getGraph()) has this exact shape:
 *
 *    {
 *      nodes: [
 *        { id: string,
 *          label: string,          // e.g. "Algorithm", "Chunk", "Document"
 *          color: string,          // hex color for the node
 *          properties: { name?, fileName?, mentions?, ... } }
 *      ],
 *      relationships: [
 *        { id: string,
 *          source: string,         // node id
 *          target: string,         // node id
 *          type: string,           // e.g. "USES", "PART_OF"
 *          properties: {} }
 *      ],
 *      stats: {
 *        total_nodes, total_relationships,
 *        label_counts: { [label]: count },
 *        rel_counts:   { [type]: count }
 *      }
 *    }
 *
 *  WHAT TO DO
 *  ----------
 *  1. Build your renderer using `graph.nodes` and `graph.relationships`.
 *  2. Replace the <div className="gvp-placeholder"> ... </div> block below
 *     with your visualization canvas (svg / canvas / webgl).
 *  3. Keep the modal shell (header, close button) if you want, or restyle.
 *  4. The `onClose` prop closes the modal — keep wiring it to your close UI.
 *
 *  Your module ONLY needs to read the `graph` data. It does not need to know
 *  anything about uploads, auth, or the backend — that's all handled
 *  upstream and passed to you as clean JSON.
 *
 * ========================================================================== */

export default function GraphViewerPlaceholder({ onClose }) {
  const [graph, setGraph] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let mounted = true
    getGraph()
      .then((data) => { if (mounted) { setGraph(data); setLoading(false) } })
      .catch(() => { if (mounted) setLoading(false) })
    return () => { mounted = false }
  }, [])

  return (
    <div className="gvp-overlay">
      <div className="gvp-modal">
        <div className="gvp-header">
          <div>
            <h2>Generated Graph</h2>
            <div className="gvp-subtitle">
              {loading
                ? 'Loading graph data…'
                : `${graph?.stats?.total_nodes ?? 0} nodes · ${graph?.stats?.total_relationships ?? 0} relationships`}
            </div>
          </div>
          <button className="gvp-close" onClick={onClose}>×</button>
        </div>

        <div className="gvp-body">
          {/* ================================================================
              ⬇️  VISUALIZATION TEAMMATE: REPLACE THIS BLOCK  ⬇️
              ================================================================ */}
          <div className="gvp-placeholder">
            <div className="gvp-icon">
              <svg width="64" height="64" viewBox="0 0 64 64" fill="none">
                <circle cx="32" cy="16" r="8" fill="#22d3ee" opacity="0.9"/>
                <circle cx="13" cy="46" r="6" fill="#f9a8d4" opacity="0.9"/>
                <circle cx="51" cy="46" r="6" fill="#a3e635" opacity="0.9"/>
                <circle cx="48" cy="20" r="5" fill="#fbbf24" opacity="0.9"/>
                <line x1="32" y1="24" x2="13" y2="40" stroke="#475569" strokeWidth="2"/>
                <line x1="32" y1="24" x2="51" y2="40" stroke="#475569" strokeWidth="2"/>
                <line x1="32" y1="20" x2="44" y2="20" stroke="#475569" strokeWidth="2"/>
                <line x1="19" y1="46" x2="45" y2="46" stroke="#475569" strokeWidth="2"/>
              </svg>
            </div>
            <h3>Graph visualization goes here</h3>
            <p className="gvp-note">
              This is <strong>INTEGRATION POINT #2</strong>. The visualization
              module renders into this area using the graph data shown below.
            </p>

            {/* A simple readout of the data, proving the contract works.
                The visualization teammate can delete this once their
                renderer is in place. */}
            {loading ? (
              <div className="gvp-loading">Fetching graph…</div>
            ) : graph ? (
              <div className="gvp-data">
                <div className="gvp-data-col">
                  <div className="gvp-data-title">Nodes ({graph.nodes.length})</div>
                  <ul>
                    {graph.nodes.slice(0, 8).map((n) => (
                      <li key={n.id}>
                        <span className="gvp-dot" style={{ background: n.color }} />
                        <span className="gvp-name">{n.properties?.name || n.properties?.fileName || n.label}</span>
                        <span className="gvp-label">{n.label}</span>
                      </li>
                    ))}
                  </ul>
                </div>
                <div className="gvp-data-col">
                  <div className="gvp-data-title">Relationships ({graph.relationships.length})</div>
                  <ul>
                    {graph.relationships.slice(0, 8).map((r) => (
                      <li key={r.id}>
                        <code>{r.source}</code>
                        <span className="gvp-rel">→ {r.type} →</span>
                        <code>{r.target}</code>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : (
              <div className="gvp-loading">No graph data available.</div>
            )}
          </div>
          {/* ================================================================
              ⬆️  END REPLACE BLOCK  ⬆️
              ================================================================ */}
        </div>
      </div>
    </div>
  )
}
