/**
 * Headless check of the production concept graph (src/components/ConceptGraph.jsx).
 *
 * Drives the real component in Chromium against a 48-concept graph shaped
 * exactly like the one Workspace builds from GET /api/graph, and asserts the
 * readability behaviour: node hierarchy, zoom-gated labels, hidden relationship
 * labels, focus mode, search, toolbar, isolated-concept handling and resize.
 *
 *   node verify_graph.mjs <baseUrl>
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'http://localhost:4174/.harness/index.html'
const EXEC = process.env.CHROMIUM_PATH || undefined

let pass = 0
let fail = 0
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  PASS  ${name}`) }
  else { fail += 1; console.log(`  FAIL  ${name}${extra ? ` — ${extra}` : ''}`) }
}

const browser = await chromium.launch({ executablePath: EXEC })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

const errors = []
const isFontNoise = (t) => /ERR_TUNNEL_CONNECTION_FAILED|fonts\.g(oogleapis|static)\.com|favicon/.test(t)
page.on('console', (m) => {
  const where = m.location?.()?.url || ''
  if (m.type() === 'error' && !isFontNoise(m.text()) && !isFontNoise(where)) errors.push(`${m.text()} @ ${where}`)
})
page.on('pageerror', (e) => { if (!isFontNoise(String(e))) errors.push(String(e)) })

await page.goto(BASE, { waitUntil: 'networkidle' })
await page.waitForSelector('.gx-node', { timeout: 8000 })
await page.waitForTimeout(3200)          // let the layout settle and auto-fit run

const counts = () => page.evaluate(() => ({
  nodes: document.querySelectorAll('.gx-node').length,
  edges: document.querySelectorAll('.gx-edge').length,
  visibleLabels: [...document.querySelectorAll('.gx-node text')]
    .filter((t) => getComputedStyle(t).display !== 'none').length,
  visibleRelLabels: [...document.querySelectorAll('.gx-edge text')]
    .filter((t) => getComputedStyle(t).display !== 'none').length,
  sims: document.querySelectorAll('svg#wsGraphSvg').length,
}))

console.log('\n— renders the backend graph —')
const base = await counts()
const expected = await page.evaluate(() => window.__HARNESS__)
ok('all 48 concepts render', base.nodes === expected.nodes, `${base.nodes} of ${expected.nodes}`)
ok('all relationships render', base.edges === expected.edges, `${base.edges} of ${expected.edges}`)
ok('exactly one graph canvas exists', base.sims === 1, `${base.sims}`)

console.log('\n— node hierarchy —')
const radii = await page.evaluate(() =>
  [...document.querySelectorAll('.gx-node circle')].map((c) => +c.getAttribute('r')))
const rMin = Math.min(...radii)
const rMax = Math.max(...radii)
ok('node size varies with connectedness', rMax - rMin > 3, `r ${rMin}–${rMax}`)
ok('no giant node circles', rMax <= 19, `max r ${rMax}`)

console.log('\n— label clutter —')
ok('not every concept label is drawn at default zoom',
  base.visibleLabels < base.nodes, `${base.visibleLabels} of ${base.nodes} labels`)
ok('the most connected concepts are still labelled',
  base.visibleLabels >= Math.floor(base.nodes * 0.2),
  `${base.visibleLabels} labels`)
ok('no relationship labels are shown by default',
  base.visibleRelLabels === 0, `${base.visibleRelLabels} shown`)

const labelOverlaps = await page.evaluate(() => {
  const boxes = [...document.querySelectorAll('.gx-node text')]
    .filter((t) => getComputedStyle(t).display !== 'none')
    .map((t) => ({
      name: [...t.querySelectorAll('tspan')].map((s) => s.textContent).join(' '),
      r: t.getBoundingClientRect(),
    }))
  const hits = []
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i].r, b = boxes[j].r
      if (Math.min(a.right, b.right) - Math.max(a.left, b.left) > 3 &&
          Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 3) {
        hits.push(`${boxes[i].name} / ${boxes[j].name}`)
      }
    }
  }
  return hits
})
ok('visible labels do not overlap each other',
  labelOverlaps.length === 0, labelOverlaps.slice(0, 3).join('; '))

console.log('\n— zoom reveals more labels —')
await page.getByRole('button', { name: 'Zoom in' }).click()
await page.getByRole('button', { name: 'Zoom in' }).click()
await page.waitForTimeout(700)
const zoomed = await counts()
ok('zooming in reveals lower-importance labels',
  zoomed.visibleLabels > base.visibleLabels,
  `${base.visibleLabels} → ${zoomed.visibleLabels}`)

await page.getByRole('button', { name: 'Fit to screen' }).click()
await page.waitForTimeout(700)
const fitted = await counts()
ok('Fit brings the label count back down',
  fitted.visibleLabels <= zoomed.visibleLabels,
  `${zoomed.visibleLabels} → ${fitted.visibleLabels}`)
const inFrame = await page.evaluate(() => {
  const box = document.querySelector('#wsGraphSvg').getBoundingClientRect()
  return [...document.querySelectorAll('.gx-node circle')].every((c) => {
    const r = c.getBoundingClientRect()
    return r.left >= box.left - 2 && r.right <= box.right + 2 &&
           r.top >= box.top - 2 && r.bottom <= box.bottom + 2
  })
})
ok('Fit puts every concept inside the canvas', inFrame)

console.log('\n— focus mode —')
const pickLabelled = async () => page.evaluate(() => {
  const g = [...document.querySelectorAll('.gx-node')]
    .sort((a, b) => +b.querySelector('circle').getAttribute('r') -
                    +a.querySelector('circle').getAttribute('r'))[0]
  const r = g.getBoundingClientRect()
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 }
})
const hub = await pickLabelled()
await page.mouse.click(hub.x, hub.y)
await page.waitForTimeout(600)

const focus = await page.evaluate(() => {
  const nodes = [...document.querySelectorAll('.gx-node')]
    .map((g) => +getComputedStyle(g).opacity)
  const edges = [...document.querySelectorAll('.gx-edge')]
    .map((g) => +getComputedStyle(g).opacity)
  return {
    bright: nodes.filter((o) => o > 0.9).length,
    dim: nodes.filter((o) => o < 0.3).length,
    edgeBright: edges.filter((o) => o > 0.9).length,
    edgeDim: edges.filter((o) => o < 0.3).length,
    rel: [...document.querySelectorAll('.gx-edge text')]
      .filter((t) => getComputedStyle(t).display !== 'none').length,
    selected: !!window.__SELECTED__,
  }
})
ok('clicking a concept selects it', focus.selected)
ok('selected concept and its neighbours stay bright',
  focus.bright > 1 && focus.bright < base.nodes, `${focus.bright} bright`)
ok('unrelated concepts are dimmed', focus.dim > 0, `${focus.dim} dimmed`)
ok('unrelated relationships are dimmed', focus.edgeDim > 0, `${focus.edgeDim} dimmed`)
ok('relationship labels appear for the selected concept',
  focus.rel > 0 && focus.rel < base.edges, `${focus.rel} of ${base.edges}`)

await page.mouse.click(120, 700)          // empty canvas
await page.waitForTimeout(500)
const cleared = await page.evaluate(() => ({
  selected: !!window.__SELECTED__,
  dim: [...document.querySelectorAll('.gx-node')]
    .map((g) => +getComputedStyle(g).opacity).filter((o) => o < 0.9).length,
  rel: [...document.querySelectorAll('.gx-edge text')]
    .filter((t) => getComputedStyle(t).display !== 'none').length,
}))
ok('clicking empty canvas clears the selection', !cleared.selected)
ok('dimming is undone on reset', cleared.dim === 0, `${cleared.dim} still dim`)
ok('relationship labels hide again', cleared.rel === 0, `${cleared.rel} shown`)

console.log('\n— show relationships toggle —')
await page.getByRole('button', { name: 'Relationships' }).click()
await page.waitForTimeout(400)
let rel = await page.evaluate(() => [...document.querySelectorAll('.gx-edge text')]
  .filter((t) => getComputedStyle(t).display !== 'none').length)
ok('the toggle shows every relationship label', rel === base.edges, `${rel} of ${base.edges}`)
ok('the toggle reports its state',
  (await page.getByRole('button', { name: 'Relationships' }).getAttribute('aria-pressed')) === 'true')
await page.getByRole('button', { name: 'Relationships' }).click()
await page.waitForTimeout(400)
rel = await page.evaluate(() => [...document.querySelectorAll('.gx-edge text')]
  .filter((t) => getComputedStyle(t).display !== 'none').length)
ok('turning it off hides them again', rel === 0, `${rel} shown`)

console.log('\n— isolated concepts —')
const gutter = await page.evaluate(() => {
  const box = document.querySelector('#wsGraphSvg').getBoundingClientRect()
  const named = [...document.querySelectorAll('.gx-node')].map((g) => ({
    name: [...g.querySelectorAll('tspan')].map((s) => s.textContent).join(' '),
    cx: g.getBoundingClientRect().left + g.getBoundingClientRect().width / 2,
  }))
  const stranded = named.filter((n) => n.name.includes('Stranded'))
  const rest = named.filter((n) => !n.name.includes('Stranded'))
  return {
    strandedCount: stranded.length,
    strandedRight: stranded.every((s) => s.cx > box.left + box.width * 0.6),
    medianRest: rest.map((r) => r.cx).sort((a, b) => a - b)[Math.floor(rest.length / 2)],
  }
})
ok('unlinked concepts are kept, not dropped', gutter.strandedCount === 2, `${gutter.strandedCount}`)
ok('unlinked concepts sit off to one side', gutter.strandedRight)

await page.getByRole('button', { name: /unlinked/ }).click()
await page.waitForTimeout(700)
const hidden = await counts()
ok('"Hide unlinked" removes exactly the unlinked concepts',
  hidden.nodes === base.nodes - 2, `${hidden.nodes} vs ${base.nodes - 2}`)
await page.getByRole('button', { name: /unlinked/ }).click()
await page.waitForTimeout(700)
const restored = await counts()
ok('showing them again brings them back', restored.nodes === base.nodes,
  `${restored.nodes} vs ${base.nodes}`)

console.log('\n— search —')
await page.locator('.gx-search input').fill('Deadlock')
await page.waitForTimeout(400)
const searching = await page.evaluate(() => ({
  dim: [...document.querySelectorAll('.gx-node')]
    .map((g) => +getComputedStyle(g).opacity).filter((o) => o < 0.3).length,
  ringed: [...document.querySelectorAll('.gx-node circle')]
    .filter((c) => c.getAttribute('stroke') === '#E0A06A').length,
}))
ok('non-matching concepts dim while searching', searching.dim > 10, `${searching.dim} dimmed`)
ok('the match is visually marked', searching.ringed === 1, `${searching.ringed} marked`)

const before = await page.evaluate(() =>
  document.querySelector('.gx-root').getAttribute('transform'))
await page.locator('.gx-search input').press('Enter')
await page.waitForTimeout(900)
const after = await page.evaluate(() => ({
  transform: document.querySelector('.gx-root').getAttribute('transform'),
  selected: window.__SELECTED__?.name,
}))
ok('Enter centres the graph on the match', after.transform !== before)
ok('Enter selects the matched concept', after.selected === 'Deadlock', String(after.selected))

console.log('\n— reset —')
await page.getByRole('button', { name: 'Reset the view' }).click()
await page.waitForTimeout(900)
const afterReset = await page.evaluate(() => ({
  search: document.querySelector('.gx-search input').value,
  selected: !!window.__SELECTED__,
  dim: [...document.querySelectorAll('.gx-node')]
    .map((g) => +getComputedStyle(g).opacity).filter((o) => o < 0.9).length,
}))
ok('Reset clears the search box', afterReset.search === '')
ok('Reset clears the selection', !afterReset.selected)
ok('Reset clears the dimming', afterReset.dim === 0, `${afterReset.dim}`)

console.log('\n— resize keeps positions —')
const posBefore = await page.evaluate(() =>
  [...document.querySelectorAll('.gx-node')].map((g) => g.getAttribute('transform')))
await page.setViewportSize({ width: 1024, height: 780 })
await page.waitForTimeout(1200)
const posAfter = await page.evaluate(() =>
  [...document.querySelectorAll('.gx-node')].map((g) => g.getAttribute('transform')))
/* The graph legitimately recentres when the canvas changes size, so what
   matters is that the layout keeps its shape: measure drift AFTER removing
   the common translation, not raw coordinates. */
