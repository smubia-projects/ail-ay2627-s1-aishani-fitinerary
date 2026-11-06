import { tripSection, setupSection, disclose, allDays, mapView, timelineView } from './helpers.js'
// Phase 9f: the two-screen flow, measured and exercised in a real browser (real backend in sample mode, offline tiles).
import { execSync } from 'node:child_process'
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from '@playwright/test'
import { expectClean, planFromExample, PLAN_WAIT, stubTiles, watchConsole } from './helpers.js'

test.beforeEach(async ({ page }) => { await stubTiles(page) })
const CITIES = ['tokyo', 'seoul']
const WIDTHS = [320, 375, 768, 1280]
const noSideScroll = (page) => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)

test('MEASURE: clicks and time from landing to the first plan with the example (written to docs/ux-metrics.md)', async ({ page }) => {
  const rows = []
  for (const city of CITIES) {
    await page.goto('/')
    await page.getByTestId(`city-${city}`).click()          // choosing the city is not counted: Tokyo is the default and the example follows the selected city
    await expect(page.getByTestId('city-chip')).toContainText(city === 'tokyo' ? 'Tokyo' : 'Seoul')
    let clicks = 0
    const click = async (id) => { clicks += 1; await page.getByTestId(id).click() }
    const t0 = Date.now()
    clicks += 1; await disclose(page, 'worked-examples')
    await click('load-example')
    await click('plan-trip')
    await timelineView(page)
    await expect(page.getByTestId('timeline')).toBeVisible({ timeout: PLAN_WAIT })
    rows.push({ city, clicks, ms: Date.now() - t0 })
    expect(clicks).toBeLessThanOrEqual(3)
  }
  let git = 'unknown'
  try { git = execSync('git rev-parse --short HEAD', { cwd: resolve(import.meta.dirname, '..', '..', '..') }).toString().trim() } catch { /* not a git checkout */ }
  const out = ['# UX metrics (measured)', '',
    `Generated ${new Date().toISOString()} by \`frontend/tests/e2e/flow.spec.js\` at git \`${git}\` (the code may have uncommitted changes). Chromium, production build, real backend in sample mode, local machine.`, '',
    'Clicks are counted from the first interaction on the Plan screen (the city was already chosen) to the plan showing on "Your trip". Time runs from the first of those clicks until the day-by-day timeline is visible.',
    '**The times are machine-dependent** (this machine, its load at that moment, one run): treat them as a rough indication, not a benchmark. Nothing here is a user-test result.', '',
    '| City | Clicks to first plan (target: at most 3) | Time to first plan |', '|---|---|---|',
    ...rows.map((r) => `| ${r.city === 'tokyo' ? 'Tokyo' : 'Seoul'} | ${r.clicks} | ${(r.ms / 1000).toFixed(1)} s |`), '',
    'Path measured: "Explore worked examples", then "Try an example" (fills the form and sights) then "Plan my trip" (compares the hotels, then plans the best one).']
  writeFileSync(resolve(import.meta.dirname, '..', '..', '..', 'docs', 'ux-metrics.md'), out.join('\n') + '\n')
})

test('staged feedback: the progress line names the real stages, then focus moves to the new screen', async ({ page }) => {
  const problems = watchConsole(page)
  await page.route('**/compare-hotels', async (r) => { await new Promise((x) => setTimeout(x, 1200)); await r.continue() })
  await page.route('**/optimise', async (r) => { await new Promise((x) => setTimeout(x, 1200)); await r.continue() })
  await page.goto('/')
  await disclose(page, 'worked-examples')
  await page.getByTestId('load-example').click()
  await page.getByTestId('plan-trip').click()
  const progress = page.getByTestId('plan-progress')
  await expect(progress).toBeVisible()
  await expect(progress).toHaveAttribute('aria-live', 'polite')
  await expect(progress).toContainText('Comparing hotels…')
  await expect(progress.locator('li[aria-current=step]')).toContainText('Comparing hotels')
  await expect(progress.locator('li[aria-current=step]')).toContainText('Planning your days', { timeout: PLAN_WAIT })
  await expect(page.getByTestId('plan-trip')).toBeDisabled()               // one chain at a time
  await expect(page).toHaveURL(/\/trip$/, { timeout: PLAN_WAIT })
  await expect.poll(() => page.evaluate(() => document.activeElement?.id), { message: 'focus moved to the main region of the new screen' }).toBe('main')
  expectClean(problems)
})

test('Cancel stops planning and the page stays on Plan with everything preserved', async ({ page }) => {
  await page.route('**/compare-hotels', async (r) => { await new Promise((x) => setTimeout(x, 4000)); await r.continue().catch(() => {}) })
  await page.goto('/')
  await setupSection(page, 'essentials')
  await page.getByLabel('Days', { exact: true }).fill('2')
  await page.getByTestId('plan-trip').click()
  await expect(page.getByTestId('plan-progress')).toBeVisible()
  await page.getByTestId('cancel-plan').click()
  await expect(page.getByTestId('plan-progress')).toHaveCount(0)
  await expect(page).toHaveURL(/\/$/)
  await setupSection(page, 'essentials')
  await expect(page.getByLabel('Days', { exact: true })).toHaveValue('2')
  await expect(page.getByTestId('plan-trip')).toBeEnabled()
})

