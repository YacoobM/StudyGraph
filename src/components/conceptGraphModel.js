export const GRAPH_COLORS = {
  core: "rgba(243,238,221,0.92)",
  added: "#C1652F",
  support: "rgba(150,137,122,0.82)",
  degraded: "#A84F3A",
  selected: "#F7F2E4",
  matched: "#E0A06A",
  label: "rgba(176,165,150,0.94)",
  edge: "rgba(255,255,255,0.17)",
  focusedEdge: "rgba(243,238,221,0.6)",
}

export const LABEL_ZOOM = { high: 0, mid: 0.85, low: 1.35 }

export function progressiveNodeIds(nodes, scale) {
  const visibleRatio =
    scale < 0.5 ? 0.12
      : scale < 0.8 ? 0.25
        : scale < 1.2 ? 0.45
          : scale < 1.8 ? 0.7
            : 1
  const ranked = [...nodes].sort(
    (left, right) => right.deg - left.deg || left.id.localeCompare(right.id),
  )
  const visibleCount = Math.min(
    ranked.length,
    Math.max(1, Math.ceil(ranked.length * visibleRatio)),
  )
  return new Set(ranked.slice(0, visibleCount).map((node) => node.id))
}

export function wrapLabel(value) {
  const words = String(value).split(/\s+/)
  if (words.length < 2 || value.length <= 15) return [value]

  let bestIndex = 1
  let bestDifference = Number.POSITIVE_INFINITY
  for (let index = 1; index < words.length; index += 1) {
    const leftLength = words.slice(0, index).join(" ").length
    const rightLength = words.slice(index).join(" ").length
    const difference = Math.abs(leftLength - rightLength)
    if (difference < bestDifference) {
      bestDifference = difference
      bestIndex = index
    }
  }

  return [words.slice(0, bestIndex).join(" "), words.slice(bestIndex).join(" ")]
    .map((line) => (line.length > 26 ? `${line.slice(0, 25)}…` : line))
}

export function buildGraphView(allNodes, allEdges, week, hideIsolated) {
  const inWeek = allNodes.filter((node) => (node.week || 1) <= week)
  const ids = new Set(inWeek.map((node) => String(node.id)))
  const edges = allEdges
    .map((edge) => ({
      source: String(edge.source?.id ?? edge.source),
      target: String(edge.target?.id ?? edge.target),
      label: edge.label || "relates to",
      strength: edge.strength || "medium",
    }))
    .filter((edge) => ids.has(edge.source) && ids.has(edge.target) && edge.source !== edge.target)

  const degree = new Map(inWeek.map((node) => [String(node.id), 0]))
  edges.forEach(({ source, target }) => {
    degree.set(source, (degree.get(source) || 0) + 1)
    degree.set(target, (degree.get(target) || 0) + 1)
  })

  const maxDegree = Math.max(1, ...degree.values())
  const sortedDegrees = inWeek
    .map((node) => degree.get(String(node.id)) || 0)
    .sort((a, b) => b - a)
  const highCut = sortedDegrees[Math.max(0, Math.floor(sortedDegrees.length * 0.25) - 1)] ?? 1
  const lowCut = sortedDegrees[Math.min(sortedDegrees.length - 1, Math.floor(sortedDegrees.length * 0.6))] ?? 0

  let nodes = inWeek.map((node) => {
    const id = String(node.id)
    const nodeDegree = degree.get(id) || 0
    const tier = nodeDegree >= Math.max(1, highCut) ? "high" : nodeDegree <= lowCut ? "low" : "mid"
    const name = node.name || id.replace(/_/g, " ")
    return {
      ...node,
      id,
      name,
      deg: nodeDegree,
      tier,
      isolated: nodeDegree === 0,
      lines: wrapLabel(name),
      r: nodeDegree === 0 ? 6 : Math.min(18, 7.5 + 10 * (nodeDegree / maxDegree) ** 0.7),
    }
  })

  const isolatedCount = nodes.filter((node) => node.isolated).length
  if (hideIsolated) nodes = nodes.filter((node) => !node.isolated)

  const visibleIds = new Set(nodes.map((node) => node.id))
  const visibleEdges = edges.filter(
    (edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
  )
  const neighbours = new Map(nodes.map((node) => [node.id, new Set()]))
  visibleEdges.forEach(({ source, target }) => {
    neighbours.get(source)?.add(target)
    neighbours.get(target)?.add(source)
  })

  return { nodes, edges: visibleEdges, neighbours, isolatedCount }
}
