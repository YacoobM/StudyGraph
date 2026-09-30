/**
 * Headless check of the redesigned landing page.
 *
 * Drives the real built app in Chromium: asserts the removed social proof is
 * gone, the process strip renders, and the Week 1 -> Week 2 demo actually
 * accumulates nodes while keeping the Week 1 concepts in roughly the same
 * place. Run against `npx vite preview` (or `npm run dev`).
 *
 *   node verify_landing.mjs [baseUrl]
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'http://localhost:4173'
const EXEC = process.env.CHROMIUM_PATH || undefined   // falls back to Playwright's own lookup

let pass = 0
let fail = 0
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  PASS  ${name}`) }
  else { fail += 1; console.log(`  FAIL  ${name}${extra ? ` — ${extra}` : ''}`) }
}

const browser = await chromium.launch({ executablePath: EXEC })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })

// Network failures for the Google Fonts stylesheet are ignored: they mean the
// machine running this has no outbound access to fonts.googleapis.com, which
// is an environment fact, not an application error.
const consoleErrors = []
const isFontNoise = (t) => /ERR_TUNNEL_CONNECTION_FAILED|fonts\.g(oogleapis|static)\.com/.test(t)
page.on('console', (m) => {
  if (m.type() === 'error' && !isFontNoise(m.text())) consoleErrors.push(m.text())
})
page.on('pageerror', (e) => { if (!isFontNoise(String(e))) consoleErrors.push(String(e)) })

await page.goto(BASE, { waitUntil: 'networkidle' })

console.log('\n— removed content —')
const body = await page.textContent('body')
ok('no "Used by students at"', !body.includes('Used by students at'))
ok('no Jordan Reyes testimonial', !body.includes('Jordan Reyes'))
ok('no fake university logos', !/Berkeley|UMich|UT Austin|Cornell|UBC/.test(body))
ok('StudyGraph wordmark present', (await page.locator('.lp-wordmark').count()) >= 1)

console.log('\n— navbar —')
for (const label of ['Features', 'How It Works', 'Demo', 'Sign In', 'Get Started']) {
  ok(`nav has "${label}"`, (await page.getByRole('button', { name: label, exact: true }).count()) >= 1)
}

console.log('\n— hero —')
ok('hero heading is the specified copy',
  (await page.locator('.lp-hero-copy h1').textContent())
    .includes('Turn your learning materials into a connected knowledge graph'))
ok('"Start Building" CTA', (await page.getByRole('button', { name: 'Start Building' }).count()) === 1)
ok('"See How It Works" CTA', (await page.getByRole('button', { name: 'See How It Works' }).count()) === 1)

console.log('\n— process —')
const steps = page.locator('.lp-step')
ok('four process steps', (await steps.count()) === 4)
const stepText = await steps.allTextContents()
ok('steps are Upload/Understand/Connect/Explore',
  ['Upload', 'Understand', 'Connect', 'Explore'].every((w, i) => stepText[i].includes(w)),
  stepText.join(' | '))
ok('steps are numbered 01–04',
  ['01', '02', '03', '04'].every((n, i) => stepText[i].includes(n)))
ok('rendered as an ordered list', (await page.locator('ol.lp-process').count()) === 1)
const connectors = await page.evaluate(() =>
  [...document.querySelectorAll('.lp-step')]
    .filter((el) => getComputedStyle(el, '::after').content !== 'none').length)
ok('no connector rules between the desktop steps', connectors === 0, `${connectors} drawn`)

console.log('\n— benefits —')
const benefits = await page.locator('.lp-benefit h3').allTextContents()
ok('three benefits', benefits.length === 3, benefits.join(' | '))
ok('benefit copy matches the spec',
  benefits[0].includes('relationships between concepts') &&
  benefits[1].includes('knowledge grows over time') &&
  benefits[2].includes('connections across'))

console.log('\n— final CTA —')
ok('"Build your knowledge graph" heading', body.includes('Build your knowledge graph'))
ok('"Upload your first document" button',
  (await page.getByRole('button', { name: 'Upload your first document' }).count()) === 1)

console.log('\n— graph demo: it plays itself —')
await page.waitForSelector('.lp-demo-canvas svg .lgd-node', { timeout: 5000 })
await page.evaluate(() => document.getElementById('lp-demo').scrollIntoView())
const countNodes = () => page.evaluate(() => document.querySelectorAll('.lgd-node').length)
await page.waitForTimeout(1200)
ok('it starts on week 1 without being touched', (await countNodes()) === 7,
  `${await countNodes()} nodes`)
await page.waitForTimeout(4200)
ok('it advances to week 2 on its own', (await countNodes()) === 14,
  `${await countNodes()} nodes`)
// Poll rather than sampling at a fixed offset: the loop keeps running, so a
// single late sample can land on week 2 of the NEXT pass and read as a failure.
let looped = false
for (let i = 0; i < 40 && !looped; i += 1) {
  await page.waitForTimeout(250)
  if ((await countNodes()) === 7) looped = true
}
ok('it loops back to week 1 on its own', looped, `still ${await countNodes()} nodes`)
ok('no replay control while it is still playing',
  (await page.getByRole('button', { name: 'Replay' }).count()) === 0)

console.log('\n— graph demo: week 1 —')
// Clicking a week chip takes control, so the assertions below are not racing
// the autoplay timer.
await page.getByRole('button', { name: 'Week 1' }).click()
await page.waitForTimeout(2600)   // let the simulation cool
ok('taking control offers a way to replay',
  (await page.getByRole('button', { name: 'Replay' }).count()) === 1)

const read = async () => page.evaluate(() => {
  const pos = {}
  document.querySelectorAll('.lgd-node').forEach((g) => {
    const m = /translate\(([-\d.]+),([-\d.]+)\)/.exec(g.getAttribute('transform') || '')
    // labels wrap onto tspans, so rejoin them with the space they were split on
    const spans = [...g.querySelectorAll('text tspan')].map((s) => s.textContent)
    const label = spans.length ? spans.join(' ') : (g.querySelector('text')?.textContent || '')
    if (m) pos[label] = { x: +m[1], y: +m[2] }
  })
  return {
    nodes: document.querySelectorAll('.lgd-node').length,
    links: document.querySelectorAll('.lgd-link').length,
    bar: document.querySelector('.lp-demo-stats')?.textContent || '',
    pos,
  }
})

const w1 = await read()
ok('week 1 draws 7 nodes', w1.nodes === 7, `got ${w1.nodes}`)
ok('week 1 draws 8 links', w1.links === 8, `got ${w1.links}`)
ok('week 1 stats read 7 concepts / 8 relationships',
  w1.bar.includes('7') && w1.bar.includes('8'), w1.bar)
ok('week 1 shows no "+N new" badge', !w1.bar.includes('+'), w1.bar)

console.log('\n— graph demo: week 1 → week 2 —')
await page.getByRole('button', { name: 'Week 2' }).click()
await page.waitForTimeout(3200)
const w2 = await read()

ok('week 2 draws 14 nodes', w2.nodes === 14, `got ${w2.nodes}`)
ok('week 2 draws 19 links', w2.links === 19, `got ${w2.links}`)
ok('week 2 stats read 14 / 19 / +7 new',
  w2.bar.includes('14') && w2.bar.includes('19') && w2.bar.includes('+7'), w2.bar)

const week1Names = [
  'Parallel Computing', 'Parallel Programming Models', 'Shared Memory',
  'Distributed Computing', 'Concurrency', 'Memory Architecture',
  'Performance Scalability',
]
ok('every week 1 concept survives into week 2',
  week1Names.every((n) => w2.pos[n]),
  week1Names.filter((n) => !w2.pos[n]).join(', '))

const drift = week1Names
  .filter((n) => w1.pos[n] && w2.pos[n])
  .map((n) => Math.hypot(w2.pos[n].x - w1.pos[n].x, w2.pos[n].y - w1.pos[n].y))
const maxDrift = Math.max(...drift)
ok('week 1 nodes stay near their old positions (max drift < 120px)',
  maxDrift < 120, `max drift ${maxDrift.toFixed(1)}px`)

// Every concept has to be inside the panel, not merely present in the DOM:
// an overflowing force layout still "renders" nodes nobody can see.
const outside = await page.evaluate(() => {
  const box = document.querySelector('.lp-demo-canvas').getBoundingClientRect()
  return [...document.querySelectorAll('.lgd-node')].filter((g) => {
    const r = g.getBoundingClientRect()
    return r.left < box.left - 1 || r.right > box.right + 1 ||
           r.top < box.top - 1 || r.bottom > box.bottom + 1
  }).map((g) => [...g.querySelectorAll('tspan')].map((s) => s.textContent).join(' '))
})
ok('every week 2 concept is inside the panel', outside.length === 0, outside.join(', '))

// Overlapping labels were the specific failure this layout had to solve, so
// the check is geometric rather than a look at a screenshot.
const collisions = await page.evaluate(() => {
  const boxes = [...document.querySelectorAll('.lgd-node text')].map((t) => ({
    name: [...t.querySelectorAll('tspan')].map((s) => s.textContent).join(' '),
    r: t.getBoundingClientRect(),
  }))
  const hits = []
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      const a = boxes[i].r, b = boxes[j].r
      const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left)
      const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top)
      if (ox > 2 && oy > 2) hits.push(`${boxes[i].name} / ${boxes[j].name}`)
    }
  }
  return hits
})
ok('no overlapping concept labels at week 2', collisions.length === 0, collisions.join('; '))

const newNames = ['TCP Sockets', 'UDP Sockets', 'Inter Process Communication',
  'Client Server Model', 'RPC', 'HTTP Protocol', 'BSD Socket API']
ok('all seven week 2 concepts appear',
  newNames.every((n) => w2.pos[n]),
  newNames.filter((n) => !w2.pos[n]).join(', '))

const emphasisCleared = await page.evaluate(() => {
  const fills = [...document.querySelectorAll('.lgd-node circle')]
    .map((c) => c.getAttribute('fill'))
  return fills.filter((f) => f === '#C1652F').length
})
ok('new-node emphasis has decayed back to normal styling',
  emphasisCleared === 0, `${emphasisCleared} still copper`)

console.log('\n— going back to week 1 —')
await page.getByRole('button', { name: 'Week 1' }).click()
await page.waitForTimeout(1600)
const back = await read()
ok('week 1 returns to 7 nodes / 8 links', back.nodes === 7 && back.links === 8,
  `${back.nodes} / ${back.links}`)

console.log('\n— replay —')
await page.getByRole('button', { name: 'Replay' }).click()
await page.waitForTimeout(1000)
ok('Replay returns to week 1', (await countNodes()) === 7, `${await countNodes()} nodes`)
ok('Replay hands the timeline back to the demo',
  (await page.getByRole('button', { name: 'Replay' }).count()) === 0)
await page.waitForTimeout(4200)
ok('and it starts playing again on its own', (await countNodes()) === 14,
  `${await countNodes()} nodes`)
await page.getByRole('button', { name: 'Week 2' }).click()   // park it for the checks below
await page.waitForTimeout(1200)

console.log('\n— demo isolation —')
const leak = await page.evaluate(() =>
  performance.getEntriesByType('resource').filter((r) => /\/api\//.test(r.name)).length)
ok('landing page makes no /api/ requests', leak === 0, `${leak} calls`)

console.log('\n— responsiveness —')
for (const w of [1920, 1440, 1280, 1024, 768, 390]) {
  await page.setViewportSize({ width: w, height: 900 })
  await page.waitForTimeout(350)
  const over = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth)
  ok(`no horizontal overflow at ${w}px`, over <= 1, `${over}px overflow`)
}

await page.setViewportSize({ width: 390, height: 844 })
await page.waitForTimeout(300)
const spine = await page.evaluate(() => {
  const s = getComputedStyle(document.querySelector('.lp-process'))
  return s.gridTemplateColumns.split(' ').length
})
ok('process becomes a single vertical column at 390px', spine === 1, `${spine} columns`)
const mobileSpine = await page.evaluate(() =>
  [...document.querySelectorAll('.lp-step')]
    .filter((el) => getComputedStyle(el, '::after').content !== 'none').length)
ok('the vertical timeline spine is drawn at 390px', mobileSpine >= 3, `${mobileSpine} drawn`)

console.log('\n— console —')
ok('no console errors or page errors', consoleErrors.length === 0,
  consoleErrors.slice(0, 3).join(' / '))

await browser.close()
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
