import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
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

export default function GraphViewerPlaceholder({ graph, onClose }) {
  const navigate = useNavigate()
  const [selectedNode, setSelectedNode] = useState(null)

  const handleNodeClick = (node) => {
    setSelectedNode(selectedNode?.id === node.id ? null : node)
  }

  const handleViewEvolution = (node) => {
    if (!node || !node.properties.name) return
    navigate(`/evolution?concept=${encodeURIComponent(node.properties.name)}`)
  }

  const loading = !graph; // Loading is true if no graph prop is passed
  const nodes = graph?.nodes || [];
  const relationships = graph?.relationships || [];
  const stats = graph?.stats || { total_nodes: 0, total_relationships: 0 };

  return (
    <div className="gvp-overlay">
      <div className="gvp-modal">
        <div className="gvp-header">
          <div>
            <h2>Generated Graph</h2>
            <div className="gvp-subtitle">
              {loading
                ? 'Loading graph data…'
                : `${stats.total_nodes} nodes · ${stats.total_relationships} relationships`}
            </div>
          </div>
          <button className="gvp-close" onClick={onClose}>×</button>
        </div>

        <div className="gvp-body">
          {/* ================================================================
              ⬇️  VISUALIZATION TEAMMATE: REPLACE THIS BLOCK  ⬇️
              ================================================================ */}
          <div className="gvp-placeholder gvp-data-with-details">
            {loading ? (
              <div className="gvp-loading">Fetching graph…</div>
            ) : nodes.length > 0 ? (
              <>
                <div className="gvp-data">
                  <div className="gvp-data-col">
                    <div className="gvp-data-title">Nodes ({nodes.length})</div>
                    <ul>
                      {nodes.slice(0, 15).map((n) => (
                        <li key={n.id} onClick={() => handleNodeClick(n)} className={selectedNode?.id === n.id ? 'selected' : ''}>
                          <span className="gvp-dot" style={{ background: n.color }} />
                          <span className="gvp-name">{n.properties?.name || n.label}</span>
                          <span className="gvp-label">{n.label}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                  <div className="gvp-data-col">
                    <div className="gvp-data-title">Relationships ({relationships.length})</div>
                    <ul>
                      {relationships.slice(0, 15).map((r) => (
                        <li key={r.id}>
                          <code>{r.source}</code>
                          <span className="gvp-rel">→ {r.type} →</span>
                          <code>{r.target}</code>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>

                {selectedNode && (
                  <div className="gvp-details-panel">
                    <h4>{selectedNode.properties.name}</h4>
                    <p>Appears in <strong>{selectedNode.properties.appears_in_weeks}</strong> week(s).</p>
                    
                    {selectedNode.properties.appears_in_weeks > 1 && (
                      <button className="btn-view-evolution" onClick={() => handleViewEvolution(selectedNode)}>
                        View Concept Evolution
                      </button>
                    )}

                    <h5>Properties</h5>
                    <pre>{JSON.stringify(selectedNode.properties, null, 2)}</pre>
                  </div>
                )}
              </>
            ) : (
              <div className="gvp-loading">
                <h3>Graph is Empty</h3>
                <p>Upload documents and generate a graph to see a visualization.</p>
              </div>
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
