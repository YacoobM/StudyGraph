// Fake StudyGraph API for the UI smoke test. Responses follow the shapes the
// FastAPI routes return, so the real frontend code runs unchanged.
export const API = 'http://localhost:8000'
export const TAB = 'tab-test-1'
export const session = {
  access_token: 't', refresh_token: 'r', token_type: 'bearer', expires_in: 86400,
  expires_at: Math.floor(Date.now() / 1000) + 86400,
  user: { id: 'user-test-1', email: 'lee@example.com', aud: 'authenticated', user_metadata: { full_name: 'Lee Jung Soo' } },
}

const iso = (minsAgo) => new Date(Date.now() - minsAgo * 60000).toISOString()

const CONCEPTS = [
  ['c1', 'Threads', 'Independent paths of execution that share one process address space.'],
  ['c2', 'Mutex', 'A lock that lets only one thread enter a critical section at a time.'],
  ['c3', 'Race Condition', 'A bug where the result depends on the timing of threads.'],
  ['c4', 'Critical Section', 'Code that touches shared data and must not run concurrently.'],
  ['c5', 'Deadlock', 'Threads wait on each other forever.'],
  ['c6', 'Semaphore', ''],
  ['c7', 'Pthreads', 'The POSIX threads API.'],
  ['c8', 'Condition Variable', 'Lets a thread wait until another thread signals a change.'],
  ['c9', 'Thread Pool', ''],
  ['c10', 'Context Switch', ''],
  ['c11', 'Shared Memory', ''],
  ['c12', 'Atomic Operation', ''],
  ['c13', 'False Sharing', ''],
  ['c14', 'Barrier', ''],
  ['c15', 'Message Passing', 'Processes exchange data by sending messages.'],
  ['c16', 'MPI_Send', ''],
  ['c17', 'Spinlock', ''],
]
const REL = [
  ['c1', 'c2', 'uses'], ['c1', 'c3', 'causes'], ['c2', 'c4', 'protects'], ['c3', 'c4', 'occurs_in'],
  ['c2', 'c5', 'can_cause'], ['c1', 'c6', 'uses'], ['c1', 'c7', 'implemented_by'], ['c1', 'c8', 'uses'],
  ['c1', 'c9', 'part_of'], ['c1', 'c10', 'requires'], ['c1', 'c11', 'uses'], ['c1', 'c12', 'uses'],
  ['c1', 'c13', 'causes'], ['c1', 'c14', 'uses'], ['c1', 'c17', 'uses'], ['c15', 'c16', 'implemented_by'],
  ['c1', 'c15', 'contrasts_with'],
]
// Names the backend merged into a concept.
const ALIASES = { c2: ['Mutual exclusion lock'] }
// [concept, document, page, week, excerpt]
const PROV = [
  ['c1', 'd1', 2, 1, 'A thread is the smallest unit of execution scheduled by the operating system.'],
  ['c1', 'd1', 5, 1, 'Threads in the same process share heap memory and file handles.'],
  ['c1', 'd2', 3, 2, 'MPI processes, unlike threads, do not share an address space.'],
  ['c2', 'd1', 7, 1, 'pthread_mutex_lock blocks until the mutex becomes available.'],
  ['c3', 'd1', 8, 1, 'Two threads incrementing a counter without a lock is a classic race.'],
  ['c4', 'd1', 8, 1, ''],
  ['c5', 'd1', 11, 1, 'Deadlock needs mutual exclusion, hold and wait, no preemption and circular wait.'],
  ['c6', null, null, 1, ''],
  ['c7', 'd1', 3, 1, ''],
  ['c8', 'd1', 12, 1, ''],
  ['c9', 'd9', 4, 1, 'Thread pools reuse workers instead of creating threads per task.'],
  ['c15', 'd2', 1, 2, 'Message passing moves data explicitly between processes.'],
  ['c16', 'd2', 6, 2, 'MPI_Send(buf, count, datatype, dest, tag, comm)'],
]
const DOCS = {
  d1: { id: 'd1', graph_id: 'g1', filename: 'FIT3143-Topic-2-2024.pdf', size_bytes: 920000, mime: 'application/pdf', status: 'ready', week: 'Week 1', concepts: 12, relationships: 14, uploaded_at: iso(300) },
  d2: { id: 'd2', graph_id: 'g1', filename: 'Lab2-MPI.pdf', size_bytes: 410000, mime: 'application/pdf', status: 'ready', week: 'Week 2', concepts: 3, relationships: 2, uploaded_at: iso(60) },
  d3: { id: 'd3', graph_id: 'g2', filename: 'OpenMP-Intro.pptx', size_bytes: 210000, mime: 'application/vnd.ms-powerpoint', status: 'ready', week: 'Week 1', concepts: 0, relationships: 0, uploaded_at: iso(30) },
}

