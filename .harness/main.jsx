/* Temporary harness: mounts the real ConceptGraph with a graph shaped exactly
   like the one Workspace builds from GET /api/graph. Not part of the app. */
import React, { useState } from 'react'
import ReactDOM from 'react-dom/client'
import ConceptGraph from '../src/components/ConceptGraph.jsx'
import Inspector from '../src/components/Inspector.jsx'
import '../src/styles/global.css'
import '../src/styles/studygraph.css'
import '../src/pages/Workspace.css'

const NAMES = [
  'Parallel Computing','Parallel Programming Models','Shared Memory','Distributed Computing',
  'Concurrency','Memory Architecture','Performance Scalability','Amdahl Law','Speedup',
  'Thread Synchronisation','Mutex','Semaphore','Race Condition','Deadlock','Cache Coherence',
  'NUMA','SIMD','MIMD','Flynn Taxonomy','Message Passing Interface','OpenMP','Pthreads',
  'Inter Process Communication','BSD Socket API','TCP Sockets','UDP Sockets','Client Server Model',
  'Remote Procedure Call','HTTP Protocol','Network Latency','Bandwidth','Packet Switching',
  'Load Balancing','Task Decomposition','Data Parallelism','Task Parallelism','Granularity',
  'Barrier Synchronisation','Atomic Operations','Lock Free Programming','False Sharing',
  'Vectorisation','GPU Computing','CUDA Kernel','Heterogeneous Computing','Profiling Tools',
  'Stranded Concept A','Stranded Concept B',
]
const slug = (s) => s.toLowerCase().replace(/[^a-z0-9]+/g, '_')

// deterministic edge set: each node links to a few earlier ones, last two are isolated
const nodes = NAMES.map((name, i) => ({
  id: slug(name), name,
  week: i < 22 ? 1 : i < 38 ? 2 : 3,
  weeks: [i < 22 ? 1 : i < 38 ? 2 : 3],
  description: '',
  sources: [i < 22 ? 'FIT3143-Topic-1-2024.pdf' : 'FIT3143-Topic-2-2024.pdf'],
}))
const edges = []
NAMES.forEach((name, i) => {
  if (i === 0 || name.startsWith('Stranded')) return
  const fan = i % 7 === 0 ? 3 : i % 3 === 0 ? 2 : 1
  for (let k = 1; k <= fan; k += 1) {
    const j = Math.max(0, i - k * ((i % 5) + 1))
    if (j === i || NAMES[j].startsWith('Stranded')) continue
    edges.push({
      source: slug(name), target: slug(NAMES[j]),
      label: ['depends on','relates to','contrasts with','enables','is measured by'][(i + k) % 5],
      strength: k === 1 ? 'high' : 'medium',
    })
  }
})
const degree = {}
edges.forEach((e) => { degree[e.source] = (degree[e.source] || 0) + 1; degree[e.target] = (degree[e.target] || 0) + 1 })
const ranked = nodes.map((n) => degree[n.id] || 0).sort((a, b) => b - a)
const cut = Math.max(1, ranked[Math.floor(ranked.length / 3)] ?? 1)
nodes.forEach((n) => { n.degree = degree[n.id] || 0; n.kind = n.degree >= cut ? 'core' : 'support' })

window.__HARNESS__ = { nodes: nodes.length, edges: edges.length }

function App() {
  const [week, setWeek] = useState(3)
  const [selected, setSelected] = useState(null)
  window.__SELECTED__ = selected
  const pretty = 'FIT3143 Topic 2 2024'
  return (
    <div className="ws" style={{ height: '100vh' }}>
      <main className="ws-main">
        <header className="ws-top">
          <div className="ws-head">
            <div className="ws-title" title="FIT3143-Topic-2-2024.pdf">{pretty}</div>
            <div className="ws-facts">
              <span><b>{nodes.length}</b> concepts</span>
              <span><b>{edges.length}</b> relationships</span>
              <span>Week <b>{week}</b> of 3</span>
            </div>
          </div>
        </header>
        <section className="ws-body">
          <div className="graph-canvas-wrap">
            <ConceptGraph
              nodes={nodes} edges={edges} week={week} maxWeek={3}
              onWeekChange={setWeek} onSelectNode={setSelected}
              selectedId={selected?.id}
            />
          </div>
          {selected && (
            <Inspector node={selected} nodes={nodes} edges={edges} maxWeek={3}
                       onClose={() => setSelected(null)} />
          )}
        </section>
      </main>
    </div>
  )
}
ReactDOM.createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>)
