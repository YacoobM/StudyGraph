import React from 'react'
import EvolutionGraph from './EvolutionGraph'

export default function ConceptDetailPanel({ conceptName, details, subgraph, loading, onClose }) {
  if (!conceptName) {
    return (
      <aside className="concept-detail-panel is-empty">
        <div className="empty-state">
          <h3>Select a Concept</h3>
          <p>Click on a node in the graph to see how it evolves over time.</p>
        </div>
      </aside>
    )
  }

  return (
    <aside className="concept-detail-panel">
      <header className="panel-header">
        <h3>{conceptName}</h3>
        <button onClick={onClose} className="close-btn">×</button>
      </header>
      <div className="panel-content">
        {loading ? (
          <div className="loading-overlay">Loading concept details...</div>
        ) : !details ? (
          <div className="error-state">Could not load details for this concept.</div>
        ) : (
          <>
            <div className="detail-section">
              <h4>Overall Evolution</h4>
              <p>{details.comparison}</p>
            </div>

            <div className="detail-section">
              <h4>Evolution Timeline</h4>
              <div className="subgraph-container">
                {subgraph ? (
                  <EvolutionGraph nodes={subgraph.nodes} edges={subgraph.edges} onNodeClick={() => {}} />
                ) : (
                  <div>No subgraph data.</div>
                )}
              </div>
            </div>

            {details.consecutive_comparisons?.map((comp, i) => (
              <div className="detail-section" key={i}>
                <h4>{comp.from_week} → {comp.to_week}</h4>
                <p>{comp.comparison}</p>
              </div>
            ))}

            <div className="detail-section">
              <h4>Weekly Summary</h4>
              <ul className="week-summary-list">
                {details.weeks_data?.map(week => (
                  <li key={week.week}>
                    <strong>{week.week}:</strong> {week.summary}
                  </li>
                ))}
              </ul>
            </div>

            <div className="detail-section">
              <h4>Related Concepts</h4>
              <div className="related-concepts">
                {details.related_concepts?.map(rel => (
                  <span key={rel} className="related-tag">{rel}</span>
                ))}
              </div>
            </div>
          </>
        )}
      </div>
    </aside>
  )
}