const xy = (t) => {
  const m = /translate\(([-\d.]+),([-\d.]+)\)/.exec(t || '')
  return m ? { x: +m[1], y: +m[2] } : null
}
const pb = posBefore.map(xy).filter(Boolean)
const pa = posAfter.map(xy).filter(Boolean)
const mean = (arr, k) => arr.reduce((s2, v) => s2 + v[k], 0) / arr.length
const shift = { x: mean(pa, 'x') - mean(pb, 'x'), y: mean(pa, 'y') - mean(pb, 'y') }
const drifts = pb.map((b, i) =>
  Math.hypot(pa[i].x - b.x - shift.x, pa[i].y - b.y - shift.y)).sort((a, b) => a - b)
const median = drifts[Math.floor(drifts.length / 2)]
ok('resizing keeps the layout recognisable (median relative drift < 40px)',
  median < 40, `median ${median.toFixed(1)}px, worst ${drifts.at(-1).toFixed(1)}px`)
const svgBox = await page.evaluate(() => {
  const el = document.querySelector('#wsGraphSvg')
  return { w: Math.round(el.getBoundingClientRect().width), vb: el.getAttribute('viewBox') }
})
ok('the canvas resizes with its container',
  svgBox.vb.split(' ')[2] === String(svgBox.w) ||
  Math.abs(+svgBox.vb.split(' ')[2] - svgBox.w) <= 2, `viewBox ${svgBox.vb}, width ${svgBox.w}`)

