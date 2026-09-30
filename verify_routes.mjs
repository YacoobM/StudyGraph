/**
 * Route smoke test (PART 10 — ROUTING).
 *
 * Loads the real built app and checks every route still mounts, that the
 * protected ones still redirect, and that no page throws. Supabase is not
 * reachable from here, so this proves the routing and rendering paths are
 * intact — it does NOT prove sign-in works. Run the auth checks by hand
 * against a real Supabase project.
 *
 *   node verify_routes.mjs <baseUrl>
 */
import { chromium } from 'playwright-core'

const BASE = process.argv[2] || 'http://localhost:4173'
let pass = 0
let fail = 0
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  PASS  ${name}`) }
  else { fail += 1; console.log(`  FAIL  ${name}${extra ? ` — ${extra}` : ''}`) }
}

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH })
const noise = (t) => /ERR_TUNNEL_CONNECTION_FAILED|fonts\.g(oogleapis|static)\.com|favicon|supabase|Failed to fetch|net::ERR_NAME_NOT_RESOLVED|ERR_PROXY/i.test(t)

async function visit(path) {
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } })
  const errs = []
  page.on('pageerror', (e) => { if (!noise(String(e))) errs.push(String(e)) })
  page.on('console', (m) => {
    if (m.type() !== 'error') return
    const where = m.location?.()?.url || ''
    if (!noise(m.text()) && !noise(where)) errs.push(m.text())
  })
  await page.goto(BASE + path, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1400)
  const state = await page.evaluate(() => ({
    url: location.pathname,
    rootHasContent: (document.getElementById('root')?.children.length || 0) > 0,
    text: document.body.innerText.slice(0, 400),
  }))
  await page.close()
  return { ...state, errs }
}

console.log('\n— landing —')
let r = await visit('/')
ok('/ renders', r.rootHasContent && r.url === '/')
ok('/ throws nothing', r.errs.length === 0, r.errs.slice(0, 2).join(' / '))
ok('/ shows the hero copy', /connected knowledge graph/i.test(r.text))

console.log('\n— login —')
r = await visit('/login')
ok('/login renders', r.rootHasContent && r.url === '/login')
ok('/login throws nothing', r.errs.length === 0, r.errs.slice(0, 2).join(' / '))
ok('/login offers both sign in and create account',
  /sign in/i.test(r.text) && /create account/i.test(r.text), r.text.slice(0, 120))

console.log('\n— protected routes —')
for (const path of ['/workspace', '/builder']) {
  r = await visit(path)
  ok(`${path} redirects to /login when signed out`, r.url === '/login', r.url)
  ok(`${path} throws nothing on the way`, r.errs.length === 0, r.errs.slice(0, 2).join(' / '))
}

console.log('\n— unknown route —')
r = await visit('/nope')
ok('an unknown path falls back to /', r.url === '/', r.url)

await browser.close()
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