function snapshot(versionNumber) {
  const keep = versionNumber === 1 ? new Set(PROV.filter((p) => p[3] === 1).map((p) => p[0])) : null
  const concepts = CONCEPTS.filter(([id]) => !keep || keep.has(id) || ['c10', 'c11', 'c12', 'c13', 'c14', 'c17'].includes(id))
    .map(([id, name, description]) => ({ id, name, normalized_name: name.toLowerCase(), description, aliases: ALIASES[id] || [], status: id === 'c13' ? 'needs_regeneration' : 'active' }))
  const ids = new Set(concepts.map((c) => c.id))
  const relationships = REL.filter(([s, t]) => ids.has(s) && ids.has(t))
    .map(([s, t, r], i) => ({ id: `r${i}`, source_concept_id: s, target_concept_id: t, relationship_type: r }))
  const concept_provenance = PROV.filter(([c]) => ids.has(c)).map(([c, d, page, week, excerpt], i) => ({
    id: `p${i}`, graph_version_id: `v${versionNumber}`, concept_id: c, document_id: d,
    document_filename: d ? (DOCS[d]?.filename || 'Old-Notes.pdf') : 'Unattributed upload',
    chunk_id: d ? `ch${i}` : null, source_name: '', source_excerpt: excerpt, page_start: page, page_end: page, source_week: week,
  }))
  return { version: { id: `v${versionNumber}`, version_number: versionNumber }, concepts, relationships, concept_provenance, relationship_provenance: [] }
}

export function makeMock(opts = {}) {
  const m = {
    calls: [],
    quizGenerated: false,
    quizDelay: opts.quizDelay ?? 2500,
    quizFail: opts.quizFail ?? false,
    noteDays: {},
    sectionDone: {},
    planDelay: opts.planDelay ?? 600,
  }
  m.notes = [
    {
      id: 'n1', graph_id: 'g1', graph_version_id: 'v2', title: 'Revision notes', kind: 'plan', days: 3, status: 'current',
      document_ids: ['d1', 'd2'], documents: ['FIT3143-Topic-2-2024.pdf', 'Lab2-MPI.pdf'], created_at: iso(90), updated_at: iso(90),
      // Stored before the fix: no day on any section.
      sections: [
        { id: 's1', section_order: 0, day: null, title: 'Threads and shared memory', concepts: ['Threads', 'Shared Memory', 'Pthreads'], relations: [{ from: 'Threads', to: 'Pthreads', relation: 'implemented_by', summary: 'Pthreads is the POSIX API for creating threads.' }], sources: ['FIT3143-Topic-2-2024.pdf'], must_cover: [], body: null, done: false },
        { id: 's2', section_order: 1, day: null, title: 'Locks and race conditions', concepts: ['Mutex', 'Race Condition', 'Critical Section'], relations: [], sources: ['FIT3143-Topic-2-2024.pdf'], must_cover: [], body: null, done: false },
        { id: 's3', section_order: 2, day: null, title: 'Message passing with MPI', concepts: ['Message Passing', 'MPI_Send'], relations: [], sources: ['Lab2-MPI.pdf'], must_cover: [], body: null, done: false },
      ],
    },
  ]
  return m
}

const BODY = {
  lead: [{ text: 'A ' }, { text: 'thread', bold: true }, { text: ' is an independent path of execution inside a process.' }],
  body: [
    { heading: 'What threads share', parts: [{ text: 'Threads in one process share the heap, global variables and open files, but each has its own ' }, { text: 'stack and registers', key: true }, { text: '.' }] },
    { heading: 'Why that matters', parts: [{ text: 'Shared memory makes communication cheap, but any shared write needs synchronisation or a ' }, { text: 'race condition', bold: true, key: true }, { text: ' can occur.' }] },
  ],
  example: { title: 'Example', paragraphs: [[{ text: 'Two threads incrementing one counter 1,000 times each can finish with a value below 2,000.' }]] },
  table: { columns: ['Threads', 'Processes'], rows: [['Share memory', 'Separate memory'], ['Cheap to create', 'More expensive']] },
  terms: [{ term: 'Pthreads', definition: 'The POSIX threads API.' }],
  quiz: [
    { q: 'What does each thread have its own copy of?', o: ['Heap', 'Stack', 'Global variables', 'Open files'], a: 1, why: 'Each thread has its own stack.', d: 'easy' },
    { q: 'What can unsynchronised shared writes cause?', o: ['Deadlock only', 'A race condition', 'A context switch', 'Nothing'], a: 1, why: 'Timing decides the result.', d: 'medium' },
  ],
  quiz_id: 'nq-1', quiz_pass_mark: 2,
}

