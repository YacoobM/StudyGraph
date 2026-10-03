import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Subject → Topic → Graph organisation for the workspace sidebar.
 *
 * A subject holds topics, a topic holds any number of graphs, and each graph
 * is a set of uploaded documents. Opening a graph shows the concepts that came
 * from its documents.
 *
 * Graphs are created lazily: a graph entry exists locally (with name and docIds)
 * until the first document is uploaded, at which point a Supabase graph is
 * created and the local id is replaced with the backend uuid.
 *
 * This grouping lives in the browser and only stores backend document ids and
 * graph uuids. Deleting a subject, topic or graph never deletes documents or
 * backend graphs — those are orphaned but still there.
 *
 * Shape:
 *   { subjects: [{ id, name, open,
 *       topics: [{ id, name, open,
 *         graphs: [{ id, name, docIds: [], supabaseId?: uuid }] }] }],
 *     activeGraphId: string | null }        null = "All documents"
 */

const EMPTY = { subjects: [], activeGraphId: null }
const keyFor = (userKey) => `studygraph.library.v2:${userKey || 'anon'}`
const newId = (p) => `${p}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
const sameId = (a, b) => String(a) === String(b)

function read(key) {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return EMPTY
    const data = JSON.parse(raw)
    if (!data || !Array.isArray(data.subjects)) return EMPTY

    let activeGraphId = data.activeGraphId ? String(data.activeGraphId) : null
    const subjects = data.subjects.map((s) => ({
      id: String(s.id),
      name: String(s.name || 'Untitled subject'),
      open: s.open !== false,
      topics: (s.topics || []).map((t) => {
        let graphs = Array.isArray(t.graphs)
          ? t.graphs.map((g) => ({
            id: String(g.id),
            name: String(g.name || 'Untitled graph'),
            docIds: Array.isArray(g.docIds) ? g.docIds : [],
            supabaseId: g.supabaseId ? String(g.supabaseId) : undefined,
          }))
          : []
        // Migration: old shape had docIds on the topic itself
        if (!Array.isArray(t.graphs)) {
          const gid = `${t.id}_g1`
          graphs = [{ id: gid, name: 'Graph 1', docIds: Array.isArray(t.docIds) ? t.docIds : [] }]
          if (!activeGraphId && data.activeTopicId && String(data.activeTopicId) === String(t.id)) {
            activeGraphId = gid
          }
        }
        return { id: String(t.id), name: String(t.name || 'Untitled topic'), open: t.open !== false, graphs }
      }),
    }))
    return { subjects, activeGraphId }
  } catch {
    return EMPTY
  }
}

function write(key, value) {
  try { window.localStorage.setItem(key, JSON.stringify(value)) } catch { /* private mode etc. */ }
}

export function useLibrary(userKey) {
  const storageKey = keyFor(userKey)
  const [lib, setLib] = useState(() => read(storageKey))
  const loadedFor = useRef(storageKey)

  // The signed-in user can arrive after first render; reload for the new key.
  useEffect(() => {
    if (loadedFor.current === storageKey) return
    loadedFor.current = storageKey
    setLib(read(storageKey))
  }, [storageKey])

  useEffect(() => {
    if (loadedFor.current === storageKey) write(storageKey, lib)
  }, [lib, storageKey])

  // --- small immutable-update helpers ---
  const mapSubjects = (fn) => (l) => ({ ...l, subjects: l.subjects.map(fn) })
  const mapTopics = (fn) => mapSubjects((s) => ({ ...s, topics: s.topics.map(fn) }))
  const mapGraphs = (fn) => mapTopics((t) => ({ ...t, graphs: t.graphs.map(fn) }))
  const graphIdsOf = (subjects, pred) => subjects.flatMap((s) =>
    s.topics.flatMap((t) => (pred(s, t) ? t.graphs.map((g) => g.id) : [])))

  // --- subjects ---
  const addSubject = useCallback((name) => {
    const id = newId('sub')
    setLib((l) => ({ ...l, subjects: [...l.subjects, { id, name, open: true, topics: [] }] }))
    return id
  }, [])

  const renameSubject = useCallback((id, name) =>
    setLib(mapSubjects((s) => (s.id === id ? { ...s, name } : s))), [])

  const toggleSubject = useCallback((id, open) =>
    setLib(mapSubjects((s) => (s.id === id ? { ...s, open: open ?? !s.open } : s))), [])

  const deleteSubject = useCallback((id) => setLib((l) => {
    const gone = graphIdsOf(l.subjects, (s) => s.id === id)
    return {
      subjects: l.subjects.filter((s) => s.id !== id),
      activeGraphId: gone.includes(l.activeGraphId) ? null : l.activeGraphId,
    }
  }), [])

  // --- topics ---
  const addTopic = useCallback((subjectId, name) => {
    const id = newId('top')
    setLib(mapSubjects((s) => (s.id === subjectId
      ? { ...s, open: true, topics: [...s.topics, { id, name, open: true, graphs: [] }] }
      : s)))
    return id
  }, [])

  const renameTopic = useCallback((id, name) =>
    setLib(mapTopics((t) => (t.id === id ? { ...t, name } : t))), [])

  const toggleTopic = useCallback((id, open) =>
    setLib(mapTopics((t) => (t.id === id ? { ...t, open: open ?? !t.open } : t))), [])

  const deleteTopic = useCallback((id) => setLib((l) => {
    const gone = graphIdsOf(l.subjects, (_s, t) => t.id === id)
    return {
      subjects: l.subjects.map((s) => ({ ...s, topics: s.topics.filter((t) => t.id !== id) })),
      activeGraphId: gone.includes(l.activeGraphId) ? null : l.activeGraphId,
    }
  }), [])

  // --- graphs ---
  /**
   * Adds a graph to a topic locally. The graph is not created on Supabase
   * until the first document is uploaded to it. Returns the local graph id.
   */
  const addGraph = useCallback((topicId, name) => {
    const id = newId('gr')
    setLib((l) => ({
      ...mapTopics((t) => (t.id === topicId
        ? { ...t, open: true, graphs: [...t.graphs, { id, name, docIds: [] }] }
        : t))(l),
      activeGraphId: id,
    }))
    return id
  }, [])

  const renameGraph = useCallback((id, name) =>
    setLib(mapGraphs((g) => (g.id === id ? { ...g, name } : g))), [])

  const deleteGraph = useCallback((id) => setLib((l) => ({
    subjects: l.subjects.map((s) => ({
      ...s, topics: s.topics.map((t) => ({ ...t, graphs: t.graphs.filter((g) => g.id !== id) })),
    })),
    activeGraphId: l.activeGraphId === id ? null : l.activeGraphId,
  })), [])

  const setActiveGraph = useCallback((id) => setLib((l) => ({ ...l, activeGraphId: id || null })), [])

  /**
   * A document sits in at most one graph; graphId null takes it out of all.
   * If the graph has a supabaseId, document ids are backend uuids. If not,
   * they are whatever was passed in (used during upload before the graph exists).
   */
  const assignDocs = useCallback((docIds, graphId) => setLib(mapGraphs((g) => {
    const kept = g.docIds.filter((d) => !docIds.some((x) => sameId(x, d)))
    return g.id === graphId ? { ...g, docIds: [...kept, ...docIds] } : { ...g, docIds: kept }
  })), [])

  const findGraph = useCallback((graphId) => {
    for (const subject of lib.subjects) {
      for (const topic of subject.topics) {
        const graph = topic.graphs.find((g) => g.id === graphId)
        if (graph) return { subject, topic, graph }
      }
    }
    return null
  }, [lib])

  const findGraphBySupabaseId = useCallback((supabaseId) => {
    for (const subject of lib.subjects) {
      for (const topic of subject.topics) {
        const graph = topic.graphs.find((g) => g.supabaseId === String(supabaseId))
        if (graph) return { subject, topic, graph }
      }
    }
    return null
  }, [lib])

  const graphOfDoc = useCallback((docId) => {
    for (const s of lib.subjects) {
      for (const t of s.topics) {
        for (const g of t.graphs) if (g.docIds.some((d) => sameId(d, docId))) return g.id
      }
    }
    return null
  }, [lib])

  /**
   * Associates the local graph with its Supabase UUID after the graph is
   * created on the backend. The local id stays stable because activeGraphId,
   * queued uploads, and sidebar references use it.
   */
  const promoteGraphToSupabase = useCallback((localGraphId, supabaseId) => {
    setLib(mapGraphs((g) => {
      if (g.id === localGraphId) {
        return { ...g, supabaseId: String(supabaseId) }
      }
      return g
    }))
  }, [])

  const topicOfDoc = useCallback((docId) => {
    for (const s of lib.subjects) {
      for (const t of s.topics) {
        for (const g of t.graphs) {
          if (g.docIds.some((d) => sameId(d, docId))) return t.id
        }
      }
    }
    return null
  }, [lib])

  return {
    subjects: lib.subjects,
    activeGraphId: lib.activeGraphId,
    activeTopicId: null,
    addSubject, renameSubject, toggleSubject, deleteSubject,
    addTopic, renameTopic, toggleTopic, deleteTopic,
    addGraph, renameGraph, deleteGraph,
    setActiveGraph, assignDocs, findGraph, findGraphBySupabaseId, graphOfDoc, topicOfDoc,
    promoteGraphToSupabase,
  }
}
