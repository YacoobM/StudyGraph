// UI test: builds the app, serves it, and drives it in headless Chromium
// against the fake API in mock-api.mjs. No backend, Supabase or LLM is needed.
// Checks are grouped by the bug they guard, and each group runs on its own.
//
//   npm run test:ui
//
// Needs a Chromium for playwright-core: run `npx playwright-core install chromium`
// once, or set CHROMIUM_PATH to an installed Chrome or Chromium.
import { build, preview } from 'vite'
import { chromium } from 'playwright-core'
import { API, makeMock, mockRoutes, seed } from './mock-api.mjs'

const OUT = '.harness-dist'
Object.assign(process.env, {
  VITE_API_URL: API,
  VITE_SUPABASE_URL: 'https://example.supabase.co',
  VITE_SUPABASE_PUBLISHABLE_KEY: 'test-key',
})

let pass = 0
let fail = 0
const check = (name, ok, detail = '') => {
  if (ok) { pass++; console.log(`  PASS ${name}`) } else { fail++; console.log(`  FAIL ${name}${detail ? `  -> ${detail}` : ''}`) }
}

await build({ logLevel: 'warn', build: { outDir: OUT, emptyOutDir: true } })
const server = await preview({ logLevel: 'warn', build: { outDir: OUT }, preview: { port: 4317, strictPort: true } })
const BASE = 'http://localhost:4317'
const browser = await chromium.launch(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {})

async function session(opts = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
  const mock = makeMock(opts)
  await mockRoutes(ctx, mock)
  await seed(ctx)
  // Fonts come from Google; the test does not need them.
  await ctx.route('https://fonts.googleapis.com/**', (r) => r.fulfill({ status: 200, contentType: 'text/css', body: '' }))
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', (e) => errors.push(e.message))
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
  await page.goto(`${BASE}/workspace`, { waitUntil: 'networkidle' })
  await page.waitForSelector('.gx-node', { timeout: 15000 })
  return { ctx, page, mock, errors }
}

// Runs one group. A crash fails that group only, so later groups still report.
async function group(title, opts, fn) {
  console.log(`\n${title}`)
  let s
  try {
    s = await session(opts)
    await fn(s)
    check('no console errors', s.errors.length === 0, s.errors.join(' | '))
  } catch (e) {
    check('group ran to the end', false, e.message.split('\n')[0])
  } finally {
    await s?.ctx.close()
  }
}

const tab = (page, name) => page.click(`.ws-tab:has-text("${name}")`)
const graphCounts = (page) => page.$$eval('.st-row-graph', (rows) =>
  rows.map((r) => `${r.querySelector('.st-name')?.textContent}=${r.querySelector('.st-count')?.textContent}`).join(', '))
const openConcept = async (page, name) => {
  await page.locator(`.gx-node[aria-label^="${name},"]`).dispatchEvent('click')
  await page.waitForSelector(`.insp-name:has-text("${name}")`)
}
const regenerateCalls = (mock) => mock.calls.filter((c) => c.endsWith('/quiz/regenerate')).length

