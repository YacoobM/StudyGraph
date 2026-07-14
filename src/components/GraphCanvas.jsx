import React, { useEffect, useRef } from 'react'
import * as d3 from 'd3'

/*
 * GraphCanvas
 * Renders the force-directed knowledge graph using D3.
 * Expects nodes: [{ id, name, description, sources: [{file, page}] }]
 * and edges: [{ id, source, target, label, strength: 'high'|'medium'|'low' }]
 */
export default function GraphCanvas({
  nodes,
  edges,
  filterLevel,
  searchTerm,
  selectedNode,
  highlightedNodeIds = [],
  onNodeSelect,
}) {
  const svgRef = useRef(null)
  const containerRef = useRef(null)
  const zoomBehavior = useRef(null)

  // Pan/zoom to highlighted nodes once the simulation has positioned them
  useEffect(() => {
    if (highlightedNodeIds.length === 0 || !svgRef.current) return

    const timer = setTimeout(() => {
      const highlightedNodes = nodes.filter((n) => highlightedNodeIds.includes(n.id))
      if (highlightedNodes.length === 0) return

      const xs = highlightedNodes.map((n) => n.x ?? 0).filter((x) => x !== 0 && isFinite(x))
      const ys = highlightedNodes.map((n) => n.y ?? 0).filter((y) => y !== 0 && isFinite(y))
      if (xs.length === 0 || ys.length === 0) return

      const minX = Math.min(...xs)
      const maxX = Math.max(...xs)
      const minY = Math.min(...ys)
      const maxY = Math.max(...ys)
      const width = Math.max(100, maxX - minX)
      const height = Math.max(100, maxY - minY)
      const centerX = (minX + maxX) / 2
      const centerY = (minY + maxY) / 2

      const svgWidth = svgRef.current?.clientWidth || 800
      const svgHeight = svgRef.current?.clientHeight || 600
      const scale = Math.max(0.5, Math.min(3, 0.85 / Math.max(width / svgWidth, height / svgHeight)))
      const tx = svgWidth / 2 - centerX * scale
      const ty = svgHeight / 2 - centerY * scale

      if (zoomBehavior.current && svgRef.current) {
        const targetTransform = d3.zoomIdentity.translate(tx, ty).scale(scale)
        zoomBehavior.current.transform(d3.select(svgRef.current), targetTransform)
      }
    }, 100)

    return () => clearTimeout(timer)
  }, [highlightedNodeIds, nodes])

  // Main render effect: builds the simulation, nodes, and edges
  useEffect(() => {
    if (!svgRef.current || !containerRef.current || nodes.length === 0) {
      const svg = d3.select(svgRef.current)
      if (svg) {
        svg.selectAll('*').remove()
        svg.append('text')
          .attr('x', '50%')
          .attr('y', '50%')
          .attr('text-anchor', 'middle')
          .attr('dominant-baseline', 'middle')
          .attr('fill', 'rgba(255,255,255,0.5)')
          .text('No data yet. Upload a document to get started!')
      }
      return
    }

    const width = containerRef.current.clientWidth || 800
    const height = containerRef.current.clientHeight || 600

    // Deep copy so D3's mutation of node/link objects doesn't touch React state
    const renderNodes = JSON.parse(JSON.stringify(nodes))
    const renderedges = JSON.parse(JSON.stringify(edges)).filter((l) => {
      if (filterLevel === 1) return l.strength === 'high'
      if (filterLevel === 2) return l.strength === 'high' || l.strength === 'medium'
      return true
    })

    const svg = d3.select(svgRef.current).attr('width', width).attr('height', height)
    svg.selectAll('*').remove()
    svg.on('click', () => onNodeSelect(null))

    const defs = svg.append('defs')
    defs.append('marker')
      .attr('id', 'arrowhead')
      .attr('viewBox', '0 -5 10 10')
      .attr('refX', 34)
      .attr('refY', 0)
      .attr('orient', 'auto')
      .attr('markerWidth', 6)
      .attr('markerHeight', 6)
      .append('path')
      .attr('d', 'M0,-5L10,0L0,5')
      .attr('fill', 'var(--text-secondary, #888888)')

    defs.append('marker')
      .attr('id', 'arrowhead-high')
      .attr('viewBox', '0 -5 10 10')
      .attr('refX', 25)
      .attr('refY', 0)
      .attr('orient', 'auto')
      .attr('markerWidth', 7)
      .attr('markerHeight', 7)
      .append('path')
      .attr('d', 'M0,-5L10,0L0,5')
      .attr('fill', 'var(--text-primary, #ffffff)')

    const g = svg.append('g')
    const zoom = d3.zoom()
      .scaleExtent([0.1, 4])
      .on('zoom', (e) => g.attr('transform', e.transform))
    svg.call(zoom)
    zoomBehavior.current = zoom

    // FIXED: Convert IDs cleanly to strings to prevent string/number lookup mismatches
    const nodeIds = new Set(renderNodes.map((n) => String(n.id)))
    const validedges = renderedges.filter((link) => {
      const sourceId = String(typeof link.source === 'object' ? link.source.id : link.source)
      const targetId = String(typeof link.target === 'object' ? link.target.id : link.target)
      return nodeIds.has(sourceId) && nodeIds.has(targetId)
    })

    const simulation = d3.forceSimulation(renderNodes)
      .force('link', d3.forceLink(validedges).id((d) => String(d.id)).distance(200))
      .force('charge', d3.forceManyBody().strength(-800))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius(70))

    const link = g.append('g').selectAll('.link').data(validedges).enter().append('g').attr('class', 'link')
    const linkPath = link.append('line')
      .attr('marker-end', (d) => (d.strength === 'high' ? 'url(#arrowhead-high)' : 'url(#arrowhead)'))
      .attr('stroke', (d) => (d.strength === 'high' ? 'var(--text-primary, #ffffff)' : 'var(--text-secondary, #888888)'))
      .attr('stroke-width', (d) => (d.strength === 'high' ? 2 : 1))
      .attr('stroke-dasharray', (d) => (d.strength === 'low' ? '4,4' : 'none'))

    const node = g.append('g').selectAll('.node').data(renderNodes).enter().append('g').attr('class', 'node')
      .call(d3.drag()
        .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y })
        .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y })
        .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null })
      )

    // FIXED: Added HEX color fallbacks to stop nodes from defaulting to pure black
    node.append('circle')
      .attr('r', (d) => (highlightedNodeIds.includes(d.id) ? 30 : 25))
      .attr('fill', (d) => d.color || 'var(--primary-color, #10b981)')
      .attr('stroke', (d) => (highlightedNodeIds.includes(d.id) ? '#6B9EFF' : 'none'))
      .attr('stroke-width', (d) => (highlightedNodeIds.includes(d.id) ? 2 : 0))
      .attr('filter', (d) => (highlightedNodeIds.includes(d.id) ? 'drop-shadow(0 0 6px rgba(107, 158, 255, 0.6))' : 'none'))

    node.append('text')
      .attr('dy', (d) => (highlightedNodeIds.includes(d.id) ? 50 : 45))
      .attr('text-anchor', 'middle')
      .attr('font-weight', (d) => (highlightedNodeIds.includes(d.id) ? 'bold' : 'normal'))
      .attr('font-size', '12px')
      .attr('fill', (d) => (highlightedNodeIds.includes(d.id) ? '#6B9EFF' : 'white'))
      .text((d) => d.name)

    node.on('click', (e, d) => { e.stopPropagation(); onNodeSelect(d) })

    simulation.on('tick', () => {
      linkPath
        .attr('x1', (d) => d.source.x)
        .attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x)
        .attr('y2', (d) => d.target.y)
      node.attr('transform', (d) => `translate(${d.x},${d.y})`)
    })

    return () => { simulation.stop() }
  }, [nodes, edges, filterLevel, onNodeSelect, highlightedNodeIds])

  // Search and selection highlighting, applied without re-running the simulation
  useEffect(() => {
    if (!svgRef.current) return
    const svg = d3.select(svgRef.current)
    const nodesG = svg.selectAll('.node')
    const edgesG = svg.selectAll('.link')

    // FIXED: Ensured strict type alignment using String() matching during selection checks
    if (selectedNode) {
      nodesG.style('opacity', (n) => {
        const isConn = edges.some((l) => {
          const validLink = filterLevel === 3 || (filterLevel === 2 && l.strength !== 'low') || (filterLevel === 1 && l.strength === 'high')
          const sourceId = String(typeof l.source === 'object' ? l.source.id : l.source)
          const targetId = String(typeof l.target === 'object' ? l.target.id : l.target)
          const currentSelectedId = String(selectedNode.id)
          const currentNodeId = String(n.id)

          return validLink && (
            (sourceId === currentNodeId && targetId === currentSelectedId) || 
            (targetId === currentNodeId && sourceId === currentSelectedId)
          )
        })
        return String(n.id) === String(selectedNode.id) || isConn ? 1 : 0.15
      })
      nodesG.select('circle').attr('stroke', (n) => (String(n.id) === String(selectedNode.id) ? 'white' : 'none'))
      edgesG.style('opacity', (l) => {
        const sourceId = String(typeof l.source === 'object' ? l.source.id : l.source)
        const targetId = String(typeof l.target === 'object' ? l.target.id : l.target)
        const currentSelectedId = String(selectedNode.id)
        return sourceId === currentSelectedId || targetId === currentSelectedId ? 1 : 0.05
      })
    } else if (searchTerm) {
      const term = searchTerm.toLowerCase()
      nodesG.style('opacity', (d) => (d.name?.toLowerCase().includes(term) ? 1 : 0.15))
      nodesG.select('circle').attr('stroke', 'none')
      edgesG.style('opacity', (d) => {
        const sourceName = d.source.name || ''
        const targetName = d.target.name || ''
        return sourceName.toLowerCase().includes(term) || targetName.toLowerCase().includes(term) ? 1 : 0.05
      })
    } else {
      nodesG.style('opacity', 1).select('circle').attr('stroke', 'none')
      edgesG.style('opacity', 1)
    }
  }, [selectedNode, searchTerm, filterLevel, edges, nodes])

  const handleResetZoom = () => {
    if (zoomBehavior.current && svgRef.current) {
      d3.select(svgRef.current).transition().call(zoomBehavior.current.transform, d3.zoomIdentity)
    }
  }

  return (
    <div style={{ width: '100%', height: '100%', position: 'absolute', top: 0, left: 0 }} ref={containerRef}>
      <div className={`zoom-controls ${selectedNode ? 'shifted' : ''}`}>
        <button className="zoom-btn" onClick={() => d3.select(svgRef.current).transition().call(zoomBehavior.current.scaleBy, 1.2)}>+</button>
        <button className="zoom-btn fit" onClick={handleResetZoom}>Fit</button>
        <button className="zoom-btn" onClick={() => d3.select(svgRef.current).transition().call(zoomBehavior.current.scaleBy, 0.8)}>-</button>
      </div>
      <svg ref={svgRef} style={{ width: '100%', height: '100%' }}></svg>
    </div>
  )
}