export async function mockRoutes(context, m) {
  await context.route('https://example.supabase.co/**', (r) => r.fulfill({ status: 200, json: {} }))
  await context.route('https://example.com/**', (r) => r.fulfill({ status: 200, contentType: 'text/plain', body: 'file' }))
  await context.route(`${API}/**`, async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const p = url.pathname
    const method = req.method()
    const H = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
    const json = (body, status = 200) => route.fulfill({ status, json: body, headers: H })
    if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: H })
    m.calls.push(`${method} ${p}`)
    const wait = (ms) => new Promise((r) => setTimeout(r, ms))

    if (p === '/api/library') {
      return json({ subjects: [{ id: 's1', name: 'FIT3143 Parallel Computing', topics: [
        { id: 't1', name: 'Week 2 Threads', graphs: [
          { id: 'g1', name: 'Threads and Synchronisation', doc_ids: ['d1', 'd2'] },
          { id: 'g2', name: 'OpenMP', doc_ids: ['d3'] },
        ] },
        { id: 't2', name: 'Week 3 MPI', graphs: [{ id: 'g3', name: 'Empty graph', doc_ids: [] }] },
      ] }] })
    }
    let mm
    if ((mm = p.match(/^\/api\/graphs\/(\w+)\/documents$/)) && method === 'GET') {
      await wait(150)
      return json({ documents: Object.values(DOCS).filter((d) => d.graph_id === mm[1]) })
    }
    if ((mm = p.match(/^\/api\/graphs\/(\w+)\/versions$/)) && method === 'GET') {
      await wait(150)
      if (mm[1] === 'g1') return json({ versions: [{ id: 'v2', version_number: 2, created_at: iso(60), is_sealed: true }, { id: 'v1', version_number: 1, created_at: iso(300), is_sealed: true }] })
      return json({ versions: [] })
    }
    if ((mm = p.match(/^\/api\/graphs\/(\w+)\/versions\/(\w+)$/))) {
      await wait(200)
      return json(snapshot(mm[2] === 'v1' ? 1 : 2))
    }
    if (p.match(/^\/api\/graphs\/\w+\/support$/)) return json({ concepts: {}, relationships: {} })
    if ((mm = p.match(/^\/api\/graphs\/\w+\/concepts\/([^/]+)\/evolution$/))) {
      await wait(400)
      const name = decodeURIComponent(mm[1])
      return json({ comparison: `${name} widens from shared memory threads to a contrast with message passing.`,
        weeks_data: [{ week: 'Week 1', summary: `${name} introduced with Pthreads.` }, { week: 'Week 2', summary: `${name} contrasted with MPI processes.` }],
        consecutive_comparisons: [{ from_week: 'Week 1', to_week: 'Week 2', comparison: 'Adds the message passing contrast.' }] })
    }
    if (p.match(/^\/api\/graphs\/\w+\/documents\/\w+\/file$/)) return json({ url: 'https://example.com/file.pdf' })

    // notes
    if ((mm = p.match(/^\/api\/graphs\/(\w+)\/notes$/)) && method === 'GET') {
      await wait(150)
      return json({ notes: mm[1] === 'g1' ? m.notes.map((n) => ({ ...n, total: n.sections.length, sections: n.sections.map((s) => ({ ...s, done: !!m.sectionDone[s.id] })) })) : [] })
    }
    if ((mm = p.match(/^\/api\/graphs\/\w+\/notes\/(\w+)$/)) && method === 'GET') {
      const n = m.notes.find((x) => x.id === mm[1])
      return n ? json({ note: { ...n, total: n.sections.length } }) : json({ detail: 'Note not found' }, 404)
    }
    if (p.match(/^\/api\/graphs\/\w+\/notes\/plan$/)) {
      await wait(m.planDelay)
      const body = JSON.parse(req.postData() || '{}')
      const days = body.days || null
      const titles = ['Threads and shared memory', 'Locks and race conditions', 'Deadlock and its conditions', 'Message passing with MPI']
      const capped = days ? Math.min(days, titles.length) : null
      const sections = titles.map((t, i) => ({ id: `ns${i}`, section_order: i, day: capped ? Math.min(capped, Math.floor((i * capped) / titles.length) + 1) : null, title: t, concepts: ['Threads'], relations: [], sources: ['FIT3143-Topic-2-2024.pdf'], must_cover: [], body: null, done: false }))
      const note = { id: 'n2', graph_id: 'g1', graph_version_id: 'v2', title: 'Revision notes', kind: days ? 'plan' : 'full', days: capped, status: 'current', document_ids: body.document_ids, documents: ['FIT3143-Topic-2-2024.pdf'], created_at: iso(0), updated_at: iso(0), sections }
      m.notes = [note, ...m.notes.filter((n) => n.id !== 'n2')]
      return json({ note: { ...note, total: sections.length } })
    }
    if ((mm = p.match(/^\/api\/graphs\/\w+\/notes\/\w+\/sections\/(\w+)\/write$/))) {
      await wait(900)
      return json({ section: { ...m.notes.flatMap((n) => n.sections).find((s) => s.id === mm[1]), body: BODY }, generated: true, retry_after: null })
    }
    if ((mm = p.match(/^\/api\/graphs\/\w+\/notes\/\w+\/sections\/(\w+)$/)) && method === 'PATCH') {
      await wait(m.completionDelay ?? 0)
      if (m.completionFail) return json({ detail: 'Progress save failed' }, 503)
      const body = JSON.parse(req.postData() || '{}')
      m.sectionDone[mm[1]] = !!body.done
      return json({ section: { id: mm[1], done: !!body.done } })
    }
    if (p.match(/\/notes\/sections\/\w+\/quiz-progress$/)) return json({ result: { correct: 2, total: 2, status: 'completed' }, passed: true })

    // quiz
    if ((mm = p.match(/^\/api\/graphs\/(\w+)\/quiz$/))) {
      await wait(150)
      // Same reply as the backend for a graph with no sealed version.
      if (mm[1] !== 'g1') return json({ topics: [], generated: false, reason: 'No documents have been processed yet.' })
      if (!m.quizGenerated) {
        return json({ topics: [{ id: 'pending-v2', day: 2, name: 'Graph Version 2', desc: 'No quiz has been generated for this graph version yet.', status: 'not_generated', stale: false, latest_version: 2, questions: [] }], generated: false, reason: 'No quiz has been generated for this graph version yet.' })
      }
      return json({ topics: [{ id: 'qs1', day: 2, name: 'Graph Version 2', desc: 'Questions from graph version 2.', status: 'ready', stale: false, latest_version: 2,
        questions: [
          { id: 'q1', q: 'What does a mutex protect?', o: ['A thread pool', 'A critical section', 'A message', 'A barrier'], a: 1, why: 'Mutexes guard critical sections.', d: 'easy' },
          { id: 'q2', q: 'Which condition is needed for deadlock?', o: ['Preemption', 'Circular wait', 'Atomic operations', 'Message passing'], a: 1, why: 'Circular wait is one of the four conditions.', d: 'medium' },
          { id: 'q3', q: 'What does MPI_Send do?', o: ['Locks memory', 'Sends a message', 'Creates a thread', 'Waits on a barrier'], a: 1, why: 'It sends a message to another process.', d: 'hard' },
        ] }], generated: true, reason: null, resume: null, result: null })
    }
    if (p.match(/^\/api\/graphs\/\w+\/quiz\/regenerate$/)) {
      await wait(m.quizDelay)
      if (m.quizFail) return json({ detail: 'The quiz service is busy. Try again in a minute.' }, 503)
      m.quizGenerated = true
      return json({ topics: [{ id: 'qs1', status: 'ready', questions: [{}] }], generated: true, reason: null })
    }
    if (p.match(/^\/api\/graphs\/\w+\/quiz\/results$/)) return json({ result: { correct: 1, total: 3, status: 'in_progress', current_index: 1 }, passed: false })

    if (p.match(/^\/api\/graphs\/\w+\/query$/)) {
      return json({ answer: 'A mutex lets only one thread enter a critical section at a time.', concepts_mentioned: ['Mutex', 'Critical Section'], sources: [{ fileName: 'FIT3143-Topic-2-2024.pdf', page_number: 7, snippet: 'pthread_mutex_lock blocks until the mutex is free' }] })
    }
    return json({})
  })
}

export async function seed(context, { activeGraphId = 'g1' } = {}) {
  await context.addInitScript(([s, tab, active]) => {
    sessionStorage.setItem('studygraph.tab.id', tab)
    sessionStorage.setItem(`studygraph.auth.${tab}`, JSON.stringify(s))
    if (!sessionStorage.getItem('seeded')) {
      const g = (id, name, docIds) => ({ id, name, docIds, supabaseId: id })
      localStorage.setItem('studygraph.library.v2:user-test-1', JSON.stringify({ activeGraphId: active, subjects: [{ id: 's1', name: 'FIT3143 Parallel Computing', open: true, topics: [
        { id: 't1', name: 'Week 2 Threads', open: true, graphs: [g('g1', 'Threads and Synchronisation', ['d1', 'd2']), g('g2', 'OpenMP', ['d3'])] },
        { id: 't2', name: 'Week 3 MPI', open: true, graphs: [g('g3', 'Empty graph', [])] }] }] }))
      sessionStorage.setItem('seeded', '1')
    }
  }, [session, TAB, activeGraphId])
}
