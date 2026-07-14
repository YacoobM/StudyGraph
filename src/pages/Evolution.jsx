import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { fetchGraphData, fetchConceptDescription, fetchConcept, fetchConceptSubgraph } from '../services/api'
import ConceptDetailPanel from '../components/ConceptDetailPanel'
import EvolutionGraph from '../components/EvolutionGraph'
import GraphLegend from '../components/GraphLegend'
import './Evolution.css'

export default function Evolution() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [graphData, setGraphData] = useState({ nodes: [], edges: [] })
  const [legend, setLegend] = useState(null)
  const [loading, setLoading] = useState(true)
  const [selectedConceptName, setSelectedConceptName] = useState(null)
  const [description, setDescription] = useState(null)
  const [descriptionLoading, setDescriptionLoading] = useState(false)
  // Full evolution (all weeks + comparisons + timeline) is the expensive
  // tier — only fetched when the user explicitly asks via the "Show
  // Concept Evolution" button, not on a plain node click.
  const [evolution, setEvolution] = useState(null)
  const [evolutionLoading, setEvolutionLoading] = useState(false)

  useEffect(() => {
    async function loadInitialData() {
      try {
        const data = await fetchGraphData()
        setGraphData(data)
        setLegend(data.legend || null)
      } catch (error) {
        console.error('Failed to fetch evolution graph data:', error)
      } finally {
        setLoading(false)
      }
    }
    loadInitialData()
  }, [])

  const handleNodeClick = useCallback(async (node) => {
    if (!node || node.concept_name === selectedConceptName) {
      setSelectedConceptName(null)
      setDescription(null)
      setEvolution(null)
      return
    }

    setSelectedConceptName(node.concept_name)
    setDescription(null)
    setEvolution(null)
    setDescriptionLoading(true)

    try {
      const desc = await fetchConceptDescription(node.concept_name)
      setDescription(desc)
    } catch (error) {
      console.error(`Failed to fetch description for ${node.concept_name}:`, error)
      setDescription(null)
    } finally {
      setDescriptionLoading(false)
    }
  }, [selectedConceptName])

  const handleShowEvolution = useCallback(async () => {
    if (!selectedConceptName) return
    setEvolutionLoading(true)
    try {
      const [details, subgraph] = await Promise.all([
        fetchConcept(selectedConceptName),
        fetchConceptSubgraph(selectedConceptName),
      ])
      setEvolution({ details, subgraph })
    } catch (error) {
      console.error(`Failed to fetch evolution for ${selectedConceptName}:`, error)
      setEvolution(null)
    } finally {
      setEvolutionLoading(false)
    }
  }, [selectedConceptName])

  // Effect to handle pre-selection from URL query parameter
  useEffect(() => {
    const conceptToSelect = searchParams.get('concept')
    // Ensure data is loaded, a concept is specified, and we haven't already selected something.
    if (conceptToSelect && graphData.nodes.length > 0 && !selectedConceptName) {
      const nodeToSelect = graphData.nodes.find(n => n.concept_name === conceptToSelect)
      if (nodeToSelect) {
        // The click handler only needs an object with a `concept_name` property.
        // This triggers the description-loading flow (same as a real click).
        handleNodeClick({ concept_name: nodeToSelect.concept_name })
      }
    }
  }, [graphData, searchParams, handleNodeClick, selectedConceptName])

  const handleCloseDetail = () => {
    setSelectedConceptName(null)
    setDescription(null)
    setEvolution(null)
  }

  return (
    <div className="evolution-page">
      <header className="topbar">
        <div className="topbar-left">
          <button className="icon-btn" onClick={() => navigate('/builder')} title="Back to Builder">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div className="brand">
            <span>StudyGraph / Evolution</span>
          </div>
        </div>
      </header>

      <main className="evolution-body">
        <div className="evolution-graph-container">
          {loading ? (
            <div className="loading-overlay">Loading Evolution Graph...</div>
          ) : (
            <EvolutionGraph nodes={graphData.nodes} edges={graphData.edges} onNodeClick={handleNodeClick} selectedConceptName={selectedConceptName} />
          )}
          {!loading && <GraphLegend legend={legend} />}
        </div>
        <ConceptDetailPanel
          conceptName={selectedConceptName}
          description={description}
          descriptionLoading={descriptionLoading}
          evolution={evolution}
          evolutionLoading={evolutionLoading}
          onShowEvolution={handleShowEvolution}
          onClose={handleCloseDetail}
        />
      </main>
    </div>
  )
}