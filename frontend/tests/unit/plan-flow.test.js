// The two-screen flow's rules, pinned against a scripted fetch: one solver request at a time, the last good result survives every failure,
// busy and rate limits count down and never retry by themselves, the slow analysis runs only on a click.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import BusyNotice from '../../src/components/BusyNotice.vue'
import PlanProgress from '../../src/components/PlanProgress.vue'
import { roughPreview } from '../../src/lib/preview.js'
import { attachBaseLayer, PROBE_URL, resetTileProbe, tilesAvailable } from '../../src/lib/tiles.js'
import { cancelPlanning, planMyTrip, resetAll, runSensitivity, store, switchHotel, solverBusy, solverBusySeconds } from '../../src/store.js'
import { ApiError } from '../../src/api/client.js'
import { errBody, res } from './helpers.js'
import places from '../fixtures/places.json'
import compare from '../fixtures/compare-differs-true.json'
import feasible from '../fixtures/optimise-feasible.json'

const HOTELS = ['hotel_kitsune_shinjuku', 'hotel_lantern_asakusa', 'hotel_harbour_odaiba']
function seed() {
  store.server = 'ready'
  store.city = { id: 'tokyo', display_name: 'Tokyo', country: 'Japan', currency_code: 'JPY', display_currency_code: 'SGD', sample_fx_to_display_currency: 1 / 110, counts: {} }
  store.cityId = 'tokyo'
  store.cities = [store.city]
  store.fx = { currency_code: 'JPY', display_currency_code: 'SGD', rate: 1 / 110 }
  store.places = places.attractions
  store.placesById = Object.fromEntries(places.attractions.map((p) => [p.id, p]))
  store.hotelsById = Object.fromEntries(places.hotels.map((h) => [h.id, h]))
}

/** Scripted fetch: `script` maps a route to a handler. Tracks how many requests are in flight at once and keeps every call. */
function scripted(script) {
  const state = { calls: [], inflight: 0, maxInflight: 0 }
  const fn = vi.fn(async (url, init) => {
    const route = String(url).replace(/^.*?(?=\/(compare-hotels|optimise|health))/, '')
    const body = init?.body ? JSON.parse(init.body) : null
    state.calls.push({ route, body })
    state.inflight += 1
    state.maxInflight = Math.max(state.maxInflight, state.inflight)
    try { return await script[route](body, state) } finally { state.inflight -= 1 }
  })
  return { fn, state }
}
const ok = (body) => async () => res(200, { city_id: 'tokyo', ...body })
const delayed = (ms, body) => async () => { await new Promise((r) => setTimeout(r, ms)); return res(200, { city_id: 'tokyo', ...body }) }
const planFor = (hotel) => ({ ...feasible, result: { ...feasible.result, hotel_id: hotel } })

beforeEach(() => { resetAll(); seed(); store.rateLimitUntil = 0; store.solverBusyUntil = 0; store.now = Date.now() })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); resetTileProbe() })

describe('Plan my trip is one strictly sequential chain', () => {
  it('compares (without the slow analysis), plans the best hotel, never has two solver requests in flight', async () => {
    const { fn, state } = scripted({ '/compare-hotels': delayed(15, compare), '/optimise': delayed(15, feasible) })
    vi.stubGlobal('fetch', fn)
    const stages = []
    const stop = setInterval(() => stages.push(store.stage), 5)
    const okResult = await planMyTrip()
    clearInterval(stop)
    expect(okResult).toBe(true)
    expect(state.calls.map((c) => c.route)).toEqual(['/compare-hotels', '/optimise'])
    expect(state.calls[0].body.include_sensitivity).toBe(false)     // the slow analysis never runs by itself
    expect(state.calls[1].body.hotel_id).toBe(compare.best_by_true_cost)
    expect(state.maxInflight).toBe(1)
    expect(store.chosenHotel).toBe(compare.best_by_true_cost)
    expect(store.plan).toBeTruthy()
    expect(store.comparison.best_by_true_cost).toBe(compare.best_by_true_cost)
    expect(stages).toContain('comparing')
    expect(stages).toContain('planning')
    expect(store.stage).toBeNull()
  })

  it('a second press while a plan is running is ignored (one request chain per tab)', async () => {
    const { fn, state } = scripted({ '/compare-hotels': delayed(30, compare), '/optimise': delayed(10, feasible) })
    vi.stubGlobal('fetch', fn)
    const first = planMyTrip()
    expect(solverBusy.value).toBe(true)
    expect(await planMyTrip()).toBe(false)
    await first
    expect(state.calls.filter((c) => c.route === '/compare-hotels')).toHaveLength(1)
    expect(state.maxInflight).toBe(1)
  })

  it('an infeasible trip is a normal plan result: it is planned for the first hotel and shown, never treated as an error', async () => {
    const infeasibleCompare = { ...compare, best_by_true_cost: null, results: compare.results.map((r) => ({ ...r, feasible: false })) }
    const { fn } = scripted({ '/compare-hotels': ok(infeasibleCompare), '/optimise': ok({ ...feasible, result: { ...feasible.result, status: 'infeasible', conflicts: [{ code: 'x', message: 'no', attraction_ids: [] }] } }) })
    vi.stubGlobal('fetch', fn)
    expect(await planMyTrip()).toBe(true)
    expect(store.chosenHotel).toBe(HOTELS[0])
    expect(store.plan.result.status).toBe('infeasible')
    expect(store.planError).toBeNull()
  })
})

