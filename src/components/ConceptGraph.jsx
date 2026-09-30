import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'

/**
 * Concept graph.
 *
 * The props and the data shapes are the ones Workspace already passes — node
 * {id, name, week, weeks, degree, kind, description, sources} and edge
 * {source, target, label, strength} — so nothing about the API contract or the
 * backend's graph payload is involved here.
 *
 * Three effects, deliberately separated, because conflating them is what makes
 * force graphs stutter:
 *
 *   mount   builds the SVG scaffold, the zoom behaviour and ONE simulation
 *   data    re-binds nodes/links through d3 joins when the visible set changes
 *   style   re-paints opacity, emphasis and label visibility only
 *
 * The simulation is never torn down and rebuilt, so node positions survive a
 * week change, a resize, a filter and every React re-render.
 *
 * Readability comes from three things working together: node size and label
 * weight follow how connected a concept is; labels are revealed by importance
 * and zoom rather than all at once; and relationship labels stay hidden until
 * you ask for them, by hovering a link, selecting a concept, or switching them
 * on in the toolbar.
 */

const CHAR_W = 6.0          // Inter at 10.5px, rounded up to cover the label halo
const LINE_H = 11.5

/* Zoom at which each tier's label becomes readable enough to be worth drawing. */
const LABEL_ZOOM = { high: 0, mid: 0.85, low: 1.45 }

function wrapLabel(name) {
  const words = String(name).split(' ')
  if (words.length < 2 || name.length <= 15) return [name]
  let best = null
  for (let i = 1; i < words.length; i += 1) {
    const a = words.slice(0, i).join(' ')
    const b = words.slice(i).join(' ')
    const score = Math.abs(a.length - b.length)
    if (!best || score < best.score) best = { lines: [a, b], score }
  }
  // A third line costs more than it buys; truncate the tail instead.
  return best.lines.map((l) => (l.length > 26 ? `${l.slice(0, 25)}…` : l))
}

/* Stable pseudo-random angle from an id, so a re-layout is reproducible. */
function spread(id) {
  let h = 2166136261
  const s = String(id)
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) / 4294967296) * Math.PI * 2
}