console.log('\n— week slider —')
await page.setViewportSize({ width: 1440, height: 900 })
await page.waitForTimeout(600)
await page.locator('.ws-timeline input[type=range]').fill('1')
await page.waitForTimeout(1000)
const wk1 = await counts()
ok('week 1 shows fewer concepts', wk1.nodes < base.nodes, `${wk1.nodes} vs ${base.nodes}`)
await page.locator('.ws-timeline input[type=range]').fill('3')
await page.waitForTimeout(1200)
const wk3 = await counts()
ok('returning to week 3 restores them all', wk3.nodes === base.nodes,
  `${wk3.nodes} vs ${base.nodes}`)

console.log('\n— keyboard and labelling —')
const a11y = await page.evaluate(() => {
  const btns = [...document.querySelectorAll('.gx-btn')]
  return {
    total: btns.length,
    labelled: btns.filter((b) => b.getAttribute('aria-label') || b.textContent.trim().length > 1).length,
    titled: btns.filter((b) => b.getAttribute('title')).length,
    nodesFocusable: document.querySelectorAll('.gx-node[tabindex="0"]').length,
    searchLabelled: !!document.querySelector('.gx-search input').getAttribute('aria-label'),
    sliderLabelled: !!document.querySelector('.ws-timeline input').getAttribute('aria-label'),
  }
})
ok('every toolbar button is labelled', a11y.labelled === a11y.total, `${a11y.labelled}/${a11y.total}`)
ok('every toolbar button has a tooltip', a11y.titled === a11y.total, `${a11y.titled}/${a11y.total}`)
ok('concepts are keyboard-focusable', a11y.nodesFocusable > 0, `${a11y.nodesFocusable}`)
ok('the search field is labelled', a11y.searchLabelled)
ok('the week slider is labelled', a11y.sliderLabelled)