describe('the last good result survives every failure', () => {
  async function withGoodPlan() {
    const { fn } = scripted({ '/compare-hotels': ok(compare), '/optimise': ok(planFor('hotel_lantern_asakusa')) })
    vi.stubGlobal('fetch', fn)
    expect(await planMyTrip()).toBe(true)
    return { plan: store.plan, comparison: store.comparison, hotel: store.chosenHotel }
  }

  it('423 solver_busy: plan and comparison stay, a countdown starts, and NOTHING is retried automatically', async () => {
    const good = await withGoodPlan()
    vi.useFakeTimers()
    const { fn, state } = scripted({ '/compare-hotels': async () => res(423, errBody('solver_busy', 'busy', { retry_after_seconds: 7 }), { 'retry-after': '7' }), '/optimise': ok(feasible) })
    vi.stubGlobal('fetch', fn)
    expect(await planMyTrip()).toBe(false)
    expect(store.comparisonError.code).toBe('solver_busy')
    expect(store.plan).toBe(good.plan)
    expect(store.comparison).toBe(good.comparison)
    expect(store.chosenHotel).toBe(good.hotel)
    expect(solverBusySeconds.value).toBe(7)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(state.calls).toHaveLength(1)                                 // no automatic retry, however long we wait
    expect(solverBusySeconds.value).toBe(0)
  })

  it('423 on the planning step AFTER a successful comparison: the previous plan, hotel and comparison are all untouched', async () => {
    const good = await withGoodPlan()
    const { fn } = scripted({ '/compare-hotels': ok({ ...compare, best_by_true_cost: HOTELS[2] }), '/optimise': async () => res(423, errBody('solver_busy', 'busy', { retry_after_seconds: 4 }), { 'retry-after': '4' }) })
    vi.stubGlobal('fetch', fn)
    expect(await planMyTrip()).toBe(false)
    expect(store.planError.code).toBe('solver_busy')
    expect(store.plan).toBe(good.plan)
    expect(store.chosenHotel).toBe(good.hotel)
    expect(store.comparison).toBe(good.comparison)          // the new comparison is committed together with its plan, or not at all
    expect(store.stage).toBeNull()
  })

  it('423 while switching hotel: the old hotel and plan stay selected; the retry is manual and works', async () => {
    const good = await withGoodPlan()
    let n = 0
    const { fn } = scripted({ '/optimise': async () => (++n === 1 ? res(423, errBody('solver_busy', 'busy', { retry_after_seconds: 3 }), { 'retry-after': '3' }) : res(200, { city_id: 'tokyo', ...planFor(HOTELS[2]) })) })
    vi.stubGlobal('fetch', fn)
    await switchHotel(HOTELS[2])
    expect(store.planError.code).toBe('solver_busy')
    expect(store.chosenHotel).toBe(good.hotel)
    expect(store.plan).toBe(good.plan)
    expect(store.pendingHotel).toBe(HOTELS[2])
    await switchHotel(store.pendingHotel)                                // the Retry button
    expect(store.chosenHotel).toBe(HOTELS[2])
    expect(store.planError).toBeNull()
    expect(store.pendingHotel).toBeNull()
  })

  it('429: plan kept, rate-limit countdown set, the action is refused until it ends', async () => {
    const good = await withGoodPlan()
    const { fn, state } = scripted({ '/optimise': async () => res(429, errBody('rate_limited', 'slow down', { retry_after_seconds: 5 }), { 'retry-after': '5' }) })
    vi.stubGlobal('fetch', fn)
    await switchHotel(HOTELS[2])
    expect(store.plan).toBe(good.plan)
    expect(store.rateLimitUntil).toBeGreaterThan(Date.now())
    const before = state.calls.length
    await switchHotel(HOTELS[0])                                          // refused while limited: no request
    expect(state.calls.length).toBe(before)
  })

  it('cancel aborts the chain and commits nothing', async () => {
    const good = await withGoodPlan()
    const { fn } = scripted({ '/compare-hotels': delayed(40, compare), '/optimise': ok(feasible) })
    vi.stubGlobal('fetch', fn)
    const run = planMyTrip()
    cancelPlanning()
    expect(await run).toBe(false)
    expect(store.stage).toBeNull()
    expect(store.plan).toBe(good.plan)
  })
})

