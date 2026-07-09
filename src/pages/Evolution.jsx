import React, { useEffect, useState, useCallback } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { fetchGraphData, fetchConcept, fetchConceptSubgraph } from '../services/api'
import ConceptDetailPanel from '../components/ConceptDetailPanel'
import EvolutionGraph from '../components/EvolutionGraph'
import './Evolution.css'

export default function Evolution() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [graphData, setGraphData] = useState({ nodes: [], edges: [] })
  const [loading, setLoading] = useState(true)
  const [selectedConceptName, setSelectedConceptName] = useState(null)
  const [selectedConceptData, setSelectedConceptData] = useState(null)
  const [selectedConceptSubgraph, setSelectedConceptSubgraph] = useState(null)
  const [detailLoading, setDetailLoading] = useState(false)

  useEffect(() => {
    async function loadInitialData() {
      try {
        const data = await fetchGraphData()
        setGraphData(data)
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
      setSelectedConceptData(null)
      setSelectedConceptSubgraph(null)
      return
    }

    setSelectedConceptName(node.concept_name)
    setDetailLoading(true)

    try {
      const [details, subgraph] = await Promise.all([
        fetchConcept(node.concept_name),
        fetchConceptSubgraph(node.concept_name),
      ])
      setSelectedConceptData(details)
      setSelectedConceptSubgraph(subgraph)
    } catch (error) {
      console.error(`Failed to fetch details for ${node.concept_name}:`, error)
      setSelectedConceptData(null)
      setSelectedConceptSubgraph(null)
    } finally {
      setDetailLoading(false)
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
        // This triggers the full detail-loading flow.
        handleNodeClick({ concept_name: nodeToSelect.concept_name })
      }
    }
  }, [graphData, searchParams, handleNodeClick, selectedConceptName])

  const handleCloseDetail = () => {
    setSelectedConceptName(null)
    setSelectedConceptData(null)
    setSelectedConceptSubgraph(null)
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
        </div>
        <ConceptDetailPanel conceptName={selectedConceptName} details={selectedConceptData} subgraph={selectedConceptSubgraph} loading={detailLoading} onClose={handleCloseDetail} />
      </main>
    </div>
  )
}