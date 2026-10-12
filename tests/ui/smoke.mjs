// UI smoke test: builds the app, serves it, and drives it in headless Chromium
// against the fake API in mock-api.mjs. No backend, Supabase or LLM is needed.
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

const tab = (page, name) => page.click(`.ws-tab:has-text("${name}")`)
const graphCounts = (page) => page.$$eval('.st-row-graph', (rows) =>
  rows.map((r) => `${r.querySelector('.st-name')?.textContent}=${r.querySelector('.st-count')?.textContent}`).join(', '))

try {
  console.log('graph and sidebar')
  {
    const { ctx, page, errors } = await session()
    await page.waitForTimeout(1500)
    const drawn = await page.$$eval('.gx-node', (nodes) => nodes.filter((n) => getComputedStyle(n).display !== 'none').length)
    check('a small graph draws every concept', drawn === 17, `${drawn} of 17`)
    const before = await graphCounts(page)
    await page.click('.st-row-graph:has-text("OpenMP") .st-main')
    await page.waitForTimeout(800)
    await page.click('.st-row-graph:has-text("Threads") .st-main')
    await page.waitForSelector('.gx-node')
    const after = await graphCounts(page)
    check('graph document counts stay the same after switching graphs', before === after, `${before} | ${after}`)

    console.log('inspector')
    await page.click('.gx-node[aria-label^="Threads,"]')
    await page.waitForSelector('.inspector .insp-sec-head')
    const head = page.locator('.insp-sec-head').first()
    const opened = await head.getAttribute('aria-expanded')
    await head.click()
    check('a section collapses and expands', opened === 'true' && (await head.getAttribute('aria-expanded')) === 'false')
    await head.click()
    const more = page.locator('.insp-more:has-text("Show all")')
    check('long connection lists start short', (await more.count()) === 1)
    check('sources show page numbers', /p\. 2|pp\. 2/.test(await page.locator('.insp-srcs').innerText()))
    const popup = ctx.waitForEvent('page')
    await page.locator('.insp-src-open:not([disabled])').first().click()
    const opened2 = await popup
    await opened2.waitForURL(/example\.com/)
    check('Open goes to the signed file at the cited page', opened2.url().includes('#page='), opened2.url())
    await opened2.close()
    await more.click()
    await page.click('button.insp-rel-name:has-text("Thread Pool")')
    await page.waitForSelector('.insp-name:has-text("Thread Pool")')
    check('a connection opens that concept', true)
    check('a source from a removed document is flagged', (await page.locator('.insp-src.gone').count()) === 1)
    await page.keyboard.press('Escape')
    check('Escape closes the concept panel', (await page.locator('.inspector.insp-concept').count()) === 0)
    await page.focus('.gx-node[aria-label^="Mutex,"]')
    await page.keyboard.press('Enter')
    check('Enter on a focused concept opens it', await page.locator('.insp-name:has-text("Mutex")').isVisible())
    check('no console errors on the graph tab', errors.length === 0, errors.join(' | '))
    await ctx.close()
  }

  console.log('revision notes')
  {
    const { ctx, page, mock, errors } = await session()
    await tab(page, 'Notes')
    await page.click('.nt-card-open >> nth=0')
    await page.waitForSelector('.nt-eyebrow')
    const eyebrow = await page.locator('.nt-eyebrow').innerText()
    check('a stored note without days never shows "Day null"', !/null/i.test(eyebrow) && /Section 1 of 3/i.test(eyebrow), eyebrow)
    await page.click('.nt-back')
    await page.click('.nt-head .nt-btn.primary')
    await page.click('.nt-switch-row')
    await page.click('.nt-actions .nt-btn.primary')
    await page.waitForSelector('.nt-eyebrow', { timeout: 10000 })
    const planEyebrow = await page.locator('.nt-eyebrow').innerText()
    check('a 3 day plan labels days', /Day 1 of 3/i.test(planEyebrow), planEyebrow)
    const groups = await page.locator('.nt-group.day .nt-group-head').allInnerTexts()
    check('every day in the plan has sections', groups.length === 3, groups.join(' / '))
    await page.waitForSelector('.nt-done-btn:not([disabled])', { timeout: 10000 })
    await page.click('.nt-done-btn')
    await page.waitForTimeout(300)
    check('Mark complete is saved to the server', mock.calls.some((c) => /^PATCH .*\/sections\/ns0$/.test(c)))
    check('no console errors in notes', errors.length === 0, errors.join(' | '))
    await ctx.close()
  }

  console.log('quiz generation')
  {
    const { ctx, page, mock, errors } = await session({ quizDelay: 1500 })
    await tab(page, 'Quiz')
    await page.click('.qz-land-row >> nth=0')
    await page.click('button:has-text("Generate quiz")')
    check('generating shows progress at once', await page.locator('.qz-gen-title').isVisible())
    await page.waitForSelector('.qz-gen', { state: 'detached', timeout: 10000 })
    check('questions appear when generation finishes', !(await page.locator('.qz-gen').count()))
    await page.click('button:has-text("Start")')
    await page.click('.qz-choice >> nth=1')
    await page.click('button:has-text("Check answer")')
    await page.click('button:has-text("Next question")')
    await page.waitForTimeout(300)
    check('answering a question saves progress', mock.calls.some((c) => c === 'POST /api/graphs/g1/quiz/results'))
    check('no console errors in quiz', errors.length === 0, errors.join(' | '))
    await ctx.close()
  }
  {
    const { ctx, page } = await session({ quizDelay: 300, quizFail: true })
    await tab(page, 'Quiz')
    await page.click('.qz-land-row >> nth=0')
    await page.click('button:has-text("Generate quiz")')
    await page.waitForSelector('.qz-gen-body.error', { timeout: 5000 })
    const text = await page.locator('.qz-gen-body.error').innerText()
    check('a failed generation shows the server message', text.includes('busy'), text)
    await ctx.close()
  }
} catch (e) {
  check('test ran to the end', false, e.message.split('\n')[0])
} finally {
  await browser.close()
  await new Promise((resolve) => server.httpServer.close(resolve))
}

console.log(`\nRESULT: ${pass}/${pass + fail} passed`)
process.exit(fail ? 1 : 0)
