// Every screen of the two-screen flow (Plan idle, Plan with the example chosen, the Food theme tab, Your trip with costs and day cards, Your trip after a hotel switch) for two different cities, on desktop and on a phone, plus the Proof and How it works pages.
// Real backend (sample mode) and a production build. Output: docs/screenshots/steps/<city>-<step>-<desktop|mobile>.png
// Usage (from frontend/):  node scripts/take-step-screenshots.mjs [--offline-tiles]
import { spawn } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'
import { readFileSync } from 'node:fs'

const here = dirname(fileURLToPath(import.meta.url))
const FRONT = resolve(here, '..')
const BACK = resolve(FRONT, '../backend')
const OUT = resolve(FRONT, '../docs/screenshots/steps')
const API = 'http://localhost:8010'
const APP = 'http://localhost:4181'
const offlineTiles = process.argv.includes('--offline-tiles')
const CITIES = ['kuala_lumpur', 'seoul']
const CITY_NAME = { kuala_lumpur: 'Kuala Lumpur', seoul: 'Seoul', tokyo: 'Tokyo' }
const VIEWPORTS = { desktop: { width: 1280, height: 900 }, mobile: { width: 375, height: 812 } }
mkdirSync(OUT, { recursive: true })

const procs = []
const run = (cmd, cwd, env = {}) => { const p = spawn(cmd, { cwd, shell: true, env: { ...process.env, ...env }, stdio: 'ignore', detached: true }); procs.push(p) }
const stopAll = () => procs.forEach((p) => { try { process.kill(-p.pid) } catch { /* already gone */ } })
async function waitFor(url, ms = 120_000) {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) { try { if ((await fetch(url)).ok) return } catch { /* not up yet */ } await new Promise((r) => setTimeout(r, 400)) }
  throw new Error(`timed out waiting for ${url}`)
}
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==', 'base64')
const tilesLoaded = (page) => page.waitForFunction(() => !document.querySelector('.leaflet-container') || (document.querySelectorAll('.leaflet-tile-loaded').length > 0 && !document.querySelector('.leaflet-tile:not(.leaflet-tile-loaded)')), null, { timeout: 15_000 }).catch(() => {})
const wait = (locator, timeout = 60_000) => locator.waitFor({ state: 'visible', timeout })
const written = []
async function shot(page, name) {
  await page.evaluate(() => window.scrollTo(0, 0))
  await tilesLoaded(page)
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(OUT, name), fullPage: true })
  written.push(name)
  console.log('wrote', name)
}