export default function ConceptGraph({
  nodes: allNodes = [],
  edges: allEdges = [],
  week,
  maxWeek,
  onWeekChange,
  onSelectNode,
  selectedId,
}) {
  const svgRef = useRef(null)
  const wrapRef = useRef(null)
  const simRef = useRef(null)
  const zoomRef = useRef(null)
  const posRef = useRef({})               // id -> {x, y}, survives every rebind
  const sizeRef = useRef({ w: 900, h: 600 })
  const zoomKRef = useRef(1)
  const styleRef = useRef(null)           // latest applyStyles, callable from d3 handlers
  const hoverRef = useRef({ node: null, edge: null })

  const [search, setSearch] = useState('')
  const [showRelations, setShowRelations] = useState(false)
  const [hideIsolated, setHideIsolated] = useState(false)
  const [tip, setTip] = useState(null)

  // Held in refs so the d3 handlers created once at mount always see the
  // current callback without the effect having to depend on it.
  const onSelectNodeRef = useRef(onSelectNode)
  const retargetRef = useRef(null)
  const placeLabelsRef = useRef(null)
  useEffect(() => { onSelectNodeRef.current = onSelectNode }, [onSelectNode])

  // --- what is on screen -----------------------------------------------------

  /**
   * Everything derived from the props in one place, so the data effect below
   * has a single dependency and React's own re-renders cannot cause a rebind.
   *
   * Importance is a frontend-only reading of node degree within the currently
   * visible subgraph. Nothing is added to or taken from the backend's data.
   */
  const view = useMemo(() => {
    const inWeek = allNodes.filter((n) => (n.week || 1) <= week)
    const ids = new Set(inWeek.map((n) => String(n.id)))

    const edges = allEdges
      .map((e) => ({
        source: String(e.source?.id ?? e.source),
        target: String(e.target?.id ?? e.target),
        label: e.label || 'relates to',
        strength: e.strength || 'medium',
      }))
      .filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target)

    const degree = {}
    edges.forEach((e) => {
      degree[e.source] = (degree[e.source] || 0) + 1
      degree[e.target] = (degree[e.target] || 0) + 1
    })

    const maxDeg = Math.max(1, ...Object.values(degree))
    const sorted = inWeek.map((n) => degree[String(n.id)] || 0).sort((a, b) => b - a)
    const highCut = sorted[Math.max(0, Math.floor(sorted.length * 0.25) - 1)] ?? 1
    const lowCut = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.6))] ?? 0

    let nodes = inWeek.map((n) => {
      const id = String(n.id)
      const deg = degree[id] || 0
      const tier = deg >= Math.max(1, highCut) ? 'high' : deg <= lowCut ? 'low' : 'mid'
      const lines = wrapLabel(n.name || id.replace(/_/g, ' '))
      return {
        ...n,
        id,
        name: n.name || id.replace(/_/g, ' '),
        deg,
        tier,
        isolated: deg === 0,
        lines,
        labelHalf: (Math.max(...lines.map((l) => l.length)) * CHAR_W) / 2,
        r: deg === 0 ? 6 : Math.min(18, 7.5 + 10 * (deg / maxDeg) ** 0.7),
      }
    })
    if (hideIsolated) {
      const keep = new Set(nodes.filter((n) => !n.isolated).map((n) => n.id))
      nodes = nodes.filter((n) => keep.has(n.id))
    }

    const neighbours = new Map(nodes.map((n) => [n.id, new Set()]))
    edges.forEach((e) => {
      neighbours.get(e.source)?.add(e.target)
      neighbours.get(e.target)?.add(e.source)
    })

    return {
      nodes,
      edges,
      neighbours,
      isolatedCount: inWeek.length - inWeek.filter((n) => (degree[String(n.id)] || 0) > 0).length,
    }
  }, [allNodes, allEdges, week, hideIsolated])

  const suggestions = useMemo(() => {
    const term = search.trim().toLowerCase()
    if (!term) return []
    return view.nodes
      .filter((n) => n.name.toLowerCase().includes(term))
      .slice(0, 8)
      .map((n) => n.name)
  }, [search, view.nodes])

  // --- styling ---------------------------------------------------------------

  /**
   * Repaints emphasis without touching the simulation. Called by the style
   * effect, and directly by the zoom and hover handlers so neither has to go
   * through React state on every frame.
   */
  const applyStyles = useCallback(() => {
    const svg = d3.select(svgRef.current)
    const root = svg.select('g.gx-root')
    if (root.empty()) return

    const k = zoomKRef.current
    const term = search.trim().toLowerCase()
    const hoverNode = hoverRef.current.node
    const hoverEdge = hoverRef.current.edge
    const focus = selectedId ? String(selectedId) : hoverNode

    const near = focus ? new Set(view.neighbours.get(focus) || []) : null
    if (near) near.add(focus)

    const matches = (d) => term && d.name.toLowerCase().includes(term)

    const nodeSel = root.selectAll('.gx-node')
    const edgeSel = root.selectAll('.gx-edge')

    // --- nodes
    nodeSel.style('opacity', (d) => {
      if (near) return near.has(d.id) ? 1 : selectedId ? 0.1 : 0.22
      if (term) return matches(d) ? 1 : 0.14
      return 1
    })

    nodeSel.select('circle')
      .attr('r', (d) => d.r * (d.id === focus ? 1.22 : 1))
      .attr('stroke', (d) => (
        d.id === String(selectedId) ? 'rgba(243,238,221,0.96)'
          : matches(d) ? '#E0A06A'
            : 'rgba(0,0,0,0.42)'))
      .attr('stroke-width', (d) => (d.id === String(selectedId) || matches(d) ? 2.4 : 1.4))

    // The label rule: importance sets a zoom threshold, and being involved in
    // what the user is currently looking at overrides it.
    nodeSel.select('text').style('display', (d) => {
      const involved = d.id === focus || near?.has(d.id) || matches(d)
      return involved || k >= LABEL_ZOOM[d.tier] ? null : 'none'
    })
    nodeSel.select('text')
      .attr('font-weight', (d) => (d.id === focus ? 700 : d.tier === 'high' ? 600 : 500))
      .attr('fill', (d) => (
        d.id === focus ? '#F7F2E4'
          : d.tier === 'high' ? 'rgba(243,238,221,0.94)'
            : 'rgba(176,165,150,0.94)'))

    // --- edges
    const touchesFocus = (d) => {
      const a = d.source.id ?? d.source
      const b = d.target.id ?? d.target
      return near ? near.has(a) && near.has(b) : false
    }
    edgeSel.style('opacity', (d) => {
      if (near) return touchesFocus(d) ? 1 : selectedId ? 0.05 : 0.12
      if (term) return 0.12
      return 0.85
    })
    edgeSel.select('line.gx-wire')
      .attr('stroke', (d) => (touchesFocus(d) ? 'rgba(243,238,221,0.6)' : 'rgba(255,255,255,0.17)'))
      .attr('stroke-width', (d) => (touchesFocus(d) ? 1.7 : 1))

    // Relationship labels: only for what is being inspected, or all of them on
    // request. Showing every one at once is what made the graph unreadable.
    // "Inspected" means edges attached to the focused concept itself — an edge
    // between two of its neighbours is not one of its connections.
    const incidentToFocus = (d) => (
      focus ? (d.source.id ?? d.source) === focus || (d.target.id ?? d.target) === focus : false
    )
    edgeSel.select('text').style('display', (d) => {
      if (d === hoverEdge) return null
      if (showRelations && k >= 0.7) return null
      if (incidentToFocus(d)) return null
      return 'none'
    })
  }, [selectedId, search, showRelations, view])

  useEffect(() => { styleRef.current = applyStyles })
  useEffect(() => { applyStyles() }, [applyStyles])

  // --- mount: scaffold, zoom, one simulation ---------------------------------

  useEffect(() => {
    const svgEl = svgRef.current
    const wrap = wrapRef.current
    if (!svgEl || !wrap) return

    const svg = d3.select(svgEl)
    svg.selectAll('*').remove()

    const defs = svg.append('defs')
    ;[['gx-arrow', 'rgba(255,255,255,0.4)'], ['gx-arrow-hi', 'rgba(243,238,221,0.85)']]
      .forEach(([id, fill]) => {
        defs.append('marker')
          .attr('id', id).attr('viewBox', '0 -5 10 10')
          .attr('refX', 22).attr('refY', 0).attr('orient', 'auto')
          .attr('markerWidth', 5).attr('markerHeight', 5)
          .append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', fill)
      })

    const root = svg.append('g').attr('class', 'gx-root')
    root.append('g').attr('class', 'gx-edges')
    root.append('g').attr('class', 'gx-nodes')

    const zoom = d3.zoom().scaleExtent([0.25, 3.2])
      .on('zoom', (event) => {
        root.attr('transform', event.transform)
        zoomKRef.current = event.transform.k
        styleRef.current?.()          // label thresholds are zoom-dependent
      })
    zoomRef.current = zoom
    svg.call(zoom)
      .on('click', () => {
        // clicking empty canvas clears the focus, as the spec asks
        hoverRef.current = { node: null, edge: null }
        onSelectNodeRef.current?.(null)
      })
      .on('dblclick.zoom', null)

    const sim = d3.forceSimulation([])
      .force('link', d3.forceLink([]).id((d) => d.id)
        .distance((d) => (d.strength === 'high' ? 110 : d.strength === 'medium' ? 140 : 170))
        .strength((d) => (d.strength === 'high' ? 0.5 : 0.3)))
      .force('charge', d3.forceManyBody().strength(-560).distanceMax(460))
      // Collision accounts for the label, not just the circle — overlapping
      // text was the main readability complaint.
      .force('collide', d3.forceCollide()
        .radius((d) => Math.min(58, Math.max(d.r + 16, (d.labelHalf || 20) * 0.78)))
        .strength(0.95).iterations(2))
      .velocityDecay(0.62)
      .alphaDecay(0.045)
      .alphaMin(0.008)
    simRef.current = sim

    const remember = () => sim.nodes().forEach((n) => {
      posRef.current[n.id] = { x: n.x, y: n.y }
    })

    /* Relationship labels sit a fixed distance along the edge from its source
       rather than at the midpoint. Several edges meeting at one hub then fan
       out around it instead of stacking on top of each other. */
    const edgeLabelAt = (d) => {
      const dx = d.target.x - d.source.x
      const dy = d.target.y - d.source.y
      const len = Math.hypot(dx, dy) || 1
      const base = Math.min(62, len * 0.44)
      // three radii, picked deterministically per edge, so edges leaving the
      // same node at similar angles do not park their labels on top of one
      // another. Clamped to clear the source circle and stop short of the target.
      const lo = (d.source.r || 9) + 24
      const hi = Math.max(lo, len - (d.target.r || 9) - 18)
      const at = Math.min(hi, Math.max(lo, base * (0.62 + 0.19 * (d.index % 3))))
      return { x: d.source.x + (dx / len) * at, y: d.source.y + (dy / len) * at - 5 }
    }

    /* Labels hang off their node in the direction that faces away from that
       node's own neighbours — the emptier side, by construction — and any
       pair that still lands on top of another is rotated a quarter turn at a
       time until it clears. Circular collision alone cannot fix this: the
       text sits outside the circle it is attached to. */
    const place = (n, dx, dy) => {
      const off = n.r + 9 + (n.pushOut || 0)
      const lines = n.lines.length
      const wHalf = n.labelHalf || 20
      const lx = dx * off
      const ly = dy * off
      n.anchor = dx > 0.45 ? 'start' : dx < -0.45 ? 'end' : 'middle'
      n.lx = lx
      if (dy > 0.45) n.ly0 = ly + LINE_H
      else if (dy < -0.45) n.ly0 = ly - 4 - (lines - 1) * LINE_H
      else n.ly0 = ly - ((lines - 1) * LINE_H) / 2 + 3.5
      const x0 = n.anchor === 'start' ? lx : n.anchor === 'end' ? lx - wHalf * 2 : lx - wHalf
      const PAD = 3
      n.box = { x0: x0 - PAD, x1: x0 + wHalf * 2 + PAD,
                y0: n.ly0 - 9 - PAD, y1: n.ly0 + (lines - 1) * LINE_H + 3 + PAD }
    }

    const hits = (a, b) => (
      Math.min(a.x + a.box.x1, b.x + b.box.x1) - Math.max(a.x + a.box.x0, b.x + b.box.x0) > 1 &&
      Math.min(a.y + a.box.y1, b.y + b.box.y1) - Math.max(a.y + a.box.y0, b.y + b.box.y0) > 1
    )

    const QUARTERS = [[0, 1], [1, 0], [0, -1], [-1, 0], [0.7, 0.7], [-0.7, 0.7]]

    const placeLabels = () => {
      const ns = sim.nodes()
      if (!ns.length) return
      const cx = d3.mean(ns, (n) => n.x) ?? 0
      const cy = d3.mean(ns, (n) => n.y) ?? 0

      ns.forEach((n) => {
        const near = n.nbr || []
        let dx = 0
        let dy = 0
        if (near.length) {
          dx = n.x - d3.mean(near, (m) => m.x)
          dy = n.y - d3.mean(near, (m) => m.y)
        }
        if (!dx && !dy) { dx = n.x - cx; dy = (n.y - cy) || 1 }
        const mag = Math.hypot(dx, dy) || 1
        n.ldx = dx / mag
        n.ldy = dy / mag
        n.pushOut = 0
        place(n, n.ldx, n.ldy)
      })

      for (let pass = 0; pass < 4; pass += 1) {
        let clean = true
        for (let i = 0; i < ns.length; i += 1) {
          for (let j = i + 1; j < ns.length; j += 1) {
            if (!hits(ns[i], ns[j])) continue
            clean = false
            const loser = (ns[i].deg <= ns[j].deg) ? ns[i] : ns[j]
            const other = loser === ns[i] ? ns[j] : ns[i]
            let best = null
            QUARTERS.forEach(([qx, qy]) => {
              place(loser, qx, qy)
              if (hits(loser, other)) return
              const away = qx * loser.ldx + qy * loser.ldy
              if (!best || away > best.away) best = { qx, qy, away }
            })
            if (best) {
              place(loser, best.qx, best.qy)
            } else {
              // nothing clears by rotating: push this label further from its node
              loser.pushOut = (loser.pushOut || 0) + 10
              place(loser, loser.ldx, loser.ldy)
            }
          }
        }
        if (clean) break
      }

      root.selectAll('.gx-node text')
        .attr('text-anchor', (d) => d.anchor)
        .each(function (d) {
          d3.select(this).selectAll('tspan')
            .attr('x', d.lx)
            .attr('y', (_, i) => d.ly0 + i * LINE_H)
        })
    }
    placeLabelsRef.current = placeLabels

    sim.on('tick', () => {
      root.selectAll('.gx-edge line')
        .attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y)
      root.selectAll('.gx-edge text')
        .attr('x', (d) => edgeLabelAt(d).x)
        .attr('y', (d) => edgeLabelAt(d).y)
      root.selectAll('.gx-node').attr('transform', (d) => `translate(${d.x},${d.y})`)
      placeLabels()
    })
    sim.on('end', () => { remember(); placeLabels() })

    /* Resize updates geometry and the centring forces only. It deliberately
       does NOT rebind data or restart from a high alpha, so opening the
       inspector or dragging the window does not re-scramble the layout. */
    const applySize = () => {
      const w = wrap.clientWidth || 900
      const h = wrap.clientHeight || 600
      if (w === sizeRef.current.w && h === sizeRef.current.h) return
      sizeRef.current = { w, h }
      svg.attr('viewBox', `0 0 ${w} ${h}`)
      retarget()
      if (sim.nodes().length) sim.alpha(Math.max(sim.alpha(), 0.06)).restart()
    }

    /* Connected concepts gravitate to the middle; concepts with no recorded
       relationship are parked in a quiet gutter on the right instead of
       drifting through the middle of everything. They are never removed. */
    const retarget = () => {
      const { w, h } = sizeRef.current
      const anyIsolated = sim.nodes().some((n) => n.isolated)
      const cx = anyIsolated ? w * 0.44 : w * 0.5
      sim.force('x', d3.forceX((d) => (d.isolated ? w - 74 : cx))
        .strength((d) => (d.isolated ? 0.42 : 0.055)))
      sim.force('y', d3.forceY((d) => (d.isolated ? d.gutterY ?? h / 2 : h / 2))
        .strength((d) => (d.isolated ? 0.3 : 0.055)))
    }
    retargetRef.current = retarget
    applySize()

    const observer = new ResizeObserver(applySize)
    observer.observe(wrap)

    return () => {
      observer.disconnect()
      sim.on('tick', null).on('end', null)
      sim.stop()
      svg.on('.zoom', null).on('click', null)
      simRef.current = null
      retargetRef.current = null
      placeLabelsRef.current = null
    }
  }, [])   // eslint-disable-line react-hooks/exhaustive-deps

  // --- data: rebind through joins, never rebuild ------------------------------

  useEffect(() => {
    const sim = simRef.current
    const svgEl = svgRef.current
    if (!sim || !svgEl) return

    const { w, h } = sizeRef.current
    const nodes = view.nodes.map((n) => {
      const kept = posRef.current[n.id]
      if (kept) return { ...n, x: kept.x, y: kept.y }
      const a = spread(n.id)
      return { ...n, x: w / 2 + Math.cos(a) * 90, y: h / 2 + Math.sin(a) * 90 }
    })
    // stagger the gutter so isolated concepts stack rather than pile up
    let slot = 0
    nodes.forEach((n) => {
      if (!n.isolated) return
      n.gutterY = 60 + (slot % 12) * ((h - 150) / 12)
      slot += 1
    })

    const edges = view.edges.map((e) => ({ ...e, id: `${e.source}\u0000${e.target}` }))
    const root = d3.select(svgEl).select('g.gx-root')

    const edgeSel = root.select('g.gx-edges')
      .selectAll('g.gx-edge').data(edges, (d) => d.id)
    edgeSel.exit().remove()
    const edgeEnter = edgeSel.enter().append('g').attr('class', 'gx-edge')
    // A transparent fat line makes links practical to hover; the visible wire
    // stays hairline.
    edgeEnter.append('line').attr('class', 'gx-hit')
      .attr('stroke', 'transparent').attr('stroke-width', 12)
      .style('cursor', 'pointer')
    edgeEnter.append('line').attr('class', 'gx-wire')
      .attr('stroke-linecap', 'round').style('pointer-events', 'none')
    edgeEnter.append('text').attr('class', 'gx-edge-label')
      .attr('text-anchor', 'middle').style('pointer-events', 'none')

    const allEdgeSel = edgeEnter.merge(edgeSel)
    allEdgeSel.select('line.gx-wire')
      .attr('stroke-dasharray', (d) => (d.strength === 'low' ? '4,4' : null))
      .attr('marker-end', (d) => (d.strength === 'high' ? 'url(#gx-arrow-hi)' : 'url(#gx-arrow)'))
    allEdgeSel.select('text').text((d) => d.label)
    allEdgeSel.select('line.gx-hit')
      .on('mouseenter', (_, d) => { hoverRef.current.edge = d; styleRef.current?.() })
      .on('mouseleave', () => { hoverRef.current.edge = null; styleRef.current?.() })

    const drag = d3.drag()
      .on('start', (event, d) => {
        if (!event.active) sim.alphaTarget(0.12).restart()
        d.fx = d.x; d.fy = d.y
      })
      .on('drag', (event, d) => { d.fx = event.x; d.fy = event.y })
      .on('end', (event, d) => {
        if (!event.active) sim.alphaTarget(0)
        // stays where it was dropped; everything else is free to settle
        d.fx = d.x; d.fy = d.y
        posRef.current[d.id] = { x: d.x, y: d.y }
      })

    const nodeSel = root.select('g.gx-nodes')
      .selectAll('g.gx-node').data(nodes, (d) => d.id)
    nodeSel.exit().remove()
    const nodeEnter = nodeSel.enter().append('g').attr('class', 'gx-node')
      .attr('transform', (d) => `translate(${d.x},${d.y})`)
      .attr('tabindex', 0)
      .attr('role', 'button')
      .style('cursor', 'pointer')
    nodeEnter.append('circle')
    nodeEnter.append('text')
      .attr('text-anchor', 'middle')
      .attr('paint-order', 'stroke')
      .attr('stroke', 'rgba(20,17,9,0.92)')
      .attr('stroke-width', '3.2px')
      .style('pointer-events', 'none')

    const allNodeSel = nodeEnter.merge(nodeSel)
    allNodeSel
      .attr('aria-label', (d) => `${d.name}, ${d.deg} connection${d.deg === 1 ? '' : 's'}`)
      .call(drag)
      .on('click', (event, d) => { event.stopPropagation(); onSelectNodeRef.current?.(d) })
      .on('keydown', (event, d) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelectNodeRef.current?.(d)
        }
      })
      .on('mouseenter', (event, d) => {
        hoverRef.current.node = d.id
        styleRef.current?.()
        const box = wrapRef.current.getBoundingClientRect()
        setTip({
          x: event.clientX - box.left, y: event.clientY - box.top,
          name: d.name, deg: d.deg, week: d.week,
        })
      })
      .on('mouseleave', () => {
        hoverRef.current.node = null
        styleRef.current?.()
        setTip(null)
      })
      .on('focus', (_, d) => { hoverRef.current.node = d.id; styleRef.current?.() })
      .on('blur', () => { hoverRef.current.node = null; styleRef.current?.() })

    allNodeSel.select('circle')
      .attr('fill', (d) => (
        d.week === week ? '#C1652F'
          : d.tier === 'high' ? 'rgba(243,238,221,0.92)'
            : 'rgba(150,137,122,0.82)'))

    allNodeSel.select('text')
      .attr('font-size', (d) => (d.tier === 'high' ? 11.5 : 10.5))
      .each(function (d) {
        const text = d3.select(this)
        text.selectAll('tspan').remove()
        d.lines.forEach((line) => text.append('tspan').text(line))
      })

    sim.nodes(nodes)
    sim.force('link').links(edges)

    // forceLink has swapped the id strings for node objects, so neighbour
    // lists can be held by reference for the label placement pass.
    nodes.forEach((n) => { n.nbr = [] })
    edges.forEach((e) => { e.source.nbr.push(e.target); e.target.nbr.push(e.source) })

    retargetRef.current?.()
    sim.alpha(Object.keys(posRef.current).length ? 0.4 : 0.9).restart()
    placeLabelsRef.current?.()
    styleRef.current?.()
  }, [view, week])

  // --- toolbar ----------------------------------------------------------------

  const zoomBy = (k) => {
    const svg = d3.select(svgRef.current)
    if (zoomRef.current) svg.transition().duration(220).call(zoomRef.current.scaleBy, k)
  }

  /** True fit: frames the layout's bounding box, not just zoomIdentity. */
  const fit = useCallback((duration = 380) => {
    const sim = simRef.current
    const svg = d3.select(svgRef.current)
    if (!sim || !zoomRef.current || !sim.nodes().length) return
    const { w, h } = sizeRef.current
    // Legend and search sit at the top, the toolbar on the right and the week
    // timeline along the bottom, so the usable frame is not the whole canvas.
    const pad = { top: 64, right: w > 760 ? 150 : 60, bottom: 96, left: 48 }
    const fw = Math.max(120, w - pad.left - pad.right)
    const fh = Math.max(120, h - pad.top - pad.bottom)
    const xs = sim.nodes().map((n) => n.x)
    const ys = sim.nodes().map((n) => n.y)
    const x0 = Math.min(...xs), x1 = Math.max(...xs)
    const y0 = Math.min(...ys), y1 = Math.max(...ys)
    const k = Math.min(fw / Math.max(1, x1 - x0), fh / Math.max(1, y1 - y0), 1.6)
    const t = d3.zoomIdentity
      .translate(pad.left + fw / 2 - k * (x0 + x1) / 2,
                 pad.top + fh / 2 - k * (y0 + y1) / 2)
      .scale(k)
    svg.transition().duration(duration).call(zoomRef.current.transform, t)
  }, [])

  const relayout = () => {
    const sim = simRef.current
    if (!sim) return
    posRef.current = {}
    const { w, h } = sizeRef.current
    sim.nodes().forEach((n) => {
      n.fx = null; n.fy = null
      const a = spread(n.id)
      n.x = w / 2 + Math.cos(a) * 90
      n.y = h / 2 + Math.sin(a) * 90
    })
    sim.alpha(0.95).restart()
  }

  const reset = () => {
    setSearch('')
    setShowRelations(false)
    setHideIsolated(false)
    onSelectNode?.(null)
    fit()
  }

  /** Centres and zooms to a concept, as the search spec asks. */
  const locate = useCallback((name) => {
    const sim = simRef.current
    const svg = d3.select(svgRef.current)
    if (!sim || !zoomRef.current) return
    const term = name.trim().toLowerCase()
    const hit = sim.nodes().find((n) => n.name.toLowerCase() === term)
      || sim.nodes().find((n) => n.name.toLowerCase().includes(term))
    if (!hit) return
    const { w, h } = sizeRef.current
    const k = Math.max(1.5, zoomKRef.current)
    const t = d3.zoomIdentity.translate(w / 2 - k * hit.x, h / 2 - k * hit.y).scale(k)
    svg.transition().duration(450).call(zoomRef.current.transform, t)
    onSelectNode?.(hit)
  }, [onSelectNode])

  // Fit once the first layout has settled, so the graph arrives framed.
  useEffect(() => {
    if (!view.nodes.length) return undefined
    const id = setTimeout(() => fit(600), 1500)
    return () => clearTimeout(id)
  }, [view.nodes.length, fit])

  const clearHover = () => {
    hoverRef.current = { node: null, edge: null }
    styleRef.current?.()
    setTip(null)
  }

  return (
    <div className="graph-panel-inner" ref={wrapRef} onMouseLeave={clearHover}>
      <div className="gx-legend" aria-hidden="true">
        <span><i style={{ background: 'rgba(243,238,221,0.92)' }} />Core concept</span>
        <span><i style={{ background: '#C1652F' }} />Added this week</span>
        <span><i style={{ background: 'rgba(150,137,122,0.82)' }} />Supporting idea</span>
      </div>

      <div className="gx-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" aria-hidden="true">
          <circle cx="11" cy="11" r="7" /><path d="m16.5 16.5 4 4" />
        </svg>
        <input
          value={search}
          list="gx-concepts"
          placeholder="Search concepts"
          aria-label="Search concepts"
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter' && search.trim()) locate(search) }}
        />
        <datalist id="gx-concepts">
          {suggestions.map((s) => <option key={s} value={s} />)}
        </datalist>
        {search && (
          <button type="button" className="gx-search-clear" aria-label="Clear search"
                  onClick={() => setSearch('')}>×</button>
        )}
      </div>

      <div className="gx-toolbar" role="toolbar" aria-label="Graph controls">
        <div className="gx-group">
          <button type="button" className="gx-btn" onClick={() => zoomBy(1.35)}
                  title="Zoom in" aria-label="Zoom in">+</button>
          <button type="button" className="gx-btn" onClick={() => zoomBy(0.74)}
                  title="Zoom out" aria-label="Zoom out">−</button>
          <button type="button" className="gx-btn gx-btn-wide" onClick={() => fit()}
                  title="Fit the whole graph on screen" aria-label="Fit to screen">Fit</button>
        </div>
        <div className="gx-group">
          <button type="button" className="gx-btn gx-btn-wide" onClick={relayout}
                  title="Run the layout again from scratch" aria-label="Re-layout">Re-layout</button>
          <button type="button" className="gx-btn gx-btn-wide" onClick={reset}
                  title="Clear selection, search and filters, then fit" aria-label="Reset the view">Reset</button>
        </div>
        <div className="gx-group">
          <button
            type="button"
            className={`gx-btn gx-btn-wide${showRelations ? ' on' : ''}`}
            aria-pressed={showRelations}
            title="Show every relationship label at once"
            onClick={() => setShowRelations((v) => !v)}
          >
            Relationships
          </button>
          {view.isolatedCount > 0 && (
            <button
              type="button"
              className={`gx-btn gx-btn-wide${hideIsolated ? ' on' : ''}`}
              aria-pressed={hideIsolated}
              title={`${view.isolatedCount} concept${view.isolatedCount === 1 ? ' has' : 's have'} no recorded relationship`}
              onClick={() => setHideIsolated((v) => !v)}
            >
              {hideIsolated ? 'Show' : 'Hide'} unlinked
            </button>
          )}
        </div>
      </div>


      <svg ref={svgRef} id="wsGraphSvg" role="application"
           aria-label={`Concept graph: ${view.nodes.length} concepts, ${view.edges.length} relationships`} />

      <div className="gx-hint">Drag to pin · click to inspect · hover a link for its relationship</div>

      {tip && (
        <div className="node-tip show" style={{ left: tip.x + 14, top: tip.y + 14 }}>
          <b>{tip.name}</b><br />
          {tip.deg} connection{tip.deg === 1 ? '' : 's'} · week {tip.week}
        </div>
      )}

      <div className="ws-timeline">
        <div className="ws-timeline-head">
          <span>Week <span className="num">{week}</span> of {maxWeek}</span>
          <span>{view.nodes.length} concepts · {view.edges.length} relationships</span>
        </div>
        <input
          type="range"
          min="1"
          max={maxWeek}
          value={week}
          step="1"
          aria-label={`Show concepts up to week ${week} of ${maxWeek}`}
          onChange={(e) => onWeekChange && onWeekChange(Number(e.target.value))}
        />
      </div>
    </div>
  )
}
