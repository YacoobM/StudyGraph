import { useMemo, useState } from "react"
import useConceptGraph from "./useConceptGraph.js"
import { buildGraphView, GRAPH_COLORS } from "./conceptGraphModel.js"
import "./ConceptGraph.css"

export default function ConceptGraph({
  nodes: allNodes = [],
  edges: allEdges = [],
  versions = [],
  selectedVersionId,
  onVersionChange,
  loadingVersion = false,
  week = Number.POSITIVE_INFINITY,
  onSelectNode,
  selectedId,
  degradedCount = 0,
  onRefreshDegraded,
  refreshingDegraded = false,
}) {
  const [search, setSearch] = useState("")
  const [showRelations, setShowRelations] = useState(false)
  const [hideIsolated, setHideIsolated] = useState(false)

  const view = useMemo(
    () => buildGraphView(allNodes, allEdges, week, hideIsolated),
    [allNodes, allEdges, week, hideIsolated],
  )
  const suggestions = useMemo(() => {
    const term = search.trim().toLowerCase()
    return term
      ? view.nodes.filter((node) => node.name.toLowerCase().includes(term)).slice(0, 8)
      : []
  }, [search, view.nodes])
  const graph = useConceptGraph({
    view,
    selectedId,
    onSelectNode,
    search,
    showRelations,
    week,
  })

  function reset() {
    setSearch("")
    setShowRelations(false)
    setHideIsolated(false)
    onSelectNode?.(null)
    graph.clearHover()
    graph.fit()
  }

  return (
    <div className="graph-panel-inner" ref={graph.wrapRef} onMouseLeave={graph.clearHover}>
      {degradedCount > 0 && (
        <div className="gx-degraded-banner">
          <span>
            <strong>
              {degradedCount === 1 ? "1 concept needs an updated definition" : `${degradedCount} concepts need updated definitions`}
            </strong>
            <small>Some source material was removed from this graph.</small>
          </span>
          <button type="button" onClick={onRefreshDegraded} disabled={refreshingDegraded}>
            {refreshingDegraded
              ? "Refreshing…"
              : `Refresh ${degradedCount} definition${degradedCount === 1 ? "" : "s"}`}
          </button>
        </div>
      )}

      <div className="gx-legend" aria-hidden="true">
        <span><i style={{ background: GRAPH_COLORS.core }} />Core concept</span>
        <span><i style={{ background: GRAPH_COLORS.added }} />Added this week</span>
        <span><i style={{ background: GRAPH_COLORS.support }} />Supporting idea</span>
        <span><i style={{ background: GRAPH_COLORS.degraded }} />Degraded</span>
      </div>

      <div className="gx-search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" aria-hidden="true">
          <circle cx="11" cy="11" r="7" />
          <path d="m16.5 16.5 4 4" />
        </svg>
        <input
          value={search}
          list="gx-concepts"
          placeholder="Search concepts"
          aria-label="Search concepts"
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && search.trim()) graph.locate(search)
          }}
        />
        <datalist id="gx-concepts">
          {suggestions.map((node) => <option key={node.id} value={node.name} />)}
        </datalist>
        {search && (
          <button
            type="button"
            className="gx-search-clear"
            aria-label="Clear search"
            onClick={() => setSearch("")}
          >
            ×
          </button>
        )}
      </div>

      <div className="gx-toolbar" role="toolbar" aria-label="Graph controls">
        <div className="gx-group">
          <button type="button" className="gx-btn" onClick={() => graph.zoomBy(1.35)}
                  title="Zoom in" aria-label="Zoom in">+</button>
          <button type="button" className="gx-btn" onClick={() => graph.zoomBy(0.74)}
                  title="Zoom out" aria-label="Zoom out">−</button>
          <button type="button" className="gx-btn gx-btn-wide" onClick={() => graph.fit()}
                  title="Fit the whole graph on screen" aria-label="Fit to screen">Fit</button>
        </div>
        <div className="gx-group">
          <button type="button" className="gx-btn gx-btn-wide" onClick={graph.relayout}
                  title="Run the layout again from scratch" aria-label="Re-layout">Re-layout</button>
          <button type="button" className="gx-btn gx-btn-wide" onClick={reset}
                  title="Clear selection, search and filters, then fit" aria-label="Reset the view">Reset</button>
        </div>
        <div className="gx-group">
          <button
            type="button"
            className={`gx-btn gx-btn-wide${showRelations ? " on" : ""}`}
            aria-pressed={showRelations}
            title="Show every relationship label at once"
            onClick={() => setShowRelations((value) => !value)}
          >
            Relationships
          </button>
          {view.isolatedCount > 0 && (
            <button
              type="button"
              className={`gx-btn gx-btn-wide${hideIsolated ? " on" : ""}`}
              aria-pressed={hideIsolated}
              title={`${view.isolatedCount} concept${view.isolatedCount === 1 ? " has" : "s have"} no recorded relationship`}
              onClick={() => setHideIsolated((value) => !value)}
            >
              {hideIsolated ? "Show" : "Hide"} unlinked
            </button>
          )}
        </div>
      </div>

      <svg
        ref={graph.svgRef}
        className="gx-svg"
        role="application"
        aria-label={`Concept graph: ${view.nodes.length} concepts, ${view.edges.length} relationships`}
      />

      <div className="gx-hint">Drag to pin · click to inspect · hover a link for its relationship</div>

      {graph.tooltip && (
        <div
          ref={graph.tooltipRef}
          className="node-tip show"
        >
          <b>{graph.tooltip.name}</b>
          <br />
          {graph.tooltip.degree} connection{graph.tooltip.degree === 1 ? "" : "s"} · week {graph.tooltip.week}
        </div>
      )}

      <GraphVersionTimeline
        versions={versions}
        selectedVersionId={selectedVersionId}
        onVersionChange={onVersionChange}
        loading={loadingVersion}
        inView={view.nodes.length}
        drawn={graph.drawnCount ?? view.nodes.length}
        total={allNodes.length}
      />
    </div>
  )
}

function GraphVersionTimeline({ versions, selectedVersionId, onVersionChange, loading, inView, drawn, total }) {
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
          Version <span className="num">{selected.version_number}</span> of{" "}
          {chronological[chronological.length - 1].version_number}
        </span>
        <span>
          {loading
            ? "Loading version…"
            : drawn < inView
              ? `Showing ${drawn} of ${total} concepts · zoom in for more`
              : inView < total
                ? `Showing ${inView} of ${total} concepts`
                : `${total} concepts · click one to inspect`}
        </span>
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