test('solver busy (423): friendly message, countdown, manual Retry only, nothing retried by itself, the last plan is kept', async ({ page }) => {
  await page.goto('/')
  await planFromExample(page)
  await allDays(page)
  await timelineView(page)
  await expect(page.getByTestId('timeline')).toBeVisible()
  await page.getByTestId('edit-trip').click()
  let calls = 0
  await page.route('**/compare-hotels', (r) => { calls += 1; return r.fulfill({ status: 423, contentType: 'application/json', headers: { 'retry-after': '3' },
    body: JSON.stringify({ error: { code: 'solver_busy', message: 'busy', details: { retry_after_seconds: 3, active_solves: 1, capacity: 1 }, request_id: 'rb' } }) }) })
  await page.getByTestId('plan-trip').click()
  const busy = page.getByTestId('solver-busy')
  await expect(busy).toBeVisible()
  await expect(busy).toContainText('Another plan is still being calculated.')
  await expect(busy).toContainText('Try again in a few seconds.')
  await expect(page.getByTestId('busy-countdown')).toContainText(/retry in [1-3] s/)
  await expect(page.getByTestId('busy-retry')).toBeDisabled()
  await expect(page.getByTestId('busy-countdown')).toContainText('retry now', { timeout: 10_000 })
  await expect(page.getByTestId('busy-retry')).toBeEnabled()
  expect(calls, 'no automatic retry').toBe(1)
  await expect(page).toHaveURL(/\/$/)
  // the last good plan is still reachable
  await page.getByTestId('back-to-trip').click()
  await timelineView(page)
  await expect(page.getByTestId('timeline')).toBeVisible()
  // and the manual Retry works once the server is free
  await page.getByTestId('edit-trip').click()
  await page.unroute('**/compare-hotels')
  await page.getByTestId('plan-trip').click()
  await expect(page).toHaveURL(/\/trip$/, { timeout: PLAN_WAIT })
  await allDays(page)
})

test('"Plan my trip" says why it is disabled, in plain words, when nothing is chosen', async ({ page }) => {
  const problems = watchConsole(page)
  await page.goto('/')
  await setupSection(page, 'sights')
  const cards = page.locator('article[data-testid^="card-"]')
  await expect(cards.first()).toBeVisible()
  for (let i = 0; i < await cards.count(); i++) await cards.nth(i).locator('label', { hasText: 'Nah' }).click()
  await expect(page.getByTestId('plan-trip')).toBeDisabled()
  await expect(page.getByTestId('plan-reason')).toContainText('Pick at least one sight')
  await expect(page.getByTestId('plan-trip')).toHaveAttribute('aria-describedby', 'plan-reason')
  await cards.first().locator('label', { hasText: 'Must go' }).click()
  await expect(page.getByTestId('plan-trip')).toBeEnabled()
  expectClean(problems)
})

test('rough preview follows the choices with no request, and says it is not a plan', async ({ page }) => {
  const requests = []
  page.on('request', (r) => { if (['/optimise', '/compare-hotels', '/replan', '/parse', '/explain'].some((p) => r.url().includes(p))) requests.push(r.url()) })
  await page.goto('/')
  await setupSection(page, 'sights')
  const preview = page.getByTestId('rough-preview')
  await expect(preview).toContainText('not a plan')
  await setupSection(page, 'sights')
  await expect(page.getByTestId('preview-count')).toContainText('0 must visit')
  await setupSection(page, 'sights')
  await page.getByTestId('card-senso_ji').locator('label', { hasText: 'Must go' }).click()
  await setupSection(page, 'sights')
  await expect(page.getByTestId('preview-count')).toContainText('1 must visit')
  await setupSection(page, 'sights')
  await page.getByTestId('card-teamlab_planets').locator('label', { hasText: 'Must go' }).click()
  await setupSection(page, 'sights')
  await expect(page.getByTestId('preview-unknown')).toContainText('teamLab')       // unknown prices are listed, not counted as zero
  await setupSection(page, 'sights')
  await page.getByTestId('card-akihabara').locator('label', { hasText: 'Nah' }).click()
  await setupSection(page, 'sights')
  await expect(page.getByTestId('wish-counts')).toContainText('2 must visit')
  await setupSection(page, 'essentials')
  await page.getByLabel('Days', { exact: true }).fill('1')
  await setupSection(page, 'essentials')
  await page.getByLabel('Pace', { exact: true }).selectOption('relaxed')
  await setupSection(page, 'sights')
  await expect(page.getByTestId('preview-count')).toContainText('at most 2 sights fit in 1 day')
  await setupSection(page, 'sights')
  await expect(page.getByTestId('preview-areas')).toContainText('Asakusa')
  expect(requests, 'moving choices around never calls the solver or the AI').toEqual([])
})

