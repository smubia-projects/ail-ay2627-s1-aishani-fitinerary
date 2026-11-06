// Produces named, consistent PNGs in docs/screenshots/ from the REAL backend (sample mode) and a production build of the frontend.
// Usage (from frontend/):  node scripts/take-screenshots.mjs [--offline-tiles]
// It starts its own servers (backend :8010, frontend preview :4180), so nothing needs to be running. Map tiles come from OpenStreetMap
// when the network allows; with --offline-tiles (or if tiles fail) a blank tile is used and the screenshot map background is empty.
import { spawn } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'

const here = dirname(fileURLToPath(import.meta.url))
const FRONT = resolve(here, '..')
const BACK = resolve(FRONT, '../backend')
const OUT = resolve(FRONT, '../docs/screenshots')
const API = 'http://localhost:8010'
const APP = 'http://localhost:4180'
const offlineTiles = process.argv.includes('--offline-tiles')
const repeatArg = process.argv.find((a) => a.startsWith('--repeat='))
const repeat = repeatArg ? Number(repeatArg.split('=')[1]) : 1
mkdirSync(OUT, { recursive: true })

const procs = []
const run = (cmd, cwd, env = {}) => { const p = spawn(cmd, { cwd, shell: true, env: { ...process.env, ...env }, stdio: 'ignore', detached: true }); procs.push(p); return p }
const stopAll = () => procs.forEach((p) => { try { process.kill(-p.pid) } catch { /* already gone */ } })
async function waitFor(url, ms = 90_000) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { try { if ((await fetch(url)).ok) return } catch { /* not up yet */ } await new Promise((r) => setTimeout(r, 400)) }
  throw new Error(`timed out waiting for ${url}`)
}

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64')
const diagnostics = []
const shot = async (page, name, opts = {}) => {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await page.screenshot({ path: join(OUT, name), ...opts })
      console.log('wrote', name)
      return
    } catch (e) {
      diagnostics.push({ name, attempt, error: e.message, url: page.url() })
      if (attempt === 3) throw e
      await page.waitForTimeout(500 * attempt)
    }
  }
}

async function newPage(browser, viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  if (offlineTiles) await page.route('https://tile.openstreetmap.org/**', (r) => r.fulfill({ status: 200, contentType: 'image/png', body: PNG }))
  return page
}
const tilesLoaded = (page) => page.waitForFunction(() => !document.querySelector('.leaflet-container') || document.querySelectorAll('.leaflet-tile-loaded').length > 0 && !document.querySelector('.leaflet-tile:not(.leaflet-tile-loaded)'), null, { timeout: 15_000 }).catch(() => {})
const settle = async (page) => { await page.waitForLoadState('domcontentloaded').catch(() => {}); await tilesLoaded(page); await page.waitForTimeout(250) }
const waitSensitivityDone = (page, name) => waitVisible(page, page.getByTestId('sensitivity'), name, 70_000).catch(() => {})
async function waitVisible(page, locator, name, timeout = 60_000) {
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      await locator.waitFor({ state: 'visible', timeout })
      return
    } catch (e) {
      diagnostics.push({ name, attempt, error: e.message, url: page.url() })
      if (attempt === 3) throw e
      await page.screenshot({ path: join(OUT, `.diagnostic-${name}-${attempt}.png`), fullPage: true }).catch(() => {})
      await page.waitForTimeout(1000 * attempt)
    }
  }
}
/** Full-page capture without relying on Playwright's resize-and-capture (which changes the map size while tiles are loading). */
async function fullShot(page, name) {
  const w = page.viewportSize().width
  const h = await page.evaluate(() => Math.ceil(document.documentElement.scrollHeight))
  await page.setViewportSize({ width: w, height: h })
  await settle(page)
  await shot(page, name)
}

