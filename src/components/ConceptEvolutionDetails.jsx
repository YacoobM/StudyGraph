import React from 'react'
import EvolutionGraph from './EvolutionGraph'

/*
 * ConceptEvolutionDetails
 * The expensive evolution tier's content — rendered inline inside
 * NodeDetails once the user asks for it (see GraphViewer's
 * handleShowEvolution). Purely presentational: no header/close chrome and
 * no description section, since NodeDetails already renders both around
 * this — this used to be its own full page (the old Evolution route), which
 * is why the description and the request-evolution button lived here too;
 * folding it into the same panel removed the need for both.
 *
 * consecutive_comparisons is only rendered when there's more than one:
 * for a concept spanning exactly two weeks, "Overall Evolution" already
 * *is* the week1-vs-week2 comparison (the backend reuses the same text —
 * see evolution_data.get_concept_detail), so showing both would just repeat
 * the same paragraph under two headings.
 */
export default function ConceptEvolutionDetails({ evolution }) {
  const { details, subgraph } = evolution

  return (
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

      {details.consecutive_comparisons?.length > 1 && details.consecutive_comparisons.map((comp, i) => (
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
              {week.is_fallback && <span className="fallback-tag" title="Groq summary generation failed here — this is raw excerpt text, not a summary.">unsummarized excerpt</span>}
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
  )
}