describe('hotel switch and the slow analysis', () => {
  it('switching re-plans in place: the old plan is still there while the request runs', async () => {
    const { fn } = scripted({ '/compare-hotels': ok(compare), '/optimise': delayed(25, planFor(HOTELS[2])) })
    vi.stubGlobal('fetch', fn)
    await planMyTrip()
    const old = store.plan
    const p = switchHotel(HOTELS[2])
    expect(store.plan).toBe(old)
    expect(store.busy.optimise).toBe(true)
    expect(store.stage).toBe('planning')
    await p
    expect(store.plan.result.hotel_id).toBe(HOTELS[2])
    expect(store.chosenHotel).toBe(HOTELS[2])
  })

  it('the value-of-an-hour analysis runs only when called, once, and not while another solve is running', async () => {
    const withSens = { ...compare, sensitivity: { grid: [], flips: [], solves: 3, flip_summary: 'x', approximate: false, warnings: [] } }
    const { fn, state } = scripted({ '/compare-hotels': async (b) => res(200, { city_id: 'tokyo', ...(b.include_sensitivity ? withSens : compare) }), '/optimise': ok(feasible) })
    vi.stubGlobal('fetch', fn)
    await planMyTrip()
    expect(state.calls.some((c) => c.body?.include_sensitivity)).toBe(false)
    const a = runSensitivity()
    const b = runSensitivity()                                            // second call while the first runs: refused
    await Promise.all([a, b])
    expect(state.calls.filter((c) => c.body?.include_sensitivity)).toHaveLength(1)
    expect(state.maxInflight).toBe(1)
    expect(store.comparison.sensitivity).toBeTruthy()
  })
})

describe('components announce what is happening', () => {
  it('PlanProgress names the real stages in a polite live region and offers Cancel', async () => {
    store.stage = 'comparing'
    const w = mount(PlanProgress)
    const box = w.get('[data-testid=plan-progress]')
    expect(box.attributes('role')).toBe('status')
    expect(box.attributes('aria-live')).toBe('polite')
    expect(box.text()).toContain('Comparing hotels…')
    expect(box.text()).toContain('Planning your days and checking the plan…')
    expect(box.get('li[aria-current=step]').text()).toContain('Comparing hotels')
    store.stage = 'planning'
    await flushPromises()
    expect(box.get('li[aria-current=step]').text()).toContain('Planning your days')
    expect(w.find('[data-testid=cancel-plan]').exists()).toBe(true)
    store.stage = null
    await flushPromises()
    expect(w.find('[data-testid=plan-progress]').exists()).toBe(false)
  })

  it('BusyNotice: friendly sentence, countdown, Retry disabled until it ends and never fired by itself', async () => {
    store.solverBusyUntil = Date.now() + 5000
    store.now = Date.now()
    const w = mount(BusyNotice, { props: { error: new ApiError({ code: 'solver_busy', message: 'busy', retryAfter: 5 }) } })
    expect(w.get('[data-testid=solver-busy]').text()).toContain('Another plan is still being calculated.')
    expect(w.get('[data-testid=solver-busy]').text()).toContain('Try again in a few seconds.')
    expect(w.get('[data-testid=solver-busy]').attributes('aria-live')).toBe('polite')
    expect(w.get('[data-testid=busy-countdown]').text()).toMatch(/retry in [1-5] s/)
    expect(w.get('[data-testid=busy-retry]').attributes('disabled')).toBeDefined()
    store.now = Date.now() + 6000
    await flushPromises()
    expect(w.get('[data-testid=busy-countdown]').text()).toContain('retry now')
    expect(w.get('[data-testid=busy-retry]').attributes('disabled')).toBeUndefined()
    expect(w.emitted('retry')).toBeUndefined()
    await w.get('[data-testid=busy-retry]').trigger('click')
    expect(w.emitted('retry')).toHaveLength(1)
  })

  it('BusyNotice shows nothing for other errors', () => {
    const w = mount(BusyNotice, { props: { error: new ApiError({ code: 'timeout', message: 'x' }) } })
    expect(w.find('[data-testid=solver-busy]').exists()).toBe(false)
  })
})

