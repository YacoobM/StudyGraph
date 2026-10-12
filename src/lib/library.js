import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createSubject as createRemoteSubject,
  createTopic as createRemoteTopic,
  listLibrary,
  updateSubject as updateRemoteSubject,
  updateTopic as updateRemoteTopic,
  updateGraph as updateRemoteGraph,
} from '../services/api.js'

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
  const skipWriteFor = useRef(null)

  // Supabase is the source of truth for the hierarchy. localStorage remains
  // only a cache for UI state such as expanded/collapsed rows.
  useEffect(() => {
    let cancelled = false
    if (!userKey) {
      setLib(EMPTY)
      return () => { cancelled = true }
    }
    listLibrary().then(({ subjects = [] }) => {
      if (cancelled) return
      const previous = read(storageKey)
      const previousSubjects = new Map(previous.subjects.map((s) => [String(s.id), s]))
      const nextSubjects = subjects.map((s) => {
        const oldSubject = previousSubjects.get(String(s.id))
        const previousTopics = new Map((oldSubject?.topics || []).map((t) => [String(t.id), t]))
        return {
          id: String(s.id),
          name: String(s.name || 'Untitled subject'),
          open: oldSubject?.open !== false,
          topics: (s.topics || []).map((t) => {
            const oldTopic = previousTopics.get(String(t.id))
            return {
              id: String(t.id),
              name: String(t.name || 'Untitled topic'),
              open: oldTopic?.open !== false,
              graphs: (t.graphs || []).map((g) => ({
                id: String(g.id),
                name: String(g.name || 'Untitled graph'),
                docIds: Array.isArray(g.doc_ids) ? g.doc_ids : [],
                supabaseId: String(g.id),
              })),
            }
          }),
        }
      })
      const activeGraphId = nextSubjects.some((s) => s.topics.some((t) => t.graphs.some((g) => g.id === previous.activeGraphId)))
        ? previous.activeGraphId
        : null
      setLib({ subjects: nextSubjects, activeGraphId })
    }).catch(() => {
      // Keep the cached hierarchy visible if the backend is temporarily down.
    })
    return () => { cancelled = true }
  }, [storageKey, userKey])

  // The signed-in user can arrive after first render; reload for the new key.
  useEffect(() => {
    if (loadedFor.current === storageKey) return
    loadedFor.current = storageKey
    // Do not persist the previous user's in-memory state under this new key.
    // The write effect runs for the same render as this reload effect and
    // otherwise can copy User A's library into User B's localStorage entry.
    skipWriteFor.current = storageKey
    setLib(read(storageKey))
  }, [storageKey])

  useEffect(() => {
    if (loadedFor.current !== storageKey) return
    if (skipWriteFor.current === storageKey) {
      skipWriteFor.current = null
      return
    }
    write(storageKey, lib)
  }, [lib, storageKey])

  // --- small immutable-update helpers ---
  const mapSubjects = (fn) => (l) => ({ ...l, subjects: l.subjects.map(fn) })
  const mapTopics = (fn) => mapSubjects((s) => ({ ...s, topics: s.topics.map(fn) }))
  const mapGraphs = (fn) => mapTopics((t) => ({ ...t, graphs: t.graphs.map(fn) }))
  const graphIdsOf = (subjects, pred) => subjects.flatMap((s) =>
    s.topics.flatMap((t) => (pred(s, t) ? t.graphs.map((g) => g.id) : [])))

  // --- subjects ---
  const addSubject = useCallback(async (name) => {
    const id = newId('sub')
    setLib((l) => ({ ...l, subjects: [...l.subjects, { id, name, open: true, topics: [] }] }))
    try {
      const { subject } = await createRemoteSubject(name)
      setLib((l) => ({
        ...l,
        subjects: l.subjects.map((s) => s.id === id ? { ...s, id: String(subject.id) } : s),
      }))
      return String(subject.id)
    } catch {
      return id
    }
  }, [])

  const renameSubject = useCallback((id, name) => {
    setLib(mapSubjects((s) => (s.id === id ? { ...s, name } : s)))
    if (!String(id).startsWith('sub_')) updateRemoteSubject(id, name).catch(() => {})
  }, [])

  const toggleSubject = useCallback((id, open) =>
    setLib(mapSubjects((s) => (s.id === id ? { ...s, open: open ?? !s.open } : s))), [])

  const deleteSubject = useCallback((id) => {
    setLib((l) => {
    const gone = graphIdsOf(l.subjects, (s) => s.id === id)
    return {
      subjects: l.subjects.filter((s) => s.id !== id),
      activeGraphId: gone.includes(l.activeGraphId) ? null : l.activeGraphId,
    }
    })
  }, [])

  // --- topics ---
  const addTopic = useCallback(async (subjectId, name) => {
    const id = newId('top')
    setLib(mapSubjects((s) => (s.id === subjectId
      ? { ...s, open: true, topics: [...s.topics, { id, name, open: true, graphs: [] }] }
      : s)))
    if (!String(subjectId).startsWith('sub_')) {
      try {
        const { topic } = await createRemoteTopic(subjectId, name)
        setLib(mapSubjects((s) => ({
          ...s,
          topics: s.topics.map((t) => t.id === id ? { ...t, id: String(topic.id) } : t),
        })))
        return String(topic.id)
      } catch {
        return id
      }
    }
    return id
  }, [])

  const renameTopic = useCallback((id, name) => {
    setLib(mapTopics((t) => (t.id === id ? { ...t, name } : t)))
    if (!String(id).startsWith('top_')) updateRemoteTopic(id, name).catch(() => {})
  }, [])

  const toggleTopic = useCallback((id, open) =>
    setLib(mapTopics((t) => (t.id === id ? { ...t, open: open ?? !t.open } : t))), [])

  const deleteTopic = useCallback((id) => {
    setLib((l) => {
    const gone = graphIdsOf(l.subjects, (_s, t) => t.id === id)
    return {
      subjects: l.subjects.map((s) => ({ ...s, topics: s.topics.filter((t) => t.id !== id) })),
      activeGraphId: gone.includes(l.activeGraphId) ? null : l.activeGraphId,
    }
    })
  }, [])

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

  const renameGraph = useCallback((id, name) => {
    const supabaseId = lib.subjects
      .flatMap((subject) => subject.topics.flatMap((topic) => topic.graphs))
      .find((graph) => graph.id === id)?.supabaseId
    setLib(mapGraphs((g) => (g.id === id ? { ...g, name } : g)))
    if (supabaseId) updateRemoteGraph(supabaseId, name).catch(() => {})
  }, [lib])

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

  /** Replaces a graph's document ids with the list the backend just returned. */
  const setGraphDocs = useCallback((supabaseId, docIds) => {
    setLib((l) => {
      let changed = false
      const next = mapGraphs((g) => {
        if (g.supabaseId !== String(supabaseId)) return g
        const same = g.docIds.length === docIds.length && g.docIds.every((id, i) => sameId(id, docIds[i]))
        if (same) return g
        changed = true
        return { ...g, docIds: [...docIds] }
      })(l)
      return changed ? next : l
    })
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
    promoteGraphToSupabase, setGraphDocs,
  }
}
