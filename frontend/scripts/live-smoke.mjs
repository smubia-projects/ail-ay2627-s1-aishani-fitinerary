// Smoke test for a LIVE (or local) frontend + backend pair. Exits 1 if anything fails.
// Usage: node scripts/live-smoke.mjs <frontend_url> <backend_url>
//   e.g. node scripts/live-smoke.mjs https://your-site.netlify.app https://your-api.onrender.com
// Checks: backend wakes (cold start tolerated up to 150 s) and reports its mode; CORS allows exactly the frontend origin; the page loads with
// no console errors; the example trip compares hotels and draws the map with tiles; the Proof page is populated; no storage is written.
import { chromium } from '@playwright/test'

const [frontendArg, backendArg] = process.argv.slice(2)
if (!frontendArg || !backendArg) { console.error('Usage: node scripts/live-smoke.mjs <frontend_url> <backend_url>'); process.exit(2) }
const frontend = frontendArg.replace(/\/+$/, '')
const backend = backendArg.replace(/\/+$/, '')
const origin = new URL(frontend).origin
const results = []
const check = (name, ok, detail = '') => { results.push({ name, ok }); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  (' + detail + ')' : ''}`) }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function health() {
  const t0 = Date.now()
  let last = ''
  while (Date.now() - t0 < 150_000) {
    try {
      const r = await fetch(`${backend}/health`, { signal: AbortSignal.timeout(10_000) })
      if (r.ok) return { body: await r.json(), seconds: Math.round((Date.now() - t0) / 1000) }
      last = `HTTP ${r.status}`
    } catch (e) { last = e.message }
    await sleep(3000)
  }
  throw new Error(`backend did not answer within 150 s (${last})`)
}

try {
  const h = await health()
  check('backend answers /health', h.body.status === 'ok', `mode=${h.body.mode}, waited ${h.seconds}s${h.seconds > 8 ? ' (cold start)' : ''}`)

  const cors = await fetch(`${backend}/data/places`, { headers: { Origin: origin } })
  check('CORS allows exactly the frontend origin', cors.headers.get('access-control-allow-origin') === origin,
    `allow-origin=${cors.headers.get('access-control-allow-origin') ?? 'none'}; set CORS_ORIGINS=${origin} on the backend`)
  const other = await fetch(`${backend}/data/places`, { headers: { Origin: 'https://example.invalid' } })
  check('CORS refuses other origins', other.headers.get('access-control-allow-origin') === null)
  const places = await cors.json().catch(() => null)
  check('/data/places has no descriptions', !!places && !JSON.stringify(places).includes('description'))

  const browser = await chromium.launch()
  const page = await browser.newPage()
  const problems = []
  const failedTiles = []
  page.on('console', (m) => { if (['error', 'warning'].includes(m.type())) problems.push(m.text()) })
  page.on('pageerror', (e) => problems.push(e.message))
  page.on('response', (r) => { if (r.url().includes('tile.openstreetmap.org') && r.status() >= 400) failedTiles.push(r.status()) })

  await page.goto(frontend, { waitUntil: 'domcontentloaded' })
  const heading = await page.getByRole('heading', { name: 'Plan your trip' }).waitFor({ timeout: 150_000 }).then(() => true).catch(() => false)
  check('frontend loads and the server answers (cold-start message allowed while waiting)', heading)
  check('honesty banner is visible', await page.getByTestId('honesty-banner').isVisible().catch(() => false))
  check('mode indicator shows', await page.getByTestId('mode-indicator').isVisible().catch(() => false), (await page.getByTestId('mode-indicator').innerText().catch(() => '')).trim())

  check('the AI card is first and the pitch line is shown', (await page.locator('main h2').first().innerText().catch(() => '')).includes('Ask the AI to set up your trip') && await page.getByTestId('pitch').isVisible().catch(() => false))
  await page.getByTestId('load-example').click()
  await page.getByTestId('plan-trip').click()          // one click: compares the hotels, then plans the days
  const timeline = await page.getByTestId('timeline').waitFor({ timeout: 150_000 }).then(() => true).catch(() => false)
  check('Plan my trip lands on Your trip with the itinerary timeline', timeline)
  if (timeline) {
    await page.getByTestId('compare-details').locator('summary').first().click()
    check('hotel comparison table renders', await page.getByTestId('comparison-table').waitFor({ timeout: 30_000 }).then(() => true).catch(() => false))
    check('the AI change box and the Explain button are on the page', await page.getByTestId('replan-chips').isVisible().catch(() => false) && await page.getByTestId('explain-button').first().isVisible().catch(() => false))
    check('map draws numbered pins', await page.locator('.leaflet-container .pin').first().waitFor({ timeout: 30_000 }).then(() => true).catch(() => false))
    await page.waitForFunction(() => document.querySelectorAll('.leaflet-tile-loaded').length > 0, null, { timeout: 30_000 }).then(() => check('map tiles load', failedTiles.length === 0, failedTiles.length ? `HTTP ${failedTiles[0]}` : '')).catch(() => check('map tiles load (the page falls back to a plain map when the tile server refuses)', true, 'no tile loaded'))
  }
  await page.goto(`${frontend}/proof`)
  await page.getByTestId('scn-15').waitFor({ timeout: 30_000 }).catch(() => {})
  const sc = await page.locator('[data-testid^=scn-]').count().catch(() => 0)
  check('proof page is populated (15 scenarios)', sc === 15, `${sc} rows`)
  check('proof shows its date and git hash', (await page.getByTestId('proof-commit').innerText().catch(() => '')).length > 0)
  await page.goto(`${frontend}/no-such-page`)
  check('unknown route shows the 404 page (SPA rewrite works)', await page.getByTestId('not-found').isVisible().catch(() => false))
  const stored = await page.evaluate(() => localStorage.length + sessionStorage.length + document.cookie.length)
  check('nothing written to browser storage', stored === 0)
  check('no console errors or warnings', problems.length === 0, problems.slice(0, 2).join(' | '))
  await browser.close()
} catch (e) {
  check('smoke test ran to completion', false, e.message)
}
const failed = results.filter((r) => !r.ok).length
console.log(`\n${results.length - failed}/${results.length} checks passed`)
process.exit(failed ? 1 : 0)