describe('rough preview (computed in the browser, no request)', () => {
  it('counts must / include-if-it-fits, excludes "not interested", sums only KNOWN admissions and lists unknown prices', () => {
    store.form.days = 1
    store.form.pace = 'relaxed'
    store.wishlist = { senso_ji: 'must', akihabara: 'skip', teamlab_planets: 'must' }
    const p = roughPreview(store)
    const all = places.attractions
    expect(p.mustCount).toBe(2)
    expect(p.maybeCount).toBe(all.length - 3)
    expect(p.total).toBe(all.length - 1)
    expect(p.maxFit).toBe(2)                                              // relaxed = 2 a day x 1 day (same cap the planner uses)
    const know = (id) => all.find((a) => a.id === id).admission_cost
    expect(p.mustAdmissions).toBe((know('senso_ji') ?? 0) + (know('teamlab_planets') ?? 0))
    expect(p.mustUnknown).toContain(all.find((a) => a.id === 'teamlab_planets').name)  // unknown is listed, never counted as 0
    expect(p.areas.length).toBeGreaterThan(0)
    expect(p.mustOverCap).toBe(false)
    store.wishlist = { senso_ji: 'must', teamlab_planets: 'must', tokyo_skytree: 'must' }
    expect(roughPreview(store).mustOverCap).toBe(true)
  })
  it('makes no network request', () => {
    const f = vi.fn(); vi.stubGlobal('fetch', f)
    roughPreview(store)
    expect(f).not.toHaveBeenCalled()
  })
})

describe('map tiles degrade to a plain background', () => {
  const probeWith = (impl) => tilesAvailable({ fetchImpl: impl, force: true })
  it('is available when the tile server answers 200, with the Referer policy that OpenStreetMap requires', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200 }))
    expect(await probeWith(f)).toBe(true)
    expect(f.mock.calls[0][0]).toBe(PROBE_URL)
    expect(f.mock.calls[0][1]).toMatchObject({ referrerPolicy: 'origin', credentials: 'omit', mode: 'cors' })
  })
  it('is NOT available on 403 (the "Access blocked" picture), 429 or a network error', async () => {
    expect(await probeWith(async () => ({ ok: false, status: 403 }))).toBe(false)
    expect(await probeWith(async () => ({ ok: false, status: 429 }))).toBe(false)
    expect(await probeWith(async () => { throw new TypeError('Failed to fetch') })).toBe(false)
  })
  it('a slow answer is not treated as a refusal', async () => {
    const never = (_u, { signal }) => new Promise((_r, rej) => signal.addEventListener('abort', () => rej(new DOMException('aborted', 'AbortError'))))
    expect(await tilesAvailable({ fetchImpl: never, timeoutMs: 20, force: true })).toBe(true)
  })
  it('remembers the answer so several maps do not each probe', async () => {
    const f = vi.fn(async () => ({ ok: true, status: 200 }))
    await tilesAvailable({ fetchImpl: f, force: true })
    await tilesAvailable({ fetchImpl: f })
    expect(f).toHaveBeenCalledTimes(1)
  })
  it('attachBaseLayer reports "unavailable" and adds no tile layer when the probe says no', async () => {
    const map = { addLayer: vi.fn(), removeLayer: vi.fn(), _container: {} }
    const onUnavailable = vi.fn()
    attachBaseLayer(map, { onUnavailable, probe: async () => false })
    await flushPromises()
    expect(onUnavailable).toHaveBeenCalledTimes(1)
    expect(map.addLayer).not.toHaveBeenCalled()
  })
})