async function runScreenshots(runIndex = 1) {
  const browser = await chromium.launch()

  // 1 create trip
  let page = await newPage(browser, { width: 1280, height: 900 })
  await page.goto(APP)
  await waitVisible(page, page.getByRole('heading', { name: 'Plan your trip' }), 'create-trip-heading')
  await settle(page)
  await fullShot(page, '01-create-trip.png')

  // 2 wishlist (a few choices made so the states are visible)
  await page.getByTestId('next-wishlist').click()
  await waitVisible(page, page.getByTestId('card-senso_ji'), 'wishlist-card')
  await page.getByTestId('card-senso_ji').locator('label', { hasText: 'Must visit' }).click()
  await page.getByTestId('card-meiji_jingu').locator('label', { hasText: 'Must visit' }).click()
  await page.getByTestId('card-shibuya_sky').locator('label', { hasText: 'Not interested' }).click()
  await settle(page)
  await fullShot(page, '02-wishlist.png')

  // 3 hotel comparison (example trip, wait for the sensitivity section)
  await page.goto(APP)
  await page.getByTestId('load-example').click()
  await waitVisible(page, page.getByTestId('comparison-table'), 'comparison-table')
  await waitVisible(page, page.getByTestId('sensitivity'), 'sensitivity', 60_000)
  await settle(page)
  await fullShot(page, '03-hotel-comparison.png')

  // 4 + 5 itinerary with map, feasibility report
  await page.locator('button.btn-primary', { hasText: 'Use this hotel' }).click()
  await waitVisible(page, page.getByTestId('timeline'), 'timeline')
  await waitVisible(page, page.locator('.leaflet-container .pin').first(), 'map-pin')
  await settle(page)
  await fullShot(page, '04-itinerary-map.png')
  await page.getByTestId('feasibility-report').scrollIntoViewIfNeeded()
  await shot(page, '05-feasibility-report.png', { clip: await page.getByTestId('feasibility-report').boundingBox() })

  // 6 replan diff: queue a lock and the removal of an optional sight, apply, and capture the diff (its warnings include "Not like-for-like")
  await page.goto(APP)
  await page.getByTestId('load-example').click()
  await waitVisible(page, page.getByTestId('comparison-table'), 'comparison-table-replan')
  await waitSensitivityDone(page, 'sensitivity-replan')
  await page.locator('button.btn-primary', { hasText: 'Use this hotel' }).click()
  await waitVisible(page, page.getByTestId('timeline'), 'timeline-replan')
  await page.locator('[data-testid^=stop-]').first().getByTestId('act-lock').click()
  await page.locator('[data-testid^=stop-]').filter({ hasNot: page.locator('.pill-must') }).first().getByTestId('act-remove').click()
  await page.getByTestId('apply-changes').click()
  await waitVisible(page, page.getByTestId('diff-view'), 'diff-view')
  await settle(page)
  await page.getByTestId('diff-view').scrollIntoViewIfNeeded()
  await shot(page, '06-replan-diff.png', { clip: await page.getByTestId('diff-view').boundingBox() })

  // 10 natural-language panel in sample mode (calm message, form still usable)
  await page.goto(APP)
  await page.getByLabel(/For example: "3 days/).fill('3 days, relaxed pace, about 20,000 yen, I love shrines')
  await page.getByTestId('nl-extract').click()
  await waitVisible(page, page.getByTestId('nl-unavailable'), 'nl-unavailable')
  await settle(page)
  await shot(page, '10-nl-parse-sample-mode.png', { clip: await page.getByTestId('nl-panel').boundingBox() })

  // 8 proof page, 11 how-it-works page
  await page.goto(`${APP}/proof`)
  await waitVisible(page, page.getByTestId('scn-15'), 'proof-scenario')
  await fullShot(page, '08-proof.png')
  await page.goto(`${APP}/how-it-works`)
  await waitVisible(page, page.getByTestId('architecture'), 'how-it-works-architecture')
  await fullShot(page, '11-how-it-works.png')

  // 7 impossible trip -> conflict panel
  await page.goto(APP)
  await page.getByTestId('load-impossible').click()
  await waitVisible(page, page.getByTestId('conflict-panel'), 'conflict-panel')
  await settle(page)
  await fullShot(page, '07-impossible-conflict.png')

  // 9 mobile itinerary (375 px)
  const mobile = await newPage(browser, { width: 375, height: 812 })
  await mobile.goto(APP)
  await mobile.getByTestId('load-example').click()
  await waitSensitivityDone(mobile, 'mobile-sensitivity')
  await mobile.locator('button.btn-primary', { hasText: 'Use this hotel' }).click()
  await waitVisible(mobile, mobile.getByTestId('timeline'), 'mobile-timeline')
  await waitVisible(mobile, mobile.locator('.leaflet-container .pin').first(), 'mobile-map-pin')
  await settle(mobile)
  await fullShot(mobile, '09-mobile-itinerary.png')

  // screen recording of the core flow (Playwright video capture): example -> compare -> itinerary -> lock + remove -> diff -> impossible trip
  const videoDir = resolve(OUT, '.video-tmp')
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, reducedMotion: 'reduce', recordVideo: { dir: videoDir, size: { width: 1280, height: 800 } } })
  const rp = await ctx.newPage()
  const t0 = Date.now()
  const beat = (ms = 1100) => rp.waitForTimeout(ms)
  await rp.goto(APP); await beat(1500)
  await rp.getByTestId('load-example').click()
  await waitVisible(rp, rp.getByTestId('comparison-table'), 'video-comparison'); await beat(2500)
  await waitSensitivityDone(rp, 'video-sensitivity'); await beat(1500)
  await rp.locator('button.btn-primary', { hasText: 'Use this hotel' }).click()
  await waitVisible(rp, rp.getByTestId('timeline'), 'video-timeline'); await waitVisible(rp, rp.locator('.leaflet-container .pin').first(), 'video-map-pin'); await beat(3000)
  await rp.locator('[data-testid^=stop-]').first().getByTestId('act-lock').click(); await beat()
  await rp.locator('[data-testid^=stop-]').filter({ hasNot: rp.locator('.pill-must') }).first().getByTestId('act-remove').click(); await beat()
  await rp.getByTestId('apply-changes').click()
  await waitVisible(rp, rp.getByTestId('diff-view'), 'video-diff'); await rp.getByTestId('diff-view').scrollIntoViewIfNeeded(); await beat(4000)
  await rp.goto(APP); await beat(800)
  await rp.getByTestId('load-impossible').click()
  await waitVisible(rp, rp.getByTestId('conflict-panel'), 'video-conflict'); await beat(4000)
  const seconds = Math.round((Date.now() - t0) / 1000)
  const video = rp.video()
  await ctx.close()
  await video.saveAs(join(OUT, 'demo-core-flow.webm'))
  rmSync(videoDir, { recursive: true, force: true })
  console.log(`wrote demo-core-flow.webm (about ${seconds} s)`)
  await browser.close()
  diagnostics.push({ run: runIndex, status: 'pass' })
}

try {
  run('.venv/bin/uvicorn app.main:app --port 8010 --log-level warning', BACK, { RATE_LIMIT_EXPENSIVE_PER_MIN: '500', RATE_LIMIT_PER_MIN: '1000', RATE_LIMIT_CHEAP_PER_MIN: '5000', OPENROUTER_API_KEY: '', LLM_MODEL: '', CORS_ORIGINS: APP, SOLVER_CONCURRENCY: '1' })
  run(`node scripts/sync-proof.mjs && VITE_API_BASE=${API} npx vite build --outDir dist-shots && npx vite preview --outDir dist-shots --port 4180 --strictPort`, FRONT)
  await waitFor(`${API}/health`)
  await waitFor(APP, 120_000)
  for (let i = 1; i <= repeat; i++) await runScreenshots(i)
  writeFileSync(join(OUT, 'screenshot-run-report.json'), JSON.stringify({ repeat, diagnostics }, null, 2) + '\n')
} finally {
  stopAll()
}