async function flow(browser, city, device) {
  const ctx = await browser.newContext({ viewport: VIEWPORTS[device], deviceScaleFactor: 1, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  if (offlineTiles) await page.route('https://tile.openstreetmap.org/**', (r) => r.fulfill({ status: 200, contentType: 'image/png', body: PNG, headers: { 'access-control-allow-origin': '*' } }))
  const tag = `${city}`
  await page.goto(APP)
  await wait(page.getByTestId('city-chip'))
  if (city !== 'tokyo') { await page.getByTestId(`city-${city}`).click(); await page.getByTestId('city-chip').filter({ hasText: CITY_NAME[city] }).first().waitFor() }
  await wait(page.locator('article[data-testid^="card-"]').first())
  await shot(page, `${tag}-1-plan-${device}.png`)
  await page.getByTestId('load-example').click()
  await wait(page.getByTestId('example-loaded'))
  await shot(page, `${tag}-2-plan-chosen-${device}.png`)
  await page.getByTestId('tab-food').click()
  await shot(page, `${tag}-2b-plan-theme-food-${device}.png`)
  await page.getByTestId('tab-all').click()
  await page.getByTestId('plan-trip').click()
  await wait(page.getByTestId('timeline'))
  await wait(page.locator('.leaflet-container .pin').first())
  await shot(page, `${tag}-3-trip-${device}.png`)
  const other = page.getByTestId('hotel-switch').locator('input:not(:checked):not(:disabled)').first()
  await page.locator(`label[for="${await other.getAttribute('id')}"]`).click()
  await page.getByTestId('plan-progress').waitFor({ state: 'detached', timeout: 60_000 }).catch(() => {})
  await page.getByTestId('compare-details').locator('summary').first().click()
  await wait(page.getByTestId('comparison-table'))
  await shot(page, `${tag}-4-trip-hotel-switched-${device}.png`)
  if (city === CITIES[0]) {
    await page.goto(`${APP}/proof`); await wait(page.getByTestId('scn-15')); await shot(page, `proof-${device}.png`)
    await page.goto(`${APP}/how-it-works`); await wait(page.getByTestId('architecture')); await shot(page, `how-it-works-${device}.png`)
  }
  await ctx.close()
}

// AI-first screens. The backend runs in sample mode (no key), so /health and /parse are answered with CANNED responses built from the city's
// example (clearly named "ai-canned-*"): they show the interface, not a real model's answer. Everything else is the real backend.
const examples = JSON.parse(readFileSync(join(FRONT, 'src/lib/examples.json'), 'utf8'))
const aiTexts = JSON.parse(readFileSync(join(FRONT, 'src/lib/aiExamples.json'), 'utf8'))
const CORS = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': 'GET,POST,OPTIONS' }
const canned = (json) => (r) => (r.request().method() === 'OPTIONS' ? r.fulfill({ status: 204, headers: CORS }) : r.fulfill({ json, headers: CORS }))
async function aiFlow(browser, city, device) {
  const ctx = await browser.newContext({ viewport: VIEWPORTS[device], deviceScaleFactor: 1, reducedMotion: 'reduce' })
  const page = await ctx.newPage()
  const ex = examples[city].valid
  const must = Object.entries(ex.wishlist).filter(([, v]) => v === 'must').map(([k]) => k)
  const skip = Object.entries(ex.wishlist).filter(([, v]) => v === 'skip').map(([k]) => k)
  await page.route('**/health', canned({ status: 'ok', mode: 'llm', llm_configured: true, llm_mode: 'llm', llm_limit_reached: false, llm_endpoint: 'gateway.example.test', llm_relay: true }))
  await page.route('**/parse', canned({ status: 'ok', draft: { days: 3, budget: null, strict_budget: false, pace: 'balanced', interests: ['food'], must_visit: must, skip, day_start: '09:00', day_end: '19:00',
    value_of_time_per_hour: ex.form.valueOfTime, max_walking_minutes_per_day: 90, start_weekday: 2, allow_unknown_hours: false, nights: null, locked: [] },
    raw_input_echo: aiTexts[city].typical + ' I do not want to walk much, I prefer quiet places and I am vegetarian.', extracted_json: { days: 3, must_visit_names: must }, resolved_names: [], unresolved_names: [{ input: 'Moon Base', message: 'Not a place in this dataset, so it cannot be scheduled.', suggestions: [] }],
    ambiguities: [], defaults_applied: ['pace: balanced (not stated)', 'day start: 09:00 (not stated)'], clarifications: [], unclear: ['How busy the places are'], assumptions: {}, call_count: 1, mode: 'llm', prompt_version: 'canned', city_id: city, city_name: CITY_NAME[city] }))
  await page.goto(APP)
  await wait(page.getByTestId('city-chip'))
  if (city !== 'tokyo') { await page.getByTestId(`city-${city}`).click(); await page.getByTestId('city-chip').filter({ hasText: CITY_NAME[city] }).first().waitFor() }
  await wait(page.getByTestId('ex-typical'))
  await page.getByTestId('ex-typical').click()
  await page.getByTestId('nl-extract').click()
  await wait(page.getByTestId('nl-rows'))
  await shot(page, `${city}-ai-canned-1-understood-${device}.png`)
  await page.getByTestId('ai-popover-button').first().click()
  await wait(page.getByTestId('ai-popover'))
  await shot(page, `${city}-ai-canned-2-popover-${device}.png`)
  await page.keyboard.press('Escape')
  await page.getByTestId('plan-trip').click()
  await wait(page.getByTestId('timeline'))
  await wait(page.getByTestId('replan-chips'))
  await shot(page, `${city}-ai-canned-3-trip-${device}.png`)
  await ctx.close()
}

try {
  run('.venv/bin/uvicorn app.main:app --port 8010 --log-level warning', BACK, { RATE_LIMIT_EXPENSIVE_PER_MIN: '500', RATE_LIMIT_PER_MIN: '1000', RATE_LIMIT_CHEAP_PER_MIN: '5000', OPENROUTER_API_KEY: '', LLM_MODEL: '', CORS_ORIGINS: APP, SOLVER_CONCURRENCY: '1' })
  run(`node scripts/sync-proof.mjs && VITE_API_BASE=${API} npx vite build --outDir dist-steps && npx vite preview --outDir dist-steps --port 4181 --strictPort`, FRONT)
  await waitFor(`${API}/health`)
  await waitFor(APP)
  const browser = await chromium.launch()
  for (const city of CITIES) for (const device of Object.keys(VIEWPORTS)) await flow(browser, city, device)
  for (const city of CITIES) for (const device of Object.keys(VIEWPORTS)) await aiFlow(browser, city, device)
  await browser.close()
  console.log(`${written.length} screenshots written to docs/screenshots/steps/`)
} finally {
  stopAll()
}
