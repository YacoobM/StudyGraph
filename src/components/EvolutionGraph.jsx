import React, { useEffect, useRef } from 'react'
import * as d3 from 'd3'

export default function EvolutionGraph({ nodes, edges, onNodeClick, selectedConceptName }) {
  const svgRef = useRef(null)
  const containerRef = useRef(null)

  useEffect(() => {
    if (!svgRef.current || !containerRef.current || nodes.length === 0) return

    const rect = containerRef.current.getBoundingClientRect()
    const width = rect.width || 600
    const height = rect.height || 400

    const renderNodes = JSON.parse(JSON.stringify(nodes))
    const renderEdges = JSON.parse(JSON.stringify(edges)).map(e => ({ ...e, source: e.from_node || e.source, target: e.to_node || e.target }))

    const svg = d3.select(svgRef.current).attr('viewBox', [0, 0, width, height]).attr('width', width).attr('height', height)
    svg.selectAll('*').remove()

    const g = svg.append('g')
    const zoom = d3.zoom().scaleExtent([0.2, 3]).on('zoom', (e) => g.attr('transform', e.transform))
    svg.call(zoom)

    const simulation = d3.forceSimulation(renderNodes)
      .force('link', d3.forceLink(renderEdges).id(d => d.id).distance(d => d.label === 'evolved to' ? 120 : 250))
      .force('charge', d3.forceManyBody().strength(-1000))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide().radius(80))

    const link = g.append('g').selectAll('g').data(renderEdges).join('g')
    link.append('line').attr('stroke', d => d.label === 'evolved to' ? '#a3e635' : '#475569').attr('stroke-width', d => d.label === 'evolved to' ? 2 : 1.5)
    link.append('text').attr('text-anchor', 'middle').attr('dy', -5).attr('fill', '#a3e635').attr('font-size', '10px').text(d => d.label === 'evolved to' ? 'evolved to' : '')

    const node = g.append('g').selectAll('g').data(renderNodes).join('g').attr('class', 'cursor-pointer')
      .call(d3.drag().on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y }).on('drag', (e, d) => { d.fx = e.x; d.fy = e.y }).on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null }))

    node.append('circle').attr('r', d => d.group === 'concept' ? 20 : (20 + (d.importance || 1) * 4)).attr('fill', d => d.color || '#a3e635').attr('stroke', '#1e293b').attr('stroke-width', 2)
    node.append('text').attr('dy', d => `${(d.group === 'concept' ? 25 : (25 + (d.importance || 1) * 4))}px`).attr('text-anchor', 'middle').attr('fill', 'white').attr('font-size', '13px').attr('paint-order', 'stroke').attr('stroke', '#111827').attr('stroke-width', '3px').attr('stroke-linecap', 'round').attr('stroke-linejoin', 'round').text(d => d.concept_name || d.label)

    node.on('click', (e, d) => { e.stopPropagation(); onNodeClick(d) })

    simulation.on('tick', () => {
      link.select('line').attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y)
      link.select('text').attr('x', d => (d.source.x + d.target.x) / 2).attr('y', d => (d.source.y + d.target.y) / 2)
      node.attr('transform', d => `translate(${d.x},${d.y})`)
    })

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0]
      if (!entry) return
      const newWidth = entry.contentRect.width
      const newHeight = entry.contentRect.height
      if (!newWidth || !newHeight) return
      svg.attr('viewBox', [0, 0, newWidth, newHeight]).attr('width', newWidth).attr('height', newHeight)
      simulation.force('center', d3.forceCenter(newWidth / 2, newHeight / 2)).alpha(0.3).restart()
    })
    resizeObserver.observe(containerRef.current)

    return () => {
      simulation.stop()
      resizeObserver.disconnect()
    }
  }, [nodes, edges, onNodeClick])

  useEffect(() => {
    if (!svgRef.current) return
    const svg = d3.select(svgRef.current)
    svg.selectAll('g.cursor-pointer').transition().duration(200).style('opacity', d => !selectedConceptName || d.concept_name === selectedConceptName ? 1 : 0.3)
    svg.selectAll('circle').transition().duration(200).attr('stroke', d => d.concept_name === selectedConceptName ? '#60a5fa' : '#1e293b').attr('stroke-width', d => d.concept_name === selectedConceptName ? 4 : 2)
  }, [selectedConceptName])

  return (
    <div ref={containerRef} style={{ width: '100%', height: '100%', position: 'relative' }}>
      <svg ref={svgRef} style={{ width: '100%', height: '100%' }}></svg>
    </div>
  )
}