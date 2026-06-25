import React, { useState } from 'react'
import GraphCanvas from './GraphCanvas'
import NodeDetails from './NodeDetails'
import './GraphViewer.css'

/*
 * GraphViewer
 * Modal wrapper around GraphCanvas + NodeDetails.
 * Drop-in replacement for GraphViewerPlaceholder.
 *
 * Props:
 * nodes, edges - graph data, shape: see GraphCanvas
 * onClose - closes the modal
 */
export default function GraphViewer({ nodes = [], edges = [], onClose }) {
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedNode, setSelectedNode] = useState(null)
  const [filterLevel, setFilterLevel] = useState(3)

  const getConnections = () => {
    if (!selectedNode) return []
    return edges
      .filter((l) => {
        const validLink = filterLevel === 3 || (filterLevel === 2 && l.strength !== 'low') || (filterLevel === 1 && l.strength === 'high')
        // FIXED: Using string casting uniform verification matching types
        const sourceId = String(typeof l.source === 'object' ? l.source.id : l.source)
        const targetId = String(typeof l.target === 'object' ? l.target.id : l.target)
        const currentSelectedId = String(selectedNode.id)
        
        return validLink && (sourceId === currentSelectedId || targetId === currentSelectedId)
      })
      .map((l) => {
        const sourceId = String(typeof l.source === 'object' ? l.source.id : l.source)
        const targetId = String(typeof l.target === 'object' ? l.target.id : l.target)
        const currentSelectedId = String(selectedNode.id)
        
        const isSource = sourceId === currentSelectedId
        const connectedNodeId = isSource ? targetId : sourceId
        const connectedNode = nodes.find((n) => String(n.id) === connectedNodeId)
        
        return {
          nodeName: connectedNode?.name || 'Unknown',
          label: l.label,
          direction: isSource ? '→' : '←',
          strength: l.strength,
        }
      })
  }

  return (
    <div className="graph-modal-overlay" onClick={onClose}>
      <div className="graph-modal" onClick={(e) => e.stopPropagation()}>
        <div className="graph-modal-header">
          <input
            className="graph-search"
            type="text"
            placeholder="Search nodes..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
          />
          <div className="graph-filter-group">
            <span className="graph-filter-label">Relationship strength</span>
            <select value={filterLevel} onChange={(e) => setFilterLevel(Number(e.target.value))}>
              <option value={1}>High only</option>
              <option value={2}>High + Medium</option>
              <option value={3}>All</option>
            </select>
          </div>
          <button className="graph-close-btn" onClick={onClose}>×</button>
        </div>

        <div className="graph-modal-body">
          {nodes.length === 0 ? (
            <div className="graph-empty-state">
              No graph data yet. Upload documents and click Generate Graph.
            </div>
          ) : (
            <GraphCanvas
              nodes={nodes}
              edges={edges}
              filterLevel={filterLevel}
              searchTerm={searchTerm}
              selectedNode={selectedNode}
              onNodeSelect={setSelectedNode}
            />
          )}
          <NodeDetails
            node={selectedNode}
            onClose={() => setSelectedNode(null)}
            connections={getConnections()}
          />
        </div>
      </div>
    </div>
  )
}