console.log('\n— inspector (PART 3) —')
await page.evaluate(() => window.scrollTo(0, 0))
const hub2 = await pickLabelled()
await page.mouse.click(hub2.x, hub2.y)
await page.waitForTimeout(600)

const insp = await page.evaluate(() => {
  const el = document.querySelector('.inspector')
  if (!el) return null
  const chips = [...el.querySelectorAll('.insp-chip')].map((c) => c.textContent.trim())
  const rels = [...el.querySelectorAll('.insp-rels li')].map((li) => ({
    name: li.querySelector('.insp-rel-name')?.textContent,
    type: li.querySelector('.insp-rel-type')?.textContent,
  }))
  const graphBox = document.querySelector('#wsGraphSvg').getBoundingClientRect()
  const inspBox = el.getBoundingClientRect()
  return {
    name: el.querySelector('.insp-name')?.textContent,
    chips,
    rels,
    sources: [...el.querySelectorAll('.insp-source .st')].map((x) => x.textContent),
    hasClose: !!el.querySelector('.insp-close'),
    closeLabelled: !!el.querySelector('.insp-close')?.getAttribute('aria-label'),
    labelled: !!el.getAttribute('aria-label'),
    // the graph must sit beside the panel, not underneath it
    overlapsGraph: inspBox.left < graphBox.right - 2,
  }
})
ok('the panel opens on selection', !!insp)
ok('it names the concept', !!insp?.name, String(insp?.name))
ok('it states the concept type', insp?.chips?.some((c) => /Core|Supporting/.test(c)),
  insp?.chips?.join(' | '))
