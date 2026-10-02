import React, { useRef, useState } from 'react'
import GraphCanvas from './GraphCanvas'
import NodeDetails from './NodeDetails'
import GraphLegend from './GraphLegend'
import PdfViewerModal from './PdfViewerModal'
import { fetchConceptDescription, fetchConcept, fetchConceptSubgraph, regenerateConcept, getDocumentFileUrl } from '../services/api.js'
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
export default function GraphViewer({
  nodes = [],
  edges = [],
  legend = null,
  graphStatus = 'active',
  onClose,
  onGraphRegenerate,
}) {
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedNode, setSelectedNode] = useState(null)
  const [filterLevel, setFilterLevel] = useState(3)
  const [conceptDescription, setConceptDescription] = useState(null)
  const [detailsLoading, setDetailsLoading] = useState(false)
  const [regenerating, setRegenerating] = useState(false)
  const [graphRegenerating, setGraphRegenerating] = useState(false)
  const [toast, setToast] = useState(null)
  const toastTimerRef = useRef(null)

  // Full evolution (all weeks + comparisons + timeline) is deliberately not
  // fetched on plain node select — it's the expensive tier, only loaded when
  // the user explicitly presses "View Full Evolution", and shown inline in
  // NodeDetails rather than on a separate page/graph.
  const [evolution, setEvolution] = useState(null)
  const [evolutionLoading, setEvolutionLoading] = useState(false)

  const [pdfSource, setPdfSource] = useState(null) // { fileUrl, fileName, page, highlightText }

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
          summary: l.summary || l.properties?.summary || '',
          context: l.context || l.properties?.context || '',
        }
      })
  }

  const handleNodeSelect = (node) => {
    setSelectedNode(node)
    setConceptDescription(null) // Clear previous description
    setEvolution(null) // A newly selected node's evolution hasn't been requested yet
    if (node) {
      setDetailsLoading(true)
      fetchConceptDescription(node.name)
        .then(setConceptDescription)
        .catch(() => setConceptDescription(null)) // Ensure it's null on failure
        .finally(() => setDetailsLoading(false))
    }
  }

  const handleShowEvolution = async (name) => {
    setEvolutionLoading(true)
    try {
      const [details, subgraph] = await Promise.all([
        fetchConcept(name),
        fetchConceptSubgraph(name),
      ])
      setEvolution({ details, subgraph })
    } catch (e) {
      setEvolution(null)
    } finally {
      setEvolutionLoading(false)
    }
  }

  const showToast = (message) => {
    setToast(message)
    if (toastTimerRef.current) {
      clearTimeout(toastTimerRef.current)
    }
    toastTimerRef.current = setTimeout(() => setToast(null), 2800)
  }

  const handleRegenerate = async () => {
    if (!selectedNode) return
    if (graphStatus !== 'needs_regeneration') {
      showToast('This graph is still active — no concepts currently need regeneration.')
      return
    }
    setRegenerating(true)
    try {
      await regenerateConcept(selectedNode.name)
      const desc = await fetchConceptDescription(selectedNode.name).catch(() => null)
      setConceptDescription(desc)
      if (evolution) {
        await handleShowEvolution(selectedNode.name)
      }
    } finally {
      setRegenerating(false)
    }
  }

  const handleGraphRegenerate = async () => {
    if (graphStatus !== 'needs_regeneration') {
      showToast('This graph is still active — no rebuild is needed.')
      return
    }
    if (!onGraphRegenerate) {
      showToast('Graph regeneration is unavailable right now.')
      return
    }
    setGraphRegenerating(true)
    try {
      await onGraphRegenerate()
    } finally {
      setGraphRegenerating(false)
    }
  }

  const handleOpenSource = (src, conceptName) => {
    setPdfSource({
      fileUrl: getDocumentFileUrl(src.file),
      fileName: src.file,
      page: src.page || 1,
      highlightText: conceptName,
    })
  }

  return (
    <div className="graph-modal-overlay" onClick={onClose}>
      <div className="graph-modal" onClick={(e) => e.stopPropagation()}>
        {toast && <div className="graph-toast">{toast}</div>}
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
          <button
            className="graph-regenerate-btn"
            onClick={handleGraphRegenerate}
            disabled={graphRegenerating || graphStatus !== 'needs_regeneration'}
            title={
              graphStatus === 'needs_regeneration'
                ? 'Rebuild the graph from current evidence'
                : 'This graph is still active — no rebuild is required.'
            }
          >
            {graphRegenerating ? 'Rebuilding…' : 'Rebuild graph'}
          </button>
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
              onNodeSelect={handleNodeSelect}
            />
          )}
          {legend && <GraphLegend legend={legend} />}
          <NodeDetails
            node={selectedNode}
            onClose={() => setSelectedNode(null)}
            connections={getConnections()}
            conceptDescription={conceptDescription}
            detailsLoading={detailsLoading}
            evolution={evolution}
            evolutionLoading={evolutionLoading}
            onShowEvolution={handleShowEvolution}
            onRegenerate={handleRegenerate}
            regenerating={regenerating}
            graphStatus={graphStatus}
            onOpenSource={handleOpenSource}
          />
        </div>
      </div>

      {pdfSource && (
        <PdfViewerModal
          fileUrl={pdfSource.fileUrl}
          fileName={pdfSource.fileName}
          page={pdfSource.page}
          highlightText={pdfSource.highlightText}
          onClose={() => setPdfSource(null)}
        />
      )}
    </div>
  )
}
