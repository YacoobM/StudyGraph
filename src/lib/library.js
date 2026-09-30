import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Subject → Topic organisation for the workspace sidebar.
 *
 * The backend has no notion of subjects or topics — a document row is just a
 * file, a status and a week, and /api/graph returns one combined graph. So the
 * grouping lives in the browser, keyed per signed-in user, and only ever
 * stores backend document ids. Nothing here is sent to the server, and
 * deleting a subject or topic never deletes a document.
 *
 * Shape:
 *   { subjects: [{ id, name, open, topics: [{ id, name, docIds: [] }] }],
 *     activeTopicId: string | null }        null = "All documents"
 *
 * Trade-off: because it is browser-local, the grouping does not follow the
 * user to another browser or device. Moving it server-side later only means
 * swapping read()/write() for API calls — the hook's surface can stay.
 */

const EMPTY = { subjects: [], activeTopicId: null }
const keyFor = (userKey) => `studygraph.library.v1:${userKey || 'anon'}`
const newId = (p) => `${p}_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`
const sameId = (a, b) => String(a) === String(b)

function read(key) {
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return EMPTY
    const data = JSON.parse(raw)
    if (!data || !Array.isArray(data.subjects)) return EMPTY
    return {
      subjects: data.subjects.map((s) => ({
        id: String(s.id), name: String(s.name || 'Untitled subject'), open: s.open !== false,
        topics: (s.topics || []).map((t) => ({
          id: String(t.id), name: String(t.name || 'Untitled topic'),
          docIds: Array.isArray(t.docIds) ? t.docIds : [],
        })),
      })),
      activeTopicId: data.activeTopicId ? String(data.activeTopicId) : null,
    }
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

  const mapSubject = (id, fn) => (l) => ({
    ...l, subjects: l.subjects.map((s) => (s.id === id ? fn(s) : s)),
  })
  const mapTopic = (id, fn) => (l) => ({
    ...l,
    subjects: l.subjects.map((s) => ({
      ...s, topics: s.topics.map((t) => (t.id === id ? fn(t) : t)),
    })),
  })

  const addSubject = useCallback((name) => {
    const id = newId('sub')
    setLib((l) => ({ ...l, subjects: [...l.subjects, { id, name, open: true, topics: [] }] }))
    return id
  }, [])

  const renameSubject = useCallback((id, name) => setLib(mapSubject(id, (s) => ({ ...s, name }))), [])
  const toggleSubject = useCallback((id, open) =>
    setLib(mapSubject(id, (s) => ({ ...s, open: open ?? !s.open }))), [])

  const deleteSubject = useCallback((id) => setLib((l) => {
    const gone = l.subjects.find((s) => s.id === id)
    const activeGone = gone?.topics.some((t) => t.id === l.activeTopicId)
    return {
      subjects: l.subjects.filter((s) => s.id !== id),
      activeTopicId: activeGone ? null : l.activeTopicId,
    }
  }), [])

  const addTopic = useCallback((subjectId, name) => {
    const id = newId('top')
    setLib((l) => ({
      ...mapSubject(subjectId, (s) => ({
        ...s, open: true, topics: [...s.topics, { id, name, docIds: [] }],
      }))(l),
      activeTopicId: id,
    }))
    return id
  }, [])

  const renameTopic = useCallback((id, name) => setLib(mapTopic(id, (t) => ({ ...t, name }))), [])

  const deleteTopic = useCallback((id) => setLib((l) => ({
    subjects: l.subjects.map((s) => ({ ...s, topics: s.topics.filter((t) => t.id !== id) })),
    activeTopicId: l.activeTopicId === id ? null : l.activeTopicId,
  })), [])

  const setActiveTopic = useCallback((id) => setLib((l) => ({ ...l, activeTopicId: id || null })), [])

  /** A document sits in at most one topic; topicId null takes it out of all. */
  const assignDocs = useCallback((docIds, topicId) => setLib((l) => ({
    ...l,
    subjects: l.subjects.map((s) => ({
      ...s,
      topics: s.topics.map((t) => {
        const kept = t.docIds.filter((d) => !docIds.some((x) => sameId(x, d)))
        return t.id === topicId ? { ...t, docIds: [...kept, ...docIds] } : { ...t, docIds: kept }
      }),
    })),
  })), [])

  const findTopic = useCallback((topicId) => {
    for (const s of lib.subjects) {
      const t = s.topics.find((x) => x.id === topicId)
      if (t) return { subject: s, topic: t }
    }
    return null
  }, [lib])

  const topicOfDoc = useCallback((docId) => {
    for (const s of lib.subjects) {
      for (const t of s.topics) if (t.docIds.some((d) => sameId(d, docId))) return t.id
    }
    return null
  }, [lib])

  return {
    subjects: lib.subjects,
    activeTopicId: lib.activeTopicId,
    addSubject, renameSubject, toggleSubject, deleteSubject,
    addTopic, renameTopic, deleteTopic,
    setActiveTopic, assignDocs, findTopic, topicOfDoc,
  }
}
