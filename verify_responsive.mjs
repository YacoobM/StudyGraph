/**
 * Responsiveness and contrast sweep (PART 6 and PART 7).
 *
 * Checks the landing page and the graph workspace at every width in the spec,
 * for horizontal overflow, text that runs outside its box, touch targets that
 * are too small, and text contrast below WCAG AA.
 *
 *   node verify_responsive.mjs <landingUrl> <graphUrl>
 */
import { chromium } from 'playwright-core'

const LANDING = process.argv[2] || 'http://localhost:4173'
const GRAPH = process.argv[3] || 'http://localhost:4174/.harness/index.html'
const WIDTHS = [1920, 1440, 1280, 1024, 768, 390]

let pass = 0
let fail = 0
const ok = (name, cond, extra = '') => {
  if (cond) { pass += 1; console.log(`  PASS  ${name}`) }
  else { fail += 1; console.log(`  FAIL  ${name}${extra ? ` — ${extra}` : ''}`) }
}

/* Relative luminance and contrast ratio, per WCAG 2.1. */
const CONTRAST = `(() => {
  const lum = (c) => {
    const [r, g, b] = c.map((v) => {
      const s = v / 255
      return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const parse = (str) => {
    const m = /rgba?\\(([^)]+)\\)/.exec(str)
    if (!m) return null
    const p = m[1].split(',').map((n) => parseFloat(n))
    return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 }
  }
  const bgOf = (el) => {
    let n = el
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor)
      if (c && c.a > 0.85) return c.rgb
      n = n.parentElement
    }
    return [255, 255, 255]
  }
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b)
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
  }
  const bad = []
  document.querySelectorAll('h1,h2,h3,p,span,button,a,label,li,div,td,th').forEach((el) => {
    if (!el.childNodes.length) return
    const text = [...el.childNodes]
      .filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join('')
    if (text.length < 2) return
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity < 0.6) return
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) return
    const fg = parse(cs.color)
    if (!fg || fg.a < 0.6) return
    const size = parseFloat(cs.fontSize)
    const heavy = +cs.fontWeight >= 700
    const large = size >= 24 || (size >= 18.66 && heavy)
    const need = large ? 3 : 4.5
    const got = ratio(fg.rgb, bgOf(el))
    if (got < need) {
      bad.push(text.slice(0, 34) + ' :: ' + got.toFixed(2) + ' need ' + need)
    }
  })
  return bad
})()`

const OVERFLOW = `(() => {
  const doc = document.documentElement
  const page = doc.scrollWidth - doc.clientWidth
  // An element inside a clipping ancestor still reports its full geometry,
  // and a skip link is deliberately parked off-screen: neither is a spill.
  const clipped = (el) => {
    let n = el.parentElement
    while (n) {
      const c = getComputedStyle(n)
      if (c.overflow !== 'visible' || n.tagName.toLowerCase() === 'svg') return true
      n = n.parentElement
    }
    return false
  }
  const name = (el) =>
    (typeof el.className === 'string' && el.className) ||
    el.id || el.tagName.toLowerCase()
  const wide = []
  document.querySelectorAll('*').forEach((el) => {
    const r = el.getBoundingClientRect()
    if (r.width === 0) return
    if (r.right > window.innerWidth + 2 || r.left < -2) {
      const cs = getComputedStyle(el)
      if (cs.position === 'fixed' || cs.overflow === 'hidden') return
      if (r.right < 0 || r.left < -1000) return     // off-screen skip link
      if (clipped(el)) return
      wide.push(name(el))
    }
  })
  return { page, wide: [...new Set(wide)].slice(0, 4) }
})()`

const TARGETS = `(() => {
  const small = []
  document.querySelectorAll('button, a[href], input[type=range]').forEach((el) => {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden') return
    const r = el.getBoundingClientRect()
    if (!r.width || !r.height) return
    if (r.height < 24 || r.width < 24) {
      small.push(((el.textContent || el.getAttribute('aria-label') || el.tagName).trim()
        .slice(0, 20)) + ' ' + Math.round(r.width) + 'x' + Math.round(r.height))
    }
  })
  return small
})()`

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH })

for (const [label, url, settle] of [['landing', LANDING, 1800], ['graph workspace', GRAPH, 3400]]) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
  await page.goto(url, { waitUntil: 'networkidle' })
  await page.waitForTimeout(settle)

  console.log(`\n— ${label}: layout at each width —`)
  for (const w of WIDTHS) {
    await page.setViewportSize({ width: w, height: w < 500 ? 844 : 900 })
    await page.waitForTimeout(500)
    const o = await page.evaluate(OVERFLOW)
    ok(`${w}px: no horizontal page scroll`, o.page <= 1, `${o.page}px`)
    ok(`${w}px: nothing spills outside the viewport`, o.wide.length === 0, o.wide.join(', '))
  }

  console.log(`\n— ${label}: touch targets at 390px —`)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.waitForTimeout(600)
  const small = await page.evaluate(TARGETS)
  ok('every control is at least 24px on its short side',
    small.length === 0, small.slice(0, 4).join(' / '))

  console.log(`\n— ${label}: text contrast (WCAG AA) —`)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.waitForTimeout(600)
  const bad = await page.evaluate(CONTRAST)
  ok('all visible text clears AA', bad.length === 0, bad.slice(0, 4).join(' | '))

  console.log(`\n— ${label}: keyboard focus —`)
  const focusable = await page.evaluate(() =>
    document.querySelectorAll('button:not([disabled]), a[href], input, [tabindex="0"]').length)
  ok('there are focusable controls', focusable > 0, `${focusable}`)
  await page.keyboard.press('Tab')
  const ring = await page.evaluate(() => {
    const el = document.activeElement
    if (!el || el === document.body) return null
    const cs = getComputedStyle(el)
    return {
      tag: el.tagName,
      outline: cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0,
      shadow: cs.boxShadow !== 'none',
    }
  })
  ok('the first tab stop shows a visible focus ring',
    !!ring && (ring.outline || ring.shadow), JSON.stringify(ring))

  await page.close()
}

await browser.close()
console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail ? 1 : 0)
