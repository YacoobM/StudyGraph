import React from 'react'
import EvolutionGraph from './EvolutionGraph'

/*
 * ConceptDetailPanel
 * Two cost tiers: `description` (one cheap LLM call, earliest-week
 * summary) loads automatically on node click and is always shown once
 * loaded. `evolution` (every week's summary + every week-to-week
 * comparison + the timeline) is the expensive tier — it only loads when
 * the user presses "Show Concept Evolution", never automatically.
 */
export default function ConceptDetailPanel({
  conceptName, description, descriptionLoading,
  evolution, evolutionLoading, onShowEvolution, onClose,
}) {
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
        {descriptionLoading ? (
          <div className="loading-overlay">Loading concept description...</div>
        ) : !description ? (
          <div className="error-state">Could not load details for this concept.</div>
        ) : (
          <>
            <div className="detail-section">
              <h4>Description</h4>
              <p>{description.summary}</p>
            </div>

            {!evolution && (
              <button className="show-evolution-btn" onClick={onShowEvolution} disabled={evolutionLoading}>
                {evolutionLoading ? 'Loading concept evolution…' : 'Show Concept Evolution'}
              </button>
            )}

            {evolution && (
              <>
                <div className="detail-section">
                  <h4>Overall Evolution</h4>
                  <p>{evolution.details.comparison}</p>
                </div>

                <div className="detail-section">
                  <h4>Evolution Timeline</h4>
                  <div className="subgraph-container">
                    {evolution.subgraph ? (
                      <EvolutionGraph nodes={evolution.subgraph.nodes} edges={evolution.subgraph.edges} onNodeClick={() => {}} />
                    ) : (
                      <div>No subgraph data.</div>
                    )}
                  </div>
                </div>

                {evolution.details.consecutive_comparisons?.map((comp, i) => (
                  <div className="detail-section" key={i}>
                    <h4>{comp.from_week} → {comp.to_week}</h4>
                    <p>{comp.comparison}</p>
                  </div>
                ))}

                <div className="detail-section">
                  <h4>Weekly Summary</h4>
                  <ul className="week-summary-list">
                    {evolution.details.weeks_data?.map(week => (
                      <li key={week.week}>
                        <strong>{week.week}:</strong> {week.summary}
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="detail-section">
                  <h4>Related Concepts</h4>
                  <div className="related-concepts">
                    {evolution.details.related_concepts?.map(rel => (
                      <span key={rel} className="related-tag">{rel}</span>
                    ))}
                  </div>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </aside>
  )
}