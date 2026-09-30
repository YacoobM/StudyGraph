import React, { useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'

/**
 * Landing-page demonstration of how a knowledge graph accumulates week to week.
 *
 * This is presentation only. It never calls the API and never touches the
 * production graph — the snapshots below are the whole of its data. Adding a
 * Week 3 is a matter of appending another entry to GRAPH_SNAPSHOTS; the
 * component derives its controls, stats and "what's new" from the array.
 *
 * The accumulation is the point, so the simulation is never torn down between
 * weeks. Node objects are reused by id, which carries their x/y across the
 * change, and a per-node positional anchor holds settled concepts near where
 * the viewer last saw them while new ones find their own space.
 */

/* Week N keeps every concept from Week N-1 and adds to it. */
export const GRAPH_SNAPSHOTS = [
  {
    week: 1,
    nodes: [
      { id: 'parallel-computing', name: 'Parallel Computing' },
      { id: 'parallel-programming-models', name: 'Parallel Programming Models' },
      { id: 'shared-memory', name: 'Shared Memory' },
      { id: 'distributed-computing', name: 'Distributed Computing' },
      { id: 'concurrency', name: 'Concurrency' },
      { id: 'memory-architecture', name: 'Memory Architecture' },
      { id: 'performance-scalability', name: 'Performance Scalability' },
    ],
    links: [
      { source: 'parallel-computing', target: 'parallel-programming-models', label: 'is described by' },
      { source: 'parallel-programming-models', target: 'shared-memory', label: 'includes' },
      { source: 'parallel-programming-models', target: 'distributed-computing', label: 'includes' },
      { source: 'shared-memory', target: 'memory-architecture', label: 'depends on' },
      { source: 'parallel-computing', target: 'concurrency', label: 'requires' },
      { source: 'concurrency', target: 'shared-memory', label: 'contends for' },
      { source: 'parallel-computing', target: 'performance-scalability', label: 'is measured by' },
      { source: 'distributed-computing', target: 'performance-scalability', label: 'is limited by' },
    ],
  },
  {
    week: 2,
    nodes: [
      { id: 'parallel-computing', name: 'Parallel Computing' },
      { id: 'parallel-programming-models', name: 'Parallel Programming Models' },
      { id: 'shared-memory', name: 'Shared Memory' },
      { id: 'distributed-computing', name: 'Distributed Computing' },
      { id: 'concurrency', name: 'Concurrency' },
      { id: 'memory-architecture', name: 'Memory Architecture' },
      { id: 'performance-scalability', name: 'Performance Scalability' },
      { id: 'inter-process-communication', name: 'Inter Process Communication' },
      { id: 'bsd-socket-api', name: 'BSD Socket API' },
      { id: 'tcp-sockets', name: 'TCP Sockets' },
      { id: 'udp-sockets', name: 'UDP Sockets' },
      { id: 'client-server-model', name: 'Client Server Model' },
      { id: 'rpc', name: 'RPC' },
      { id: 'http-protocol', name: 'HTTP Protocol' },
    ],
    links: [
      { source: 'parallel-computing', target: 'parallel-programming-models', label: 'is described by' },
      { source: 'parallel-programming-models', target: 'shared-memory', label: 'includes' },
      { source: 'parallel-programming-models', target: 'distributed-computing', label: 'includes' },
      { source: 'shared-memory', target: 'memory-architecture', label: 'depends on' },
      { source: 'parallel-computing', target: 'concurrency', label: 'requires' },
      { source: 'concurrency', target: 'shared-memory', label: 'contends for' },
      { source: 'parallel-computing', target: 'performance-scalability', label: 'is measured by' },
      { source: 'distributed-computing', target: 'performance-scalability', label: 'is limited by' },
      { source: 'distributed-computing', target: 'inter-process-communication', label: 'relies on' },
      { source: 'inter-process-communication', target: 'bsd-socket-api', label: 'is implemented by' },
      { source: 'bsd-socket-api', target: 'tcp-sockets', label: 'provides' },
      { source: 'bsd-socket-api', target: 'udp-sockets', label: 'provides' },
      { source: 'tcp-sockets', target: 'client-server-model', label: 'supports' },
      { source: 'udp-sockets', target: 'client-server-model', label: 'supports' },
      { source: 'client-server-model', target: 'rpc', label: 'enables' },
      { source: 'rpc', target: 'inter-process-communication', label: 'is a form of' },
      { source: 'client-server-model', target: 'http-protocol', label: 'is used by' },
      { source: 'http-protocol', target: 'tcp-sockets', label: 'runs over' },
      { source: 'tcp-sockets', target: 'performance-scalability', label: 'affects' },
    ],
  },
]

const EMPHASIS_MS = 1600      // how long a new node stays highlighted
const FADE_MS = 520

/* How long each week is held before the demo moves on. Week 2 is the payoff,
   so it gets longer before the loop restarts. */
const HOLD_MS = [3000, 4800]
const CHAR_W = 5.3            // approx. advance width of Inter at 10px

/**
 * Wraps a concept name onto at most two balanced lines. Long single-line
 * labels are what make this panel unreadable at week 2 — "Inter Process
 * Communication" is 140px of text under a 14px circle — so the label's
 * footprint is halved before it is ever handed to the collision force.
 */
function wrapLabel(name) {
  const words = name.split(' ')
  if (words.length < 2 || name.length <= 14) return [name]
  let best = null
  for (let i = 1; i < words.length; i += 1) {
    const a = words.slice(0, i).join(' ')
    const b = words.slice(i).join(' ')
    const score = Math.abs(a.length - b.length)
    if (!best || score < best.score) best = { lines: [a, b], score }
  }
  return best.lines
}

const labelHalfWidth = (lines) =>
  (Math.max(...lines.map((l) => l.length)) * CHAR_W) / 2

/**
 * A stable angle derived from a concept's id. Seeding new nodes from this
 * rather than Math.random keeps the demo laying out the same way on every
 * visit, which is what makes it safe to assert against in verify_landing.mjs.
 */
function spread(id) {
  let h = 2166136261
  for (let i = 0; i < id.length; i += 1) {
    h ^= id.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return ((h >>> 0) / 4294967296) * Math.PI * 2
}

const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

export default function LandingGraphDemo() {
  const wrapRef = useRef(null)
  const svgRef = useRef(null)
  const simRef = useRef(null)
  const nodeStoreRef = useRef(new Map())   // id -> live d3 node object, reused across weeks
  const homeRef = useRef(new Map())        // id -> settled {x, y}, the positional anchor
  const sizeRef = useRef({ w: 0, h: 0 })
  const paintRef = useRef(null)
  const [index, setIndex] = useState(0)
  // The demo plays itself. Touching a week chip hands control to the viewer.
  const [auto, setAuto] = useState(() => !prefersReducedMotion())

  const snapshot = GRAPH_SNAPSHOTS[index]
  const previous = index > 0 ? GRAPH_SNAPSHOTS[index - 1] : null

  const added = useMemo(() => {
    if (!previous) return []
    const before = new Set(previous.nodes.map((n) => n.id))
    return snapshot.nodes.filter((n) => !before.has(n.id)).map((n) => n.name)
  }, [snapshot, previous])

  /* Runs the week timeline on its own, and only while the panel is actually
     in view — a graph simulating behind the fold is wasted work, and a viewer
     who scrolls down should catch the transition rather than its aftermath. */
  useEffect(() => {
    if (!auto) return undefined
    const wrap = wrapRef.current
    if (!wrap) return undefined

    let timer = null
    let seen = false

    const step = () => {
      timer = setTimeout(() => {
        setIndex((i) => (i + 1) % GRAPH_SNAPSHOTS.length)
        step()
      }, HOLD_MS[index] ?? HOLD_MS[0])
    }

    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && !seen) {
        seen = true
        step()
      } else if (!entry.isIntersecting && timer) {
        clearTimeout(timer)
        timer = null
        seen = false
      }
    }, { threshold: 0.35 })
    observer.observe(wrap)

    return () => {
      observer.disconnect()
      if (timer) clearTimeout(timer)
    }
  }, [auto, index])

  /* One simulation for the life of the component. Weeks change its data; they
     never replace it, which is what lets positions survive the transition. */
  useEffect(() => {
    const svgEl = svgRef.current
    const wrap = wrapRef.current
    if (!svgEl || !wrap) return

    const svg = d3.select(svgEl)
    svg.selectAll('*').remove()

    const defs = svg.append('defs')
    defs.append('marker')
      .attr('id', 'lgd-arrow').attr('viewBox', '0 -5 10 10')
      .attr('refX', 24).attr('refY', 0).attr('orient', 'auto')
      .attr('markerWidth', 5).attr('markerHeight', 5)
      .append('path').attr('d', 'M0,-5L10,0L0,5').attr('fill', 'rgba(243,238,221,0.32)')

    const root = svg.append('g').attr('class', 'lgd-root')
    root.append('g').attr('class', 'lgd-links')
    root.append('g').attr('class', 'lgd-nodes')

    // Collision is sized from the label, not the circle: the text is what
    // actually overlaps. Capped so one long name cannot push the whole
    // layout off the panel.
    const sim = d3.forceSimulation([])
      .force('link', d3.forceLink([]).id((d) => d.id).distance(112).strength(0.4))
      .force('charge', d3.forceManyBody().strength(-620).distanceMax(500))
      .force('collide', d3.forceCollide()
        .radius((d) => Math.min(56, Math.max((d.core ? 11 : 7) + 14, (d.labelHalf || 20) * 0.8)))
        .strength(1).iterations(3))
      .velocityDecay(0.58)
      .alphaDecay(0.035)
      .alphaMin(0.006)
      .stop()
    simRef.current = sim

    /* The panel is a fixed frame and the graph grows into it, so the view
       tracks the layout's bounding box rather than the layout being clamped.
       Easing toward the target keeps that from looking like a jump when a
       week's worth of new concepts arrives. */
    const view = { k: 1, x: 0, y: 0, primed: false }
    const frame = (snap) => {
      const ns = sim.nodes()
      if (!ns.length) return
      const { w, h } = sizeRef.current
      const pad = 30
      let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
      ns.forEach((n) => {
        const b = n.labelBox
        if (!b) return
        minX = Math.min(minX, n.x + b.x0); maxX = Math.max(maxX, n.x + b.x1)
        minY = Math.min(minY, n.y + b.y0); maxY = Math.max(maxY, n.y + b.y1)
      })
      if (minX === Infinity) return
      const k = Math.min((w - pad * 2) / Math.max(1, maxX - minX),
                         (h - pad * 2) / Math.max(1, maxY - minY), 1.15)
      const tx = w / 2 - k * (minX + maxX) / 2
      const ty = h / 2 - k * (minY + maxY) / 2
      const ease = view.primed && !snap ? 0.14 : 1
      view.primed = true
      view.k += (k - view.k) * ease
      view.x += (tx - view.x) * ease
      view.y += (ty - view.y) * ease
      root.attr('transform', `translate(${view.x},${view.y}) scale(${view.k})`)
    }

    const LINE_H = 11

    /* A label hung below every node collides with whatever sits underneath it,
       which no amount of circular collision can fix. Instead each label is
       pushed to the side of its node that faces away from its own neighbours —
       the direction that is, by construction, the emptier one. */
    const apply = (n, dx, dy) => {
      const r = n.core ? 11 : 7
      const off = r + 8
      const lines = n.lines.length
      const wHalf = n.labelHalf || 20
      const lx = dx * off
      const ly = dy * off

      n.anchor = dx > 0.45 ? 'start' : dx < -0.45 ? 'end' : 'middle'
      n.lx = lx
      if (dy > 0.45) n.ly0 = ly + LINE_H
      else if (dy < -0.45) n.ly0 = ly - 4 - (lines - 1) * LINE_H
      else n.ly0 = ly - ((lines - 1) * LINE_H) / 2 + 3.5

      const tx0 = n.anchor === 'start' ? lx : n.anchor === 'end' ? lx - wHalf * 2 : lx - wHalf
      n.textBox = {
        x0: tx0, x1: tx0 + wHalf * 2,
        y0: n.ly0 - 9, y1: n.ly0 + (lines - 1) * LINE_H + 3,
      }
      n.labelBox = {
        x0: Math.min(-r, n.textBox.x0), x1: Math.max(r, n.textBox.x1),
        y0: Math.min(-r, n.textBox.y0), y1: Math.max(r, n.textBox.y1),
      }
    }

    const overlaps = (a, b) => {
      const ax0 = a.x + a.textBox.x0, ax1 = a.x + a.textBox.x1
      const ay0 = a.y + a.textBox.y0, ay1 = a.y + a.textBox.y1
      const bx0 = b.x + b.textBox.x0, bx1 = b.x + b.textBox.x1
      const by0 = b.y + b.textBox.y0, by1 = b.y + b.textBox.y1
      return Math.min(ax1, bx1) - Math.max(ax0, bx0) > 1 &&
             Math.min(ay1, by1) - Math.max(ay0, by0) > 1
    }

    const placeLabels = () => {
      const ns = sim.nodes()
      const cx = d3.mean(ns, (n) => n.x) ?? 0
      const cy = d3.mean(ns, (n) => n.y) ?? 0

      ns.forEach((n) => {
        const near = n.neighbourRefs || []
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
        apply(n, n.ldx, n.ldy)
      })

      /* The heuristic gets most of it; a few pairs still land on each other.
         Rotating the less-connected label a quarter turn at a time clears them
         without disturbing the layout itself. */
      const QUARTERS = [[0, 1], [1, 0], [0, -1], [-1, 0], [0.7, 0.7], [-0.7, 0.7]]
      for (let pass = 0; pass < 3; pass += 1) {
        let clean = true
        for (let i = 0; i < ns.length; i += 1) {
          for (let j = i + 1; j < ns.length; j += 1) {
            if (!overlaps(ns[i], ns[j])) continue
            clean = false
            const loser = ns[i].degree <= ns[j].degree ? ns[i] : ns[j]
            const other = loser === ns[i] ? ns[j] : ns[i]
            let best = null
            QUARTERS.forEach(([qx, qy]) => {
              apply(loser, qx, qy)
              if (overlaps(loser, other)) return
              const away = qx * loser.ldx + qy * loser.ldy   // prefer staying near the original side
              if (!best || away > best.away) best = { qx, qy, away }
            })
            apply(loser, best ? best.qx : loser.ldx, best ? best.qy : loser.ldy)
          }
        }
        if (clean) break
      }
    }

    const paint = (snap) => {
      root.selectAll('.lgd-link line')
        .attr('x1', (d) => d.source.x).attr('y1', (d) => d.source.y)
        .attr('x2', (d) => d.target.x).attr('y2', (d) => d.target.y)
      root.selectAll('.lgd-node').attr('transform', (d) => `translate(${d.x},${d.y})`)
      placeLabels()
      root.selectAll('.lgd-node text')
        .attr('text-anchor', (d) => d.anchor)
        .each(function (d) {
          d3.select(this).selectAll('tspan')
            .attr('x', d.lx)
            .attr('y', (_, i) => d.ly0 + i * LINE_H)
        })
      frame(snap)
    }
    paintRef.current = paint
    sim.on('tick', paint)

    // Remember where everything settled, so the next week can anchor to it.
    sim.on('end', () => {
      sim.nodes().forEach((n) => homeRef.current.set(n.id, { x: n.x, y: n.y }))
    })

    const applySize = () => {
      const w = wrap.clientWidth || 800
      const h = wrap.clientHeight || 420
      if (w === sizeRef.current.w && h === sizeRef.current.h) return
      sizeRef.current = { w, h }
      svg.attr('viewBox', `0 0 ${w} ${h}`)
      sim.force('center', d3.forceCenter(w / 2, h / 2))
      // nudge rather than restart, so a resize never re-scrambles the layout
      if (sim.nodes().length) sim.alpha(Math.max(sim.alpha(), 0.08)).restart()
    }
    applySize()

    const observer = new ResizeObserver(applySize)
    observer.observe(wrap)

    return () => {
      observer.disconnect()
      sim.on('tick', null).on('end', null)
      sim.stop()
      simRef.current = null
      paintRef.current = null
    }
  }, [])

  /* Week change: re-bind data on the running simulation. */
  useEffect(() => {
    const sim = simRef.current
    const svgEl = svgRef.current
    if (!sim || !svgEl) return

    const reduced = prefersReducedMotion()
    const { w, h } = sizeRef.current
    const cx = (w || 800) / 2
    const cy = (h || 420) / 2

    const store = nodeStoreRef.current
    const home = homeRef.current
    const firstPaint = store.size === 0

    // Reuse the existing node object where we have one: that is what carries
    // x, y and velocity across the week change.
    const freshIds = []
    const nodes = snapshot.nodes.map((spec) => {
      const existing = store.get(spec.id)
      if (existing) return Object.assign(existing, { name: spec.name })
      freshIds.push(spec.id)
      const created = { ...spec }
      store.set(spec.id, created)
      return created
    })
    const live = new Set(nodes.map((n) => n.id))
    ;[...store.keys()].forEach((id) => { if (!live.has(id)) store.delete(id) })

    // Seed a new node beside a concept it connects to, rather than at the
    // origin — otherwise it flies across the panel to get there.
    const seed = new Set(freshIds)
    // On a replay these concepts have been here before: drop them straight
    // back where they settled rather than letting them drift in again.
    freshIds.forEach((id) => {
      const was = home.get(id)
      if (!was) return
      const node = store.get(id)
      node.x = was.x
      node.y = was.y
    })
    if (!firstPaint) {
      snapshot.links.forEach(({ source, target }) => {
        const pairs = [[source, target], [target, source]]
        pairs.forEach(([a, b]) => {
          if (!seed.has(a)) return
          const anchor = store.get(b)
          if (!anchor || anchor.x == null || seed.has(b)) return
          const node = store.get(a)
          if (node.x != null) return
          const angle = spread(node.id)
          node.x = anchor.x + Math.cos(angle) * 52
          node.y = anchor.y + Math.sin(angle) * 52
        })
      })
    }
    nodes.forEach((n) => {
      if (n.x == null) {
        const angle = spread(n.id)
        n.x = cx + Math.cos(angle) * 60
        n.y = cy + Math.sin(angle) * 60
      }
    })

    const links = snapshot.links.map((l) => ({ ...l, id: `${l.source}->${l.target}` }))

    const degree = {}
    snapshot.links.forEach((l) => {
      degree[l.source] = (degree[l.source] || 0) + 1
      degree[l.target] = (degree[l.target] || 0) + 1
    })
    nodes.forEach((n) => {
      n.degree = degree[n.id] || 0
      n.core = n.degree >= 3
      n.isNew = seed.has(n.id) && !firstPaint
      n.lines = wrapLabel(n.name)
      n.labelHalf = labelHalfWidth(n.lines)
    })

    const root = d3.select(svgEl).select('g.lgd-root')

    const linkSel = root.select('g.lgd-links')
      .selectAll('g.lgd-link')
      .data(links, (d) => d.id)
    linkSel.exit().remove()
    const linkEnter = linkSel.enter().append('g').attr('class', 'lgd-link')
      .style('opacity', firstPaint || reduced ? 1 : 0)
    linkEnter.append('line')
      .attr('stroke', 'rgba(243,238,221,0.24)')
      .attr('stroke-width', 1)
      .attr('marker-end', 'url(#lgd-arrow)')
    if (!firstPaint && !reduced) {
      linkEnter.transition().delay(180).duration(FADE_MS).style('opacity', 1)
    }

    const nodeSel = root.select('g.lgd-nodes')
      .selectAll('g.lgd-node')
      .data(nodes, (d) => d.id)
    nodeSel.exit().remove()
    const nodeEnter = nodeSel.enter().append('g').attr('class', 'lgd-node')
      .attr('transform', (d) => `translate(${d.x},${d.y})`)
      .style('opacity', firstPaint || reduced ? 1 : 0)
    nodeEnter.append('circle')
    nodeEnter.append('text')
      .attr('class', 'lgd-label')
      .attr('text-anchor', 'middle')
      .attr('paint-order', 'stroke')
      .attr('stroke', 'rgba(23,19,9,0.9)')
      .attr('stroke-width', '3px')
    if (!firstPaint && !reduced) {
      nodeEnter.transition().duration(FADE_MS).style('opacity', 1)
    }

    const allNodes = nodeEnter.merge(nodeSel)
    allNodes.select('circle')
      .attr('r', (d) => (d.core ? 11 : 7))
      .attr('stroke', 'rgba(23,19,9,0.45)')
      .attr('stroke-width', 1.4)
      .attr('fill', (d) => (d.isNew ? '#C1652F' : d.core ? 'rgba(243,238,221,0.94)' : 'rgba(150,137,122,0.9)'))
    allNodes.select('text')
      .attr('font-size', (d) => (d.core ? 10.5 : 9.8))
      .attr('font-weight', (d) => (d.core ? 600 : 500))
      .attr('fill', (d) => (d.core ? 'rgba(243,238,221,0.95)' : 'rgba(178,167,152,0.95)'))
      .each(function (d) {
        // Position is set every tick by placeLabels; this only owns the text.
        const text = d3.select(this)
        text.selectAll('tspan').remove()
        d.lines.forEach((line) => text.append('tspan').text(line))
      })

    // The copper emphasis on arrivals is a cue, not a permanent colour.
    if (!firstPaint && freshIds.length && !reduced) {
      allNodes.filter((d) => d.isNew)
        .select('circle')
        .transition().delay(EMPHASIS_MS).duration(420)
        .attr('fill', (d) => (d.core ? 'rgba(243,238,221,0.94)' : 'rgba(150,137,122,0.9)'))
    }

    sim.nodes(nodes)
    sim.force('link').links(links)

    // forceLink has now swapped the id strings for node objects, so neighbour
    // lists can be built by reference — placeLabels reads them every tick.
    nodes.forEach((n) => { n.neighbourRefs = [] })
    links.forEach((l) => {
      l.source.neighbourRefs.push(l.target)
      l.target.neighbourRefs.push(l.source)
    })
    // Settled concepts are held near where the viewer last saw them; new ones
    // are free to find space. This is what stops the graph re-scrambling.
    sim.force('anchorX', d3.forceX((d) => home.get(d.id)?.x ?? cx)
      .strength((d) => (home.has(d.id) ? 0.28 : 0.03)))
    sim.force('anchorY', d3.forceY((d) => home.get(d.id)?.y ?? cy)
      .strength((d) => (home.has(d.id) ? 0.28 : 0.03)))

    sim.alpha(firstPaint ? 0.85 : 0.32).restart()
    if (reduced) {
      // Settle synchronously so nothing visibly moves.
      for (let i = 0; i < 220; i += 1) sim.tick()
      sim.stop()
      paintRef.current?.(true)   // snap the view rather than easing into it
      nodes.forEach((n) => home.set(n.id, { x: n.x, y: n.y }))
    }
  }, [snapshot])

  return (
    <div className="lp-demo">
      <div className="lp-demo-bar">
        <span className="lp-demo-title">Knowledge graph · Week {snapshot.week}</span>
        <span className="lp-demo-stats">
          <span><b>{snapshot.nodes.length}</b> concepts</span>
          <span><b>{snapshot.links.length}</b> relationships</span>
          {added.length > 0 && <span className="lp-demo-new">+{added.length} new</span>}
        </span>
      </div>

      <div className="lp-demo-canvas" ref={wrapRef}>
        <svg ref={svgRef} role="img"
             aria-label={`Example knowledge graph at week ${snapshot.week}: ${snapshot.nodes.length} concepts and ${snapshot.links.length} relationships`} />
      </div>

      <div className="lp-demo-timeline" role="group" aria-label="Graph timeline">
        {GRAPH_SNAPSHOTS.map((s, i) => (
          <React.Fragment key={s.week}>
            {i > 0 && <span className="lp-demo-track" aria-hidden="true" />}
            <button
              type="button"
              className={`lp-demo-step${i === index ? ' on' : ''}`}
              aria-pressed={i === index}
              title={`Show week ${s.week}`}
              onClick={() => { setAuto(false); setIndex(i) }}
            >
              <span className="lp-demo-dot" aria-hidden="true" />
              Week {s.week}
            </button>
          </React.Fragment>
        ))}
        {!auto && (
          <button type="button" className="lp-demo-replay"
                  title="Play the weeks again"
                  onClick={() => { setAuto(true); setIndex(0) }}>
            Replay
          </button>
        )}
      </div>

      <p className="lp-demo-caption" aria-live="polite">
        {added.length === 0
          ? 'One week of material, mapped into concepts and the relationships between them.'
          : `A second upload adds ${added.length} concepts — ${added.slice(0, 3).join(', ')} and more — without disturbing what was already there.`}
      </p>
    </div>
  )
}
