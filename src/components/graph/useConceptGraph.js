import { useCallback, useEffect, useRef, useState } from "react"
import * as d3 from "d3"
import { GRAPH_COLORS, LABEL_ZOOM, nodeMatches, progressiveNodeIds } from "./conceptGraphModel.js"

const LINE_HEIGHT = 12

export default function useConceptGraph({ view, selectedId, onSelectNode, search, showRelations, week }) {
  const svgRef = useRef(null)
  const wrapRef = useRef(null)
  const tooltipRef = useRef(null)
  const pointerRef = useRef(null)
  const simulationRef = useRef(null)
  const zoomRef = useRef(null)
  const positionsRef = useRef(new Map())
  const nodeCacheRef = useRef(new Map())
  const transitionPinsRef = useRef(new Map())
  const sizeRef = useRef({ width: 0, height: 0 })
  const frameRef = useRef(null)
  const runtimeRef = useRef(null)
  const [tooltip, setTooltip] = useState(null)
  const [drawnCount, setDrawnCount] = useState(null)

  const updateTooltipPosition = useCallback((event) => {
    pointerRef.current = { x: event.clientX, y: event.clientY }
    const element = tooltipRef.current
    const wrapper = wrapRef.current
    if (!element || !wrapper) return
    const bounds = wrapper.getBoundingClientRect()
    element.style.left = `${event.clientX - bounds.left + 14}px`
    element.style.top = `${event.clientY - bounds.top + 14}px`
  }, [])

  const applyStyles = useCallback(() => {
    const svg = d3.select(svgRef.current)
    const root = svg.select("g.gx-root")
    if (root.empty()) return

    const runtime = runtimeRef.current
    const { view: currentView, selectedId: currentSelection, search: currentSearch, showRelations: relations } = runtime
    const scale = runtime.zoomScale
    const term = currentSearch.trim().toLowerCase()
    const hoverNode = runtime.hoverNode
    const hoverEdge = runtime.hoverEdge
    const focus = currentSelection ? String(currentSelection) : hoverNode
    const near = focus ? new Set(currentView.neighbours.get(focus) || []) : null
    if (near) near.add(focus)
    const matches = (node) => nodeMatches(node, term)
    const visibleIds = progressiveNodeIds(currentView.nodes, scale)
    near?.forEach((id) => visibleIds.add(id))
    currentView.nodes.forEach((node) => {
      if (matches(node)) visibleIds.add(node.id)
    })

    const nodes = root.selectAll(".gx-node:not(.gx-exiting)")
    const edges = root.selectAll(".gx-edge:not(.gx-exiting)")
    nodes
      .style("display", (node) => (visibleIds.has(node.id) ? null : "none"))
      .style("opacity", (node) => {
      if (!visibleIds.has(node.id)) return 0
      if (near) return near.has(node.id) ? 1 : currentSelection ? 0.1 : 0.22
      if (term) return matches(node) ? 1 : 0.14
      return 1
    })
    nodes
      .select("circle")
      .attr("r", (node) => node.r * (node.id === focus ? 1.22 : 1))
      .attr("stroke", (node) =>
        node.id === String(currentSelection)
          ? GRAPH_COLORS.selected
          : matches(node)
            ? GRAPH_COLORS.matched
            : "rgba(0,0,0,0.42)",
      )
      .attr("stroke-width", (node) =>
        node.id === String(currentSelection) || matches(node) ? 2.4 : 1.4,
      )

    nodes.select("text").style("display", (node) => {
      const emphasized = node.id === focus || near?.has(node.id) || matches(node)
      return visibleIds.has(node.id) &&
        (emphasized || scale >= LABEL_ZOOM[node.tier])
        ? null
        : "none"
    })
    nodes
      .select("text")
      .attr("font-weight", (node) =>
        node.id === focus ? 700 : node.tier === "high" ? 600 : 500,
      )
      .attr("fill", (node) =>
        node.id === focus
          ? GRAPH_COLORS.selected
          : node.tier === "high"
            ? GRAPH_COLORS.core
            : GRAPH_COLORS.label,
      )

    edges
      .style("display", (edge) => {
        const source = edge.source.id ?? edge.source
        const target = edge.target.id ?? edge.target
        return visibleIds.has(source) && visibleIds.has(target) ? null : "none"
      })
      .style("opacity", (edge) => {
      const source = edge.source.id ?? edge.source
      const target = edge.target.id ?? edge.target
      if (!visibleIds.has(source) || !visibleIds.has(target)) return 0
      const touchesFocus = near?.has(source) && near?.has(target)
      if (near) return touchesFocus ? 1 : currentSelection ? 0.05 : 0.12
      if (term) return 0.12
      return 0.85
    })
    edges
      .select("line.gx-wire")
      .attr("stroke", (edge) => {
        const source = edge.source.id ?? edge.source
        const target = edge.target.id ?? edge.target
        return near?.has(source) && near?.has(target)
          ? GRAPH_COLORS.focusedEdge
          : GRAPH_COLORS.edge
      })
      .attr("stroke-width", (edge) => {
        const source = edge.source.id ?? edge.source
        const target = edge.target.id ?? edge.target
        return near?.has(source) && near?.has(target) ? 1.7 : 1
      })
    edges.select("text").style("display", (edge) => {
      const source = edge.source.id ?? edge.source
      const target = edge.target.id ?? edge.target
      const incident = focus && (source === focus || target === focus)
      if (!visibleIds.has(source) || !visibleIds.has(target)) return "none"
      if (edge === hoverEdge) return null
      if (relations && scale >= 0.7) return null
      return incident ? null : "none"
    })
    runtime.visibleIds = visibleIds
    // Tell the caption how many nodes are drawn, only when it changes.
    const drawn = currentView.nodes.filter((node) => visibleIds.has(node.id)).length
    if (drawn !== runtime.drawnCount) {
      runtime.drawnCount = drawn
      setDrawnCount(drawn)
    }
  }, [])

  runtimeRef.current = {
    ...(runtimeRef.current || {
      zoomScale: 1,
      cameraInteracted: false,
      initialFitDone: false,
      fitAfterRelayout: false,
    }),
    view,
    selectedId,
    onSelectNode,
    search,
    showRelations,
    visibleIds: runtimeRef.current?.visibleIds || new Set(),
    hoverNode: runtimeRef.current?.hoverNode || null,
    hoverEdge: runtimeRef.current?.hoverEdge || null,
    applyStyles,
  }

  const scheduleStyles = useCallback(() => {
    if (frameRef.current !== null) return
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = null
      runtimeRef.current?.applyStyles()
    })
  }, [])

  const fit = useCallback((duration = 380, userInitiated = true) => {
    if (userInitiated) runtimeRef.current.cameraInteracted = true
    const simulation = simulationRef.current
    const svg = d3.select(svgRef.current)
    if (!simulation || !zoomRef.current || !simulation.nodes().length) return

    const { width, height } = sizeRef.current
    const pad = { top: 64, right: width > 760 ? 150 : 60, bottom: 96, left: 48 }
    const frameWidth = Math.max(120, width - pad.left - pad.right)
    const frameHeight = Math.max(120, height - pad.top - pad.bottom)
    const nodes = simulation.nodes().filter((node) => runtimeRef.current.visibleIds.has(node.id))
    if (!nodes.length) return
    const x0 = Math.min(...nodes.map((node) => node.x))
    const x1 = Math.max(...nodes.map((node) => node.x))
    const y0 = Math.min(...nodes.map((node) => node.y))
    const y1 = Math.max(...nodes.map((node) => node.y)) + 34
    const scale = Math.min(
      frameWidth / Math.max(1, x1 - x0),
      frameHeight / Math.max(1, y1 - y0),
      1.6,
    )
    const transform = d3.zoomIdentity
      .translate(
        pad.left + frameWidth / 2 - (scale * (x0 + x1)) / 2,
        pad.top + frameHeight / 2 - (scale * (y0 + y1)) / 2,
      )
      .scale(scale)
    svg.transition().duration(duration).call(zoomRef.current.transform, transform)
  }, [])
  runtimeRef.current.fit = fit

  useEffect(() => {
    const svgElement = svgRef.current
    const wrapper = wrapRef.current
    if (!svgElement || !wrapper) return undefined

    const svg = d3.select(svgElement)
    const definitions = svg.append("defs")
    ;[
      ["gx-arrow", "rgba(255,255,255,0.4)"],
      ["gx-arrow-hi", GRAPH_COLORS.core],
    ].forEach(([id, fill]) => {
      definitions
        .append("marker")
        .attr("id", id)
        .attr("viewBox", "0 -5 10 10")
        .attr("refX", 22)
        .attr("refY", 0)
        .attr("orient", "auto")
        .attr("markerWidth", 5)
        .attr("markerHeight", 5)
        .append("path")
        .attr("d", "M0,-5L10,0L0,5")
        .attr("fill", fill)
    })

    const root = svg.append("g").attr("class", "gx-root")
    const edgeLayer = root.append("g").attr("class", "gx-edges")
    const nodeLayer = root.append("g").attr("class", "gx-nodes")
    const zoom = d3
      .zoom()
      .scaleExtent([0.25, 3.2])
      .on("zoom", (event) => {
        root.attr("transform", event.transform)
        runtimeRef.current.zoomScale = event.transform.k
        if (event.sourceEvent) runtimeRef.current.cameraInteracted = true
        scheduleStyles()
      })
    zoomRef.current = zoom
    svg
      .call(zoom)
      .on("click", () => {
        runtimeRef.current.onSelectNode?.(null)
      })
      .on("dblclick.zoom", null)

    const simulation = d3
      .forceSimulation([])
      .force(
        "link",
        d3
          .forceLink([])
          .id((node) => node.id)
          .distance((edge) => (edge.strength === "high" ? 110 : edge.strength === "medium" ? 140 : 170))
          .strength((edge) => (edge.strength === "high" ? 0.5 : 0.3)),
      )
      .force("charge", d3.forceManyBody().strength(-900).distanceMax(600))
      // Keeps circles and the labels under them from piling on top of each other.
      .force("collide", d3.forceCollide().radius((node) => node.r + 18).strength(0.7).iterations(2))
      .force("x", d3.forceX().strength(0.055))
      .force("y", d3.forceY().strength(0.055))
    simulationRef.current = simulation

    const rememberPositions = () => {
      simulation.nodes().forEach((node) => {
        positionsRef.current.set(node.id, {
          x: node.x,
          y: node.y,
          fx: node.fx,
          fy: node.fy,
        })
      })
    }

    simulation.on("tick", () => {
      edgeLayer
        .selectAll("g.gx-edge:not(.gx-exiting) line")
        .attr("x1", (edge) => edge.source.x)
        .attr("y1", (edge) => edge.source.y)
        .attr("x2", (edge) => edge.target.x)
        .attr("y2", (edge) => edge.target.y)
      edgeLayer
        .selectAll("g.gx-edge:not(.gx-exiting) text")
        .attr("x", (edge) => (edge.source.x + edge.target.x) / 2)
        .attr("y", (edge) => (edge.source.y + edge.target.y) / 2 - 5)
      nodeLayer
        .selectAll(".gx-node:not(.gx-exiting)")
        .attr("transform", (node) => `translate(${node.x},${node.y})`)
      rememberPositions()
    })
    simulation.on("end", () => {
      transitionPinsRef.current.forEach((position, id) => {
        const node = nodeCacheRef.current.get(id)
        if (!node) return
        node.fx = position.fx
        node.fy = position.fy
      })
      transitionPinsRef.current.clear()
      simulation.nodes().forEach((node) => {
        positionsRef.current.set(node.id, {
          x: node.x,
          y: node.y,
          fx: node.fx,
          fy: node.fy,
        })
      })
      if (runtimeRef.current.fitAfterRelayout) {
        runtimeRef.current.fitAfterRelayout = false
        runtimeRef.current.fit?.(500, false)
      } else if (
        !runtimeRef.current.initialFitDone &&
        !runtimeRef.current.cameraInteracted &&
        runtimeRef.current.view.nodes.length
      ) {
        runtimeRef.current.initialFitDone = true
        runtimeRef.current.fit?.(600, false)
      }
    })

    const resizeObserver = new ResizeObserver(() => {
      const width = wrapper.clientWidth || 900
      const height = wrapper.clientHeight || 600
      if (width === sizeRef.current.width && height === sizeRef.current.height) return
      sizeRef.current = { width, height }
      svg.attr("viewBox", `0 0 ${width} ${height}`)
      simulation.force("x", d3.forceX(width / 2).strength(0.055))
      simulation.force("y", d3.forceY(height / 2).strength(0.055))
      // Fit again once the layout settles, unless the user has moved the camera.
      if (!runtimeRef.current.cameraInteracted) runtimeRef.current.initialFitDone = false
      if (simulation.nodes().length) simulation.alpha(Math.max(simulation.alpha(), 0.06)).restart()
    })
    resizeObserver.observe(wrapper)

    return () => {
      resizeObserver.disconnect()
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current)
      simulation.stop()
      svg.on(".zoom", null).on("click", null)
      svg.selectAll("*").remove()
      simulationRef.current = null
      zoomRef.current = null
      frameRef.current = null
    }
  }, [scheduleStyles])

  useEffect(() => {
    const simulation = simulationRef.current
    const svgElement = svgRef.current
    if (!simulation || !svgElement) return

    const previousNodes = simulation.nodes()
    transitionPinsRef.current.forEach((position, id) => {
      const node = nodeCacheRef.current.get(id)
      if (!node) return
      node.fx = position.fx
      node.fy = position.fy
    })
    transitionPinsRef.current.clear()

    const { width, height } = sizeRef.current
    const previousById = new Map(previousNodes.map((node) => [node.id, node]))
    const edgeOccurrences = new Map()
    const edges = view.edges.map((edge) => {
      const signature = `${edge.source}\u0000${edge.target}\u0000${edge.label}\u0000${edge.strength}`
      const occurrence = edgeOccurrences.get(signature) || 0
      edgeOccurrences.set(signature, occurrence + 1)
      return { ...edge, id: `${signature}\u0000${occurrence}` }
    })
    const neighbors = new Map()
    edges.forEach(({ source, target }) => {
      if (!neighbors.has(source)) neighbors.set(source, [])
      if (!neighbors.has(target)) neighbors.set(target, [])
      neighbors.get(source).push(target)
      neighbors.get(target).push(source)
    })
    const nodes = view.nodes.map((data) => {
      let node = nodeCacheRef.current.get(data.id)
      if (node) Object.assign(node, data)
      else {
        node = { ...data }
        nodeCacheRef.current.set(node.id, node)
      }

      if (
        previousNodes.length &&
        (!Number.isFinite(node.x) || !Number.isFinite(node.y))
      ) {
        const nearby = (neighbors.get(node.id) || [])
          .map((id) => previousById.get(id))
          .filter((neighbor) => neighbor && Number.isFinite(neighbor.x) && Number.isFinite(neighbor.y))
        const center = nearby.length
          ? {
              x: nearby.reduce((total, neighbor) => total + neighbor.x, 0) / nearby.length,
              y: nearby.reduce((total, neighbor) => total + neighbor.y, 0) / nearby.length,
            }
          : previousNodes.length
            ? {
                x: previousNodes.reduce((total, previous) => total + previous.x, 0) / previousNodes.length,
                y: previousNodes.reduce((total, previous) => total + previous.y, 0) / previousNodes.length,
              }
            : { x: width / 2, y: height / 2 }
        let seed = 0
        for (let index = 0; index < node.id.length; index += 1) {
          seed = (seed * 31 + node.id.charCodeAt(index)) >>> 0
        }
        const angle = (seed / 0x100000000) * Math.PI * 2
        const radius = 28 + (seed % 5) * 8
        node.x = center.x + Math.cos(angle) * radius
        node.y = center.y + Math.sin(angle) * radius
      }
      return node
    })
    const addedNodes = nodes.filter((node) => !previousById.has(node.id))
    const root = d3.select(svgElement).select("g.gx-root")

    const edgeSelection = root
      .select("g.gx-edges")
      .selectAll("g.gx-edge")
      .data(edges, (edge) => edge.id)
    edgeSelection
      .exit()
      .classed("gx-exiting", true)
      .transition()
      .duration(220)
      .style("opacity", 0)
      .remove()
    const edgeEnter = edgeSelection.enter().append("g").attr("class", "gx-edge")
    edgeEnter
      .append("line")
      .attr("class", "gx-hit")
      .attr("stroke", "transparent")
      .attr("stroke-width", 12)
      .style("cursor", "pointer")
    edgeEnter
      .append("line")
      .attr("class", "gx-wire")
      .attr("stroke-linecap", "round")
      .style("pointer-events", "none")
    edgeEnter
      .append("text")
      .attr("class", "gx-edge-label")
      .attr("text-anchor", "middle")
      .style("pointer-events", "none")
    edgeEnter.classed("gx-entering", true)
    edgeEnter.selectAll("line.gx-wire, text").style("opacity", 0)
    const allEdges = edgeEnter.merge(edgeSelection).classed("gx-exiting", false).interrupt()
    allEdges
      .select("line.gx-wire")
      .attr("stroke-dasharray", (edge) => (edge.strength === "low" ? "4,4" : null))
      .attr("marker-end", (edge) =>
        edge.strength === "high" ? "url(#gx-arrow-hi)" : "url(#gx-arrow)",
      )
    allEdges.select("text").text((edge) => edge.label)
    allEdges
      .select("line.gx-hit")
      .on("mouseenter", (_, edge) => {
        runtimeRef.current.hoverEdge = edge
        scheduleStyles()
      })
      .on("mouseleave", () => {
        runtimeRef.current.hoverEdge = null
        scheduleStyles()
      })
    edgeEnter
      .selectAll("line.gx-wire, text")
      .transition()
      .duration(220)
      .style("opacity", 1)
      .on("end", function () {
        d3.select(this.parentNode).classed("gx-entering", false)
      })

    const drag = d3
      .drag()
      .on("start", (event, node) => {
        transitionPinsRef.current.delete(node.id)
        if (!event.active) simulation.alphaTarget(0.12).restart()
        node.fx = node.x
        node.fy = node.y
      })
      .on("drag", (event, node) => {
        node.fx = event.x
        node.fy = event.y
      })
      .on("end", (event, node) => {
        if (!event.active) simulation.alphaTarget(0)
        node.fx = node.x
        node.fy = node.y
        positionsRef.current.set(node.id, { x: node.x, y: node.y, fx: node.fx, fy: node.fy })
      })

    const nodeSelection = root
      .select("g.gx-nodes")
      .selectAll("g.gx-node")
      .data(nodes, (node) => node.id)
    nodeSelection
      .exit()
      .classed("gx-exiting", true)
      .transition()
      .duration(220)
      .style("opacity", 0)
      .remove()
    const nodeEnter = nodeSelection
      .enter()
      .append("g")
      .attr("class", "gx-node")
      .attr("tabindex", 0)
      .attr("role", "button")
      .style("cursor", "pointer")
    nodeEnter.classed("gx-entering", true)
    nodeEnter.append("circle")
    nodeEnter.select("circle").style("opacity", 0)
    nodeEnter
      .append("text")
      .attr("text-anchor", "middle")
      .attr("paint-order", "stroke")
      .attr("stroke", "rgba(20,17,9,0.92)")
      .attr("stroke-width", "3.2px")
      .style("pointer-events", "none")
    nodeEnter.select("text").style("opacity", 0)
    const allNodes = nodeEnter.merge(nodeSelection).classed("gx-exiting", false).interrupt()
    allNodes
      .attr("aria-label", (node) => `${node.name}, ${node.deg} connection${node.deg === 1 ? "" : "s"}`)
      .call(drag)
      .on("click", (event, node) => {
        event.stopPropagation()
        runtimeRef.current.onSelectNode?.(node)
      })
      .on("keydown", (event, node) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault()
          runtimeRef.current.onSelectNode?.(node)
        }
      })
      .on("mouseenter", (event, node) => {
        runtimeRef.current.hoverNode = node.id
        scheduleStyles()
        setTooltip({ name: node.name, degree: node.deg, week: node.week })
        updateTooltipPosition(event)
      })
      .on("mousemove", updateTooltipPosition)
      .on("mouseleave", () => {
        runtimeRef.current.hoverNode = null
        scheduleStyles()
        setTooltip(null)
      })
      .on("focus", (_, node) => {
        runtimeRef.current.hoverNode = node.id
        scheduleStyles()
      })
      .on("blur", () => {
        runtimeRef.current.hoverNode = null
        scheduleStyles()
      })
    allNodes
      .select("circle")
      .attr("fill", (node) =>
        node.status === "needs_regeneration"
          ? GRAPH_COLORS.degraded
          : node.week === week
            ? GRAPH_COLORS.added
            : node.tier === "high"
              ? GRAPH_COLORS.core
              : GRAPH_COLORS.support,
      )
      .attr("stroke", (node) => (node.status === "needs_regeneration" ? "#F2B38C" : null))
      .attr("stroke-width", (node) => (node.status === "needs_regeneration" ? 2 : null))
      .attr("r", (node) => node.r)
    nodeEnter
      .selectAll("circle, text")
      .transition()
      .duration(260)
      .style("opacity", 1)
      .on("end", function () {
        d3.select(this.parentNode).classed("gx-entering", false)
      })
    allNodes
      .select("text")
      .attr("font-size", (node) => (node.tier === "high" ? 11.5 : 10.5))
      .each(function (node) {
        d3.select(this)
          .selectAll("tspan")
          .data(node.lines)
          .join("tspan")
          .attr("x", 0)
          .attr("y", (_, index) => ((index - (node.lines.length - 1) / 2) * LINE_HEIGHT) + node.r + 13)
          .text((line) => line)
      })

    const initialLayout = !runtimeRef.current.graphInitialized && nodes.length > 0
    if (initialLayout) runtimeRef.current.graphInitialized = true
    if (!initialLayout && addedNodes.length) {
      previousNodes.forEach((node) => {
        transitionPinsRef.current.set(node.id, { fx: node.fx, fy: node.fy })
        node.fx = node.x
        node.fy = node.y
      })
    }

    simulation.nodes(nodes)
    simulation.force("link").links(edges)
    if (initialLayout) simulation.alpha(0.9).restart()
    else if (addedNodes.length) simulation.alpha(0.22).restart()
    else simulation.alpha(0).stop()
    scheduleStyles()

  }, [scheduleStyles, updateTooltipPosition, view, week])

  useEffect(() => {
    if (!tooltip || !pointerRef.current) return
    const element = tooltipRef.current
    const wrapper = wrapRef.current
    if (!element || !wrapper) return
    const bounds = wrapper.getBoundingClientRect()
    element.style.left = `${pointerRef.current.x - bounds.left + 14}px`
    element.style.top = `${pointerRef.current.y - bounds.top + 14}px`
  }, [tooltip])

  useEffect(() => {
    scheduleStyles()
  }, [scheduleStyles, selectedId, search, showRelations, view])

  const zoomBy = useCallback((factor) => {
    if (!zoomRef.current) return
    runtimeRef.current.cameraInteracted = true
    d3.select(svgRef.current)
      .transition()
      .duration(220)
      .call(zoomRef.current.scaleBy, factor)
  }, [])

  const relayout = useCallback(() => {
    const simulation = simulationRef.current
    if (!simulation) return
    positionsRef.current.clear()
    runtimeRef.current.cameraInteracted = false
    runtimeRef.current.initialFitDone = false
    runtimeRef.current.fitAfterRelayout = true
    const nodes = simulation.nodes()
    nodes.forEach((node) => {
      node.x = undefined
      node.y = undefined
      node.fx = null
      node.fy = null
    })
    simulation.nodes(nodes).alpha(0.95).restart()
  }, [])

  const locate = useCallback((name) => {
    const simulation = simulationRef.current
    if (!simulation || !zoomRef.current) return
    const term = name.trim().toLowerCase()
    const node =
      simulation.nodes().find((item) => item.name.toLowerCase() === term) ||
      simulation.nodes().find((item) => nodeMatches(item, term))
    if (!node) return

    runtimeRef.current.cameraInteracted = true
    const { width, height } = sizeRef.current
    const scale = Math.max(1.5, runtimeRef.current.zoomScale)
    const transform = d3.zoomIdentity
      .translate(width / 2 - scale * node.x, height / 2 - scale * node.y)
      .scale(scale)
    d3.select(svgRef.current)
      .transition()
      .duration(450)
      .call(zoomRef.current.transform, transform)
    runtimeRef.current.onSelectNode?.(node)
  }, [])

  const clearHover = useCallback(() => {
    runtimeRef.current.hoverNode = null
    runtimeRef.current.hoverEdge = null
    scheduleStyles()
    setTooltip(null)
  }, [scheduleStyles])

  return { svgRef, wrapRef, tooltipRef, tooltip, drawnCount, fit, zoomBy, relayout, locate, clearHover }
}