test('the live map shows pins for the sights still in the running and drops a pin when a sight becomes "Not interested"', async ({ page }) => {
  await page.goto('/')
  await setupSection(page, 'sights')
  await page.getByText('Explore the attraction map').click()
  const pins = page.locator('[data-testid=sights-map] .leaflet-marker-icon')
  await expect(pins.first()).toBeVisible()
  const before = await pins.count()
  await setupSection(page, 'sights')
  await page.getByTestId('card-akihabara').locator('label', { hasText: 'Nah' }).click()
  await expect(pins).toHaveCount(before - 1)
  await setupSection(page, 'sights')
  await page.getByTestId('card-senso_ji').locator('label', { hasText: 'Must go' }).click()
  await expect(page.locator('[data-testid=sights-map] .pin-must')).toHaveCount(1)
  await expect(pins.first()).toHaveAttribute('aria-label', /.+/)
  for (const l of await pins.evaluateAll((els) => els.map((e) => e.getAttribute('aria-label')))) expect(l).toMatch(/must visit|include if it fits/)
})

test('map tiles refused by the tile server: pins stay on a plain background with a small note, never the "Access blocked" pictures', async ({ page }) => {
  await page.route('https://tile.openstreetmap.org/**', (r) => r.fulfill({ status: 403, contentType: 'image/png', headers: { 'access-control-allow-origin': '*' }, body: 'blocked' }))
  await page.goto('/')
  await setupSection(page, 'sights')
  await page.getByText('Explore the attraction map').click()
  await expect(page.getByTestId('tiles-unavailable')).toContainText('Map tiles unavailable')
  await expect(page.locator('[data-testid=sights-map] img.leaflet-tile')).toHaveCount(0)
  await expect(page.locator('[data-testid=sights-map] .leaflet-marker-icon').first()).toBeVisible()
  await expect(page.locator('[data-testid=sights-map] .leaflet-control-attribution')).not.toContainText('OpenStreetMap')
  await planFromExample(page)
  await allDays(page)
  await expect(page.getByTestId('tiles-unavailable')).toBeVisible()
  await expect(page.locator('[data-testid=trip-map] img.leaflet-tile')).toHaveCount(0)
  await expect(page.locator('[data-testid=trip-map] .pin').first()).toBeVisible()
})

for (const city of CITIES) {
  test(`${city}: Plan and Your trip fit 320, 375, 768 and 1280 px with no sideways scroll, a reachable Plan button and no console errors`, async ({ page }) => {
    const problems = watchConsole(page)
    await page.goto('/')
    await page.getByTestId(`city-${city}`).click()
    await setupSection(page, 'sights')
    await expect(page.locator('article[data-testid^="card-"]').first()).toBeVisible()
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 800 })
      expect(await noSideScroll(page), `Plan has no sideways scroll at ${width}px`).toBe(true)
      const bar = await page.getByTestId('plan-trip').boundingBox()
      expect(bar.y + bar.height, `the Plan button is on screen at ${width}px`).toBeLessThanOrEqual(800)
      expect(bar.x, `inside the viewport at ${width}px`).toBeGreaterThanOrEqual(0)
    }
    await page.setViewportSize({ width: 1280, height: 800 })
    await planFromExample(page)
    await allDays(page)
    await timelineView(page)
    await expect(page.getByTestId('timeline')).toBeVisible()
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 800 })
      expect(await noSideScroll(page), `Your trip has no sideways scroll at ${width}px`).toBe(true)
    }
    expectClean(problems)
  })
}

test('keyboard only on the Plan screen: reach the city chips, a sight choice and the Plan button; the disabled reason is announced', async ({ page }) => {
  await page.goto('/')
  const tabTo = async (predicate, max = 400) => {   // the Plan screen has more tab stops now (the AI card, theme tabs, interest chips)
    for (let i = 0; i < max; i++) { await page.keyboard.press('Tab'); if (await page.evaluate(predicate)) return }
    throw new Error('never reached the target with the keyboard')
  }
  await tabTo(() => document.activeElement?.getAttribute('data-testid') === 'city-seoul')
  await expect(page.locator(':focus')).toBeVisible()
  await tabTo(() => document.activeElement?.getAttribute('data-testid') === 'setup-sights')
  await page.keyboard.press('Enter')
  await tabTo(() => document.activeElement?.name?.startsWith('wish-'))    // a radio group is one tab stop (the checked choice)...
  await page.keyboard.press('ArrowLeft')                                   // ...and the arrow keys move within it: "Include if it fits" -> "Must visit"
  await setupSection(page, 'sights')
  await expect(page.getByTestId('wish-counts')).toContainText('1 must visit')
  await tabTo(() => document.activeElement?.getAttribute('data-testid') === 'plan-trip')
  await expect(page.getByTestId('plan-summary')).toHaveAttribute('aria-live', 'polite')
})
