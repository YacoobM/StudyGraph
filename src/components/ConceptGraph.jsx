import React, { useCallback, useEffect, useRef, useState } from 'react'
import * as d3 from 'd3'

/**
 * Concept graph — a direct port of the prototype's gxBuild, including the
 * furniture around the canvas: legend, search, zoom/fit/re-layout toolbar,
 * hover tooltip, drag hint and graph-version timeline.
 *
 * The physics settings are the prototype's and are load-bearing, not
 * decoration: velocityDecay 0.62 gives heavy friction so nodes glide rather
 * than jitter, alphaDecay 0.05 cools the layout in about two seconds, and
 * alphaMin 0.008 stops it early instead of letting it drift. When the
 * simulation ends every node is pinned, so the graph sits still until you drag
 * something or press Re-layout.
 *
 * It draws on a dark card, as the design does — hence the light strokes.
 */

const R = (d) => (d.kind === 'core' ? 13 : 8.5)

export default function ConceptGraph({
  nodes: allNodes = [],
  edges: allEdges = [],
  versions = [],
  selectedVersionId,
  onVersionChange,
  loadingVersion,
  onSelectNode,
  selectedId,
}) {
  const svgRef = useRef(null)
  const wrapRef = useRef(null)
  const posRef = useRef({})            // id -> {x,y,fx,fy}, survives re-renders
  const zoomRef = useRef(null)
  const simRef = useRef(null)
  const [search, setSearch] = useState('')
  const [tip, setTip] = useState(null) // {x, y, name, rel}

  const neighbours = useCallback(
    (id) => {
      const s = new Set()
      allEdges.forEach((e) => {
        const a = e.source?.id ?? e.source
        const b = e.target?.id ?? e.target
        if (a === id) s.add(b)
        if (b === id) s.add(a)
      })
      return s
    },
    [allEdges],
  )

  const fill = useCallback(
    (d) => d.kind === 'core' ? 'rgba(243,238,221,0.92)' : 'rgba(150,137,122,0.85)',
    [],
  )

  /* Re-tint and dim without rebuilding the simulation. */
  useEffect(() => {
    const root = d3.select(svgRef.current).select('g.gx-root')
    if (root.empty()) return
    const term = search.trim().toLowerCase()
    const nodeSel = root.selectAll('.gx-node')
    const edgeSel = root.selectAll('.gx-edge')

    nodeSel.select('circle').attr('fill', fill)
    nodeSel.select('circle').attr('stroke', (d) =>
      String(d.id) === String(selectedId) ? 'rgba(243,238,221,0.95)' : 'rgba(0,0,0,0.35)')
    nodeSel.select('circle').attr('stroke-width', (d) =>
      String(d.id) === String(selectedId) ? 2.5 : 1.5)

    if (selectedId) {
      const near = neighbours(selectedId)
      near.add(selectedId)
      nodeSel.style('opacity', (d) => (near.has(d.id) ? 1 : 0.18))
      edgeSel.style('opacity', (d) => {
        const a = d.source?.id ?? d.source
        const b = d.target?.id ?? d.target
        return near.has(a) && near.has(b) ? 1 : 0.08
      })
    } else if (term) {
      nodeSel.style('opacity', (d) => (d.name.toLowerCase().includes(term) ? 1 : 0.15))
      edgeSel.style('opacity', 0.12)
    } else {
      nodeSel.style('opacity', 1)
      edgeSel.style('opacity', 1)
    }
  }, [selectedId, search, fill, neighbours])

  // --- build ---------------------------------------------------------------
  useEffect(() => {
    const svgEl = svgRef.current
    const wrap = wrapRef.current
    if (!svgEl || !wrap || !allNodes.length) return

    const W = wrap.clientWidth || 900
    const H = wrap.clientHeight || 600

    const nodes = allNodes.map((n) => ({ ...n, ...(posRef.current[n.id] || {}) }))
    const ids = new Set(nodes.map((n) => n.id))
    const edges = allEdges
      .filter((e) => ids.has(e.source?.id ?? e.source) && ids.has(e.target?.id ?? e.target))
      .map((e) => ({ ...e }))

    const svg = d3.select(svgEl)
    svg.selectAll('*').remove()
    svg.attr('viewBox', `0 0 ${W} ${H}`)

    const defs = svg.append('defs')
    ;[['gx-arrow', 'rgba(255,255,255,0.45)'], ['gx-arrow-hi', 'rgba(243,238,221,0.9)']]
      .forEach(([id, f]) => {
        defs.append('marker')
          .attr('id', id).attr('viewBox', '0 -5 10 10')
          .attr('refX', 26).attr('refY', 0).attr('orient', 'auto')
          .attr('markerWidth', 6).attr('markerHeight', 6)
          .append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', f)
      })

    const root = svg.append('g').attr('class', 'gx-root')
    const zoom = d3.zoom().scaleExtent([0.25, 3])
      .on('zoom', (e) => root.attr('transform', e.transform))
    zoomRef.current = zoom
    svg.call(zoom).on('click', () => onSelectNode && onSelectNode(null))

    const sim = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(edges).id((d) => d.id)
        .distance((d) => (d.strength === 'high' ? 120 : d.strength === 'medium' ? 150 : 180))
        .strength((d) => (d.strength === 'high' ? 0.5 : 0.3)))
      .force('charge', d3.forceManyBody().strength(-520).distanceMax(420))
      .force('center', d3.forceCenter(W / 2, H / 2))
      .force('collide', d3.forceCollide().radius((d) => R(d) + 26).strength(1).iterations(2))
      .force('x', d3.forceX(W / 2).strength(0.045))
      .force('y', d3.forceY(H / 2).strength(0.045))
      .velocityDecay(0.62)
      .alphaDecay(0.05)
      .alphaMin(0.008)
    simRef.current = sim

    const remember = () => nodes.forEach((n) => {
      posRef.current[n.id] = { x: n.x, y: n.y, fx: n.fx, fy: n.fy }
    })

    const edgeG = root.append('g').attr('class', 'gx-edges')
      .selectAll('g').data(edges).enter().append('g').attr('class', 'gx-edge')
    edgeG.append('line')
      .attr('stroke', (d) => (d.strength === 'high' ? 'rgba(243,238,221,0.55)' : 'rgba(255,255,255,0.18)'))
      .attr('stroke-width', (d) => (d.strength === 'high' ? 1.6 : 1))
      .attr('stroke-dasharray', (d) => (d.strength === 'low' ? '4,4' : 'none'))
      .attr('marker-end', (d) => (d.strength === 'high' ? 'url(#gx-arrow-hi)' : 'url(#gx-arrow)'))
    edgeG.append('text').attr('class', 'gx-edge-label')
      .attr('text-anchor', 'middle').text((d) => d.label)

    const nodeG = root.append('g').attr('class', 'gx-nodes')
      .selectAll('g').data(nodes).enter().append('g').attr('class', 'gx-node')
      .style('cursor', 'pointer')
      .call(d3.drag()
        .on('start', (e, d) => {
          // free only the direct neighbours, so the drag stays local
          const near = neighbours(d.id)
          nodes.forEach((n) => { if (near.has(n.id)) { n.fx = null; n.fy = null } })
          if (!e.active) sim.alphaTarget(0.1).restart()
          d.fx = d.x; d.fy = d.y
        })
        .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y })
        .on('end', (e) => {
          // re-pin everything where it landed: nothing springs back
          if (!e.active) sim.alphaTarget(0)
          nodes.forEach((n) => { n.fx = n.x; n.fy = n.y })
          remember()
        }))
      .on('click', (e, d) => {
        e.stopPropagation()
        if (onSelectNode) onSelectNode(d)
      })
      .on('mouseenter', (e, d) => {
        const box = wrap.getBoundingClientRect()
        const rel = edges
          .filter((x) => (x.source.id ?? x.source) === d.id || (x.target.id ?? x.target) === d.id)
          .length
        setTip({ x: e.clientX - box.left, y: e.clientY - box.top, name: d.name, rel })
      })
      .on('mouseleave', () => setTip(null))

    nodeG.append('circle')
      .attr('r', R).attr('fill', fill)
      .attr('stroke', 'rgba(0,0,0,0.35)').attr('stroke-width', 1.5)

    nodeG.append('text')
      .attr('text-anchor', 'middle')
      .attr('dy', (d) => R(d) + 14)
      .attr('font-size', (d) => (d.kind === 'core' ? '11.5px' : '10.5px'))
      .attr('font-weight', (d) => (d.kind === 'core' ? 600 : 500))
      .attr('fill', (d) => (d.kind === 'core' ? 'rgba(243,238,221,0.95)' : 'rgba(150,137,122,0.95)'))
      .attr('paint-order', 'stroke')
      .attr('stroke', 'rgba(23,19,9,0.85)').attr('stroke-width', '3px')
      .text((d) => d.name)

    sim.on('tick', () => {
      edgeG.select('line')
        .attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y)
      edgeG.select('text')
        .attr('x', (d) => (d.source.x + d.target.x) / 2)
        .attr('y', (d) => (d.source.y + d.target.y) / 2 - 5)
      nodeG.attr('transform', (d) => `translate(${d.x},${d.y})`)
    })

    sim.on('end', () => {
      nodes.forEach((n) => { n.fx = n.x; n.fy = n.y })
      remember()
    })

    return () => sim.stop()
  }, [allNodes, allEdges])   // eslint-disable-line react-hooks/exhaustive-deps

  // --- toolbar -------------------------------------------------------------
  const zoomBy = (k) => {
    const svg = d3.select(svgRef.current)
    if (zoomRef.current) svg.transition().duration(220).call(zoomRef.current.scaleBy, k)
  }
  const fit = () => {
    const svg = d3.select(svgRef.current)
    if (zoomRef.current) svg.transition().duration(300).call(zoomRef.current.transform, d3.zoomIdentity)
  }
  const relayout = () => {
    posRef.current = {}
    const sim = simRef.current
    if (!sim) return
    sim.nodes().forEach((n) => { n.fx = null; n.fy = null })
    sim.alpha(0.9).restart()
  }

  return (
    <div className="graph-panel-inner" ref={wrapRef}>
      <div className="ws-graph-legend">
        <div className="row">
          <span className="swatch" style={{ background: 'var(--night-text)' }} /> Core concept
        </div>
        <div className="row">
          <span className="swatch" style={{ background: 'var(--night-slate)' }} /> Supporting idea
        </div>
      </div>

      <svg ref={svgRef} id="wsGraphSvg" />

      <div className="gx-search">
        <span style={{ color: 'var(--night-slate)', fontSize: 12 }}>⌂</span>
        <input
          value={search}
          placeholder="Search concepts"
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <div className="gx-toolbar">
        <button className="gx-btn" onClick={() => zoomBy(1.3)} title="Zoom in">+</button>
        <button className="gx-btn" onClick={() => zoomBy(0.75)} title="Zoom out">−</button>
        <button className="gx-btn" onClick={fit} title="Fit to screen">Fit</button>
        <button className="gx-btn" onClick={relayout} title="Re-run the layout">↻ Re-layout</button>
      </div>

      <div className="gx-hint">Drag a node to pin it · click to inspect</div>

      {tip && (
        <div
          className="node-tip show"
          style={{ left: tip.x + 14, top: tip.y + 14 }}
        >
          <b>{tip.name}</b><br />
          {tip.rel} connection{tip.rel === 1 ? '' : 's'}
        </div>
      )}

      <GraphVersionTimeline
        versions={versions}
        selectedVersionId={selectedVersionId}
        onVersionChange={onVersionChange}
        loading={loadingVersion}
      />

    </div>
  )
}

export function GraphVersionTimeline({ versions, selectedVersionId, onVersionChange, loading }) {
  const chronological = [...versions].sort(
    (a, b) => Number(a.version_number) - Number(b.version_number),
  )
  const selectedIndex = Math.max(
    0,
    chronological.findIndex((version) => version.id === selectedVersionId),
  )
  const selected = chronological[selectedIndex]
  if (!selected) return null

  return (
    <div className="ws-timeline" aria-label="Graph version timeline">
      <div className="ws-timeline-head">
        <span>
          Version <span className="num">{selected.version_number}</span> of {chronological[chronological.length - 1].version_number}
        </span>
        <span>{loading ? 'Loading version…' : 'Click a node to inspect'}</span>
      </div>
      <input
        type="range"
        min="0"
        max={chronological.length - 1}
        value={selectedIndex}
        step="1"
        disabled={loading || chronological.length < 2}
        aria-label="Choose graph version"
        aria-valuetext={`Version ${selected.version_number}`}
        onChange={(event) => onVersionChange?.(chronological[Number(event.target.value)]?.id)}
      />
    </div>
  )
}