try {
  await group('Bug A: Generate Quiz looked frozen', { quizDelay: 1500 }, async ({ page, mock }) => {
    await tab(page, 'Quiz')
    await page.click('.qz-land-row >> nth=0')
    await page.click('button:has-text("Generate quiz")')
    check('progress shows the moment Generate is clicked', await page.locator('.qz-gen-title').isVisible())
    check('Generate cannot be clicked again while it runs', (await page.locator('button:has-text("Generate quiz")').count()) === 0)
    await tab(page, 'Notes')
    await tab(page, 'Quiz')
    await page.click('.qz-land-row >> nth=0')
    check('progress is still shown after switching tabs', await page.locator('.qz-gen-title').isVisible())
    await page.waitForSelector('.qz-gen', { state: 'detached', timeout: 10000 })
    check('only one generate request was sent', regenerateCalls(mock) === 1, `${regenerateCalls(mock)} requests`)
    check('the quiz appears when generation finishes', (await page.locator('button:has-text("Start")').count()) > 0)
    await page.click('button:has-text("Start")')
    await page.click('.qz-choice >> nth=1')
    await page.click('button:has-text("Check answer")')
    await page.click('button:has-text("Next question")')
    await page.waitForTimeout(300)
    check('answering a question saves progress', mock.calls.includes('POST /api/graphs/g1/quiz/results'))
  })

  await group('Bug A: a failed generation', { quizDelay: 300, quizFail: true }, async ({ page }) => {
    // The 503 is expected here, so it is not counted as a console error.
    page.removeAllListeners('console')
    await tab(page, 'Quiz')
    await page.click('.qz-land-row >> nth=0')
    await page.click('button:has-text("Generate quiz")')
    await page.waitForSelector('.qz-gen-body.error', { timeout: 5000 })
    const text = await page.locator('.qz-gen-body.error').innerText()
    check('the server message is shown', text.includes('busy'), text)
    check('there is a way to try again', (await page.locator('.qz-gen-body.error button').count()) > 0)
  })

  await group('Bug A: a generation that takes too long', { quizDelay: 600000 }, async ({ page, mock }) => {
    // A fake clock lets the 2 minute limit pass without waiting for it.
    await page.clock.install()
    await tab(page, 'Quiz')
    await page.click('.qz-land-row >> nth=0')
    await page.click('button:has-text("Generate quiz")')
    check('progress shows while the server is slow', await page.locator('.qz-gen-title').isVisible())
    await page.clock.fastForward(121000)
    await page.waitForSelector('.qz-gen-body.warn', { timeout: 5000 })
    check('after 2 minutes it says it is taking longer than usual', /longer than usual/i.test(await page.locator('.qz-gen-body.warn').innerText()))
    const quizReads = mock.calls.filter((c) => c === 'GET /api/graphs/g1/quiz').length
    await page.click('button:has-text("Check again")')
    await page.waitForTimeout(500)
    check('Check again asks the server for the quiz', mock.calls.filter((c) => c === 'GET /api/graphs/g1/quiz').length > quizReads)
    check('Check again does not start a second generation', regenerateCalls(mock) === 1, `${regenerateCalls(mock)} requests`)
  })

  await group('Bug B: "DAY null OF 3" in revision notes', {}, async ({ page, mock }) => {
    await tab(page, 'Notes')
    await page.click('.nt-card-open >> nth=0')
    await page.waitForSelector('.nt-eyebrow')
    const eyebrow = await page.locator('.nt-eyebrow').innerText()
    check('a note saved with null days reads "Section 1 of 3"', /Section 1 of 3/i.test(eyebrow) && !/null/i.test(eyebrow), eyebrow)
    check('no "null" anywhere in the reader', !/\bnull\b/i.test(await page.locator('.nt-detail').innerText()))

    await page.click('.nt-back')
    await page.click('.nt-head .nt-btn.primary')
    await page.click('.nt-switch-row')
    await page.click('.nt-actions .nt-btn.primary')
    await page.waitForSelector('.nt-eyebrow', { timeout: 10000 })
    const plan = await page.locator('.nt-eyebrow').innerText()
    check('a 3 day plan reads "Day 1 of 3"', /Day 1 of 3/i.test(plan), plan)
    const days = await page.locator('.nt-group.day .nt-group-head').allInnerTexts()
    check('every one of the 3 days has sections', days.length === 3, days.join(' / '))
    await page.waitForSelector('.nt-done-btn:not([disabled])', { timeout: 10000 })
    await page.click('.nt-done-btn')
    await page.waitForTimeout(300)
    check('Mark complete is saved to the server', mock.calls.some((c) => /^PATCH .*\/sections\/ns0$/.test(c)))

    await page.click('.nt-back')
    check('the notes list shows "3-day plan"', /3-day plan/i.test(await page.locator('.nt-tag.plan').first().innerText()))
    await page.click('.nt-head .nt-btn.primary')
    await page.click('.nt-switch-row')
    for (let i = 0; i < 7; i++) await page.click('button[aria-label="More days"]')
    await page.click('.nt-actions .nt-btn.primary')
    await page.waitForSelector('.nt-eyebrow', { timeout: 10000 })
    const capped = await page.locator('.nt-eyebrow').innerText()
    check('asking for 10 days with 4 sections gives a 4 day plan', /Day 1 of 4/i.test(capped), capped)
  })

  await group('Bug C: Concept connections could not be collapsed', {}, async ({ page }) => {
    await openConcept(page, 'Threads')
    const head = page.locator('.insp-sec-head:has-text("Connections")')
    check('Connections starts open', (await head.getAttribute('aria-expanded')) === 'true')
    await head.click()
    check('clicking the heading collapses it', (await head.getAttribute('aria-expanded')) === 'false' && !(await page.locator('.insp-rels').isVisible()))
    await head.click()
    check('clicking again expands it', await page.locator('.insp-rels').isVisible())
    check('a long list starts with 5', (await page.locator('.insp-rels li').count()) === 5)
    await page.click('.insp-more:has-text("Show all")')
    check('"Show all" shows all 13', (await page.locator('.insp-rels li').count()) === 13)
    await page.click('button.insp-rel-name:has-text("Thread Pool")')
    check('clicking a connection opens that concept', await page.locator('.insp-name:has-text("Thread Pool")').isVisible())
  })

  await group('Bug D: Sources broken or inconsistent', {}, async ({ page, ctx }) => {
    await openConcept(page, 'Threads')
    const cards = page.locator('.insp-src')
    check('one card per document (3 records, 2 files)', (await cards.count()) === 2, `${await cards.count()} cards`)
    const text = await page.locator('.insp-srcs').innerText()
    check('file names, weeks and pages are shown', text.includes('FIT3143-Topic-2-2024.pdf') && /Week 1/.test(text) && /pp?\. 2/.test(text), text.replace(/\n/g, ' | '))
    check('no "undefined" or "null" in sources', !/undefined|null/i.test(text))
    check('live documents are not marked removed', (await page.locator('.insp-src.gone').count()) === 0)
    const popup = ctx.waitForEvent('page')
    await page.locator('.insp-src-open:not([disabled])').first().click()
    const opened = await popup
    await opened.waitForURL(/example\.com/)
    check('Open goes to the signed file at the cited page', opened.url().includes('#page=2'), opened.url())
    await opened.close()

    await openConcept(page, 'Thread Pool')
    check('a deleted document is marked "Removed from this graph"', (await page.locator('.insp-src.gone').innerText()).includes('Removed from this graph'))
    await openConcept(page, 'Semaphore')
    check('a record with no file says "No file link recorded"', (await page.locator('.insp-srcs').innerText()).includes('No file link recorded'))
  })

  await group('Bug E: Timeline repeated information', {}, async ({ page }) => {
    await openConcept(page, 'Threads')
    const weeks = await page.locator('.insp-timeline > li .insp-tl-week').allInnerTexts()
    check('one timeline entry per week', weeks.length > 0 && new Set(weeks).size === weeks.length, weeks.join(', '))
    const lines = await page.locator('.insp-timeline > li p').allInnerTexts()
    check('no sentence is repeated', new Set(lines).size === lines.length, lines.join(' | '))
    await openConcept(page, 'Mutex')
    check('a concept seen in one week has no week slider', (await page.locator('.insp-evo-view input[type="range"]').count()) === 0)
  })

  await group('Bug F: Graph count changed from 1 to 0', {}, async ({ page }) => {
    const before = await graphCounts(page)
    check('each graph shows its own document count', before.includes('Threads and Synchronisation=2') && before.includes('OpenMP=1'), before)
    await page.click('.st-row-graph:has-text("OpenMP") .st-main')
    await page.waitForTimeout(800)
    check('counts stay the same after opening another graph', (await graphCounts(page)) === before, await graphCounts(page))
    for (const name of ['Documents', 'Notes', 'Quiz', 'Graph']) { await tab(page, name); await page.waitForTimeout(300) }
    check('counts stay the same after moving between tabs', (await graphCounts(page)) === before, await graphCounts(page))
    const all = await page.locator('.st-count').first().innerText()
    check('"All documents" counts every graph (3)', all.trim() === '3', all)
  })

  await group('Concept aliases (from main)', {}, async ({ page }) => {
    await openConcept(page, 'Mutex')
    check('the inspector lists names merged into the concept', /Also known as Mutual exclusion lock/.test(await page.locator('.insp-aliases').innerText()))
    await page.keyboard.press('Escape')
    await page.fill('.gx-search input', 'exclusion lock')
    await page.keyboard.press('Enter')
    await page.waitForTimeout(600)
    check('searching an alias finds the concept', await page.locator('.insp-name:has-text("Mutex")').isVisible())
  })

  await group('Graph and keyboard', {}, async ({ page }) => {
    await page.waitForTimeout(1500)
    const drawn = await page.$$eval('.gx-node', (nodes) => nodes.filter((n) => getComputedStyle(n).display !== 'none').length)
    check('a small graph draws every concept', drawn === 17, `${drawn} of 17`)
    await page.focus('.gx-node[aria-label^="Mutex,"]')
    await page.keyboard.press('Enter')
    check('Enter on a focused concept opens it', await page.locator('.insp-name:has-text("Mutex")').isVisible())
    await page.keyboard.press('Escape')
    check('Escape closes the concept panel', (await page.locator('.inspector.insp-concept').count()) === 0)
  })
} finally {
  await browser.close()
  await new Promise((resolve) => server.httpServer.close(resolve))
}

console.log(`\nRESULT: ${pass}/${pass + fail} passed`)
process.exit(fail ? 1 : 0)
