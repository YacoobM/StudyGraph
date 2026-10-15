import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

// Exercise the real hook with controlled network promises and hook state.
const source = readFileSync(new URL('../src/lib/library.js', import.meta.url), 'utf8')
  .replace(/^import[\s\S]*?from '\.\.\/services\/api\.js'\r?\n/, '')
  .replace('export function useLibrary', 'function useLibrary')
const deferred = () => {
  let resolve, reject
  const promise = new Promise((a, b) => { resolve = a; reject = b })
  return { promise, resolve, reject }
}
const initial = () => ({
  subjects: [{ id: 'subject', name: 'Subject', topics: [
    { id: 'topic', name: 'Topic', graphs: [{ id: 'graph', name: 'Graph', supabaseId: 'remote', docIds: ['doc'] }] },
    { id: 'other-topic', name: 'Other', graphs: [{ id: 'other-graph', name: 'Other graph', supabaseId: 'other-remote', docIds: [] }] },
  ] }], activeGraphId: 'graph',
})

function harness() {
  let cursor = 0
  const slots = [], effects = [], requests = [], loads = []
  const cache = new Map(['a', 'b'].map(user => [`studygraph.library.v2:${user}`, JSON.stringify(initial())]))
  const remove = id => {
    const request = { id, ...deferred() }
    requests.push(request)
    return request.promise
  }
  const context = {
    window: { localStorage: { getItem: key => cache.get(key), setItem: (key, value) => cache.set(key, value) } },
    useState(init) {
      const i = cursor++
      if (!(i in slots)) slots[i] = typeof init === 'function' ? init() : init
      return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value }]
    },
    useRef(value) { const i = cursor++; return slots[i] ??= { current: value } },
    useCallback: fn => fn,
    useEffect(fn, deps) {
      const i = cursor++, previous = slots[i]
      if (!previous || deps.some((value, j) => value !== previous.deps[j])) {
        previous?.cleanup?.()
        slots[i] = { deps }
        effects.push(() => { slots[i].cleanup = fn() })
      }
    },
    listLibrary() { const load = deferred(); loads.push(load); return load.promise },
    deleteRemoteSubject: remove, deleteRemoteTopic: remove, deleteRemoteGraph: remove,
    updateRemoteSubject: async () => {}, updateRemoteTopic: async () => {}, updateRemoteGraph: async () => {},
  }
  vm.createContext(context)
  vm.runInContext(source + '\nthis.hook = useLibrary', context)
  return {
    requests, loads,
    render(user = 'a') {
      cursor = 0
      const result = context.hook(user)
      while (effects.length) effects.shift()()
      return result
    },
  }
}
const action = kind => `delete${kind[0].toUpperCase()}${kind.slice(1)}`

for (const kind of ['subject', 'topic', 'graph']) {
  test(`${kind}: disappears before backend completion and stays gone on success`, async () => {
    const h = harness()
    const operation = h.render()[action(kind)](kind)
    assert.equal(h.requests.length, 1)
    assert.equal(h.render().findGraph('graph'), null)
    assert.equal(h.render().activeGraphId, null)
    h.requests[0].resolve()
    await operation
    assert.equal(h.render().findGraph('graph'), null)
  })

  test(`${kind}: failure restores subtree and active selection`, async () => {
    const h = harness()
    const operation = h.render()[action(kind)](kind)
    assert.equal(h.render().findGraph('graph'), null)
    h.requests[0].reject(new Error('Storage unavailable'))
    await assert.rejects(operation, /Storage unavailable/)
    assert.equal(h.render().findGraph('graph').graph.docIds[0], 'doc')
    assert.equal(h.render().activeGraphId, 'graph')
  })

  test(`${kind}: duplicate calls issue only one DELETE`, async () => {
    const h = harness(), library = h.render()
    const operation = library[action(kind)](kind)
    await library[action(kind)](kind)
    assert.equal(h.requests.length, 1)
    h.requests[0].resolve()
    await operation
  })
}

test('rollback preserves unrelated edits and subsequent navigation', async () => {
  const h = harness()
  const operation = h.render().deleteTopic('topic')
  const library = h.render()
  library.renameTopic('other-topic', 'Edited while deleting')
  library.setActiveGraph('other-graph')
  h.requests[0].reject(new Error('failed'))
  await assert.rejects(operation)
  assert.equal(h.render().activeGraphId, 'other-graph')
  assert.equal(h.render().subjects[0].topics[1].name, 'Edited while deleting')
  assert.ok(h.render().findGraph('graph'))
})

test('failed parent deletion does not resurrect a successfully deleted child', async () => {
  const h = harness()
  const child = h.render().deleteGraph('graph')
  const parent = h.render().deleteSubject('subject')
  h.requests[0].resolve()
  await child
  h.requests[1].reject(new Error('failed'))
  await assert.rejects(parent)
  assert.equal(h.render().subjects.length, 1)
  assert.equal(h.render().findGraph('graph'), null)
})

test('late initial library response cannot undo a completed deletion', async () => {
  const h = harness()
  const operation = h.render().deleteSubject('subject')
  h.requests[0].resolve()
  await operation
  h.loads[0].resolve({ subjects: initial().subjects })
  await Promise.resolve()
  assert.equal(h.render().subjects.length, 0)
})

test('404 requests server reconciliation after restoring the cached item', async () => {
  const h = harness()
  const operation = h.render().deleteSubject('subject')
  h.requests[0].reject(Object.assign(new Error('Item not found.'), { response: { status: 404 } }))
  await assert.rejects(operation)
  h.render()
  assert.equal(h.loads.length, 2)
  h.loads[1].resolve({ subjects: [] })
  await Promise.resolve()
  assert.equal(h.render().subjects.length, 0)
})

test('completion from the previous account cannot modify the new account', async () => {
  const h = harness()
  const operation = h.render().deleteSubject('subject')
  h.render('b')
  assert.equal(h.render('b').subjects.length, 1)
  h.requests[0].resolve()
  await operation
  assert.equal(h.render('b').subjects.length, 1)
})