ok('it states the first week', insp?.chips?.some((c) => /week/i.test(c)),
  insp?.chips?.join(' | '))
ok('it lists connected concepts', (insp?.rels?.length || 0) > 0, `${insp?.rels?.length}`)
ok('each connection carries its relationship type',
  insp?.rels?.every((r) => r.type && r.type.length > 1),
  JSON.stringify(insp?.rels?.slice(0, 2)))
ok('it names the source document', (insp?.sources?.length || 0) > 0, insp?.sources?.join(', '))
ok('the panel is closable', insp?.hasClose && insp?.closeLabelled)
ok('the panel is labelled for assistive tech', insp?.labelled)
ok('the graph resizes beside the panel rather than being covered', !insp?.overlapsGraph)

const framedWithPanel = await page.evaluate(() => {
  const box = document.querySelector('#wsGraphSvg').getBoundingClientRect()
  return box.width > 200
})
ok('the graph canvas keeps a usable width with the panel open', framedWithPanel)

await page.locator('.insp-close').click()
await page.waitForTimeout(400)
ok('closing the panel removes it',
  (await page.locator('.inspector').count()) === 0)
ok('closing the panel also clears the graph selection',
  !(await page.evaluate(() => !!window.__SELECTED__)))

console.log('\n— inspector on a phone (PART 3) —')
// Select at desktop width, then narrow: the selection persists, so this
// checks the panel's responsive form rather than re-testing hit-testing.
const hub3 = await pickLabelled()
await page.mouse.click(hub3.x, hub3.y)
await page.waitForTimeout(500)
await page.setViewportSize({ width: 390, height: 844 })
await page.waitForTimeout(800)
const sheet = await page.evaluate(() => {
  const el = document.querySelector('.inspector')
  if (!el) return null
  const cs = getComputedStyle(el)
  const r = el.getBoundingClientRect()
  return { position: cs.position, bottom: cs.bottom, radius: cs.borderTopLeftRadius,
           atBottom: Math.abs(r.bottom - window.innerHeight) < 2,
           fullWidth: r.width >= window.innerWidth - 2 }
})
ok('it becomes a bottom sheet at 390px', sheet?.position === 'fixed', JSON.stringify(sheet))
ok('the sheet is anchored to the bottom edge', sheet?.atBottom)
ok('the sheet spans the width', sheet?.fullWidth)
await page.setViewportSize({ width: 1440, height: 900 })
await page.waitForTimeout(600)

console.log('\n— graph header (PART 4) —')
const header = await page.evaluate(() => {
  const el = document.querySelector('.ws-top')
  if (!el) return null
  return {
    title: el.querySelector('.ws-title')?.textContent,
    raw: el.querySelector('.ws-title')?.getAttribute('title'),
    facts: [...el.querySelectorAll('.ws-facts span')].map((s2) => s2.textContent.trim()),
  }
})
ok('the filename is prettified for display',
  header?.title === 'FIT3143 Topic 2 2024', String(header?.title))
ok('the real filename is still available on hover',
  header?.raw === 'FIT3143-Topic-2-2024.pdf', String(header?.raw))
ok('the header shows concepts, relationships and the week',
  header?.facts?.length === 3 &&
  /concepts/.test(header.facts[0]) &&
  /relationships/.test(header.facts[1]) &&
  /Week/.test(header.facts[2]),
  header?.facts?.join(' | '))

console.log('\n— console —')
ok('no console errors or page errors', errors.length === 0, errors.slice(0, 3).join(' / '))

await browser.close()
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
