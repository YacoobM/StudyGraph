import React from 'react'

/*
 * GraphLegend
 * Explains the node color coding on the knowledge graph: one color per
 * week a concept first appeared in, plus dedicated colors for concepts
 * that span multiple weeks (the ones "View Concept Evolution" applies to)
 * and concepts with no week data.
 */
export default function GraphLegend({ legend }) {
  if (!legend) return null

  const { weeks = [], multi_week, no_week_data } = legend

  return (
    <div className="graph-legend">
      <div className="graph-legend-title">Week</div>
      {weeks.map((w) => (
        <div className="graph-legend-item" key={w.week}>
          <span className="graph-legend-swatch" style={{ background: w.color }} />
          <span>{w.week}</span>
        </div>
      ))}
      {multi_week && (
        <div className="graph-legend-item">
          <span className="graph-legend-swatch" style={{ background: multi_week.color }} />
          <span>{multi_week.label}</span>
        </div>
      )}
      {no_week_data && (
        <div className="graph-legend-item">
          <span className="graph-legend-swatch" style={{ background: no_week_data.color }} />
          <span>{no_week_data.label}</span>
        </div>
      )}
    </div>
  )
}
