import { describe, expect, it } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import ConflictPanel from '../../src/components/ConflictPanel.vue'
import ErrorPanel from '../../src/components/ErrorPanel.vue'
import FeasibilityReport from '../../src/components/FeasibilityReport.vue'
import AttractionCard from '../../src/components/AttractionCard.vue'
import NotScheduledPanel from '../../src/components/NotScheduledPanel.vue'
import DayTimeline from '../../src/components/DayTimeline.vue'
import SourceBadge from '../../src/components/SourceBadge.vue'
import infeasible from '../fixtures/optimise-infeasible.json'
import feasible from '../fixtures/optimise-feasible.json'
import places from '../fixtures/places.json'
import poisoned from '../fixtures/poisoned_attraction.json'

const byId = Object.fromEntries(places.attractions.map((p) => [p.id, p]))
const fx = places.fx

describe('ConflictPanel (infeasible is a result, not an error)', () => {
  const prefs = { days: 2, must_visit: ['akihabara', 'senso_ji'], strict_budget: false }
  const mountIt = () => mount(ConflictPanel, { props: { result: infeasible.result, prefs, placesById: byId } })

  it('lists each specific conflict and uses the info style, never the error style or an alert role', () => {
    const w = mountIt()
    expect(w.get('[data-testid=conflict-list]').text()).toContain('unknown opening hours')
    expect(w.get('[data-testid=conflict-panel]').classes()).toContain('notice-info')
    expect(w.classes()).not.toContain('notice-error')
    expect(w.find('[role=alert]').exists()).toBe(false)
    expect(w.text()).toContain('not a fault')
  })
  it('turns computed alternatives into buttons that emit the patch to re-run with', async () => {
    const w = mountIt()
    const btn = w.get('button[data-kind=allow_unknown_hours]')
    await btn.trigger('click')
    expect(w.emitted('apply')[0]).toEqual([{ allow_unknown_hours: true }])
    await w.get('button[data-kind=demote_must_visit]').trigger('click')
    expect(w.emitted('apply')[1]).toEqual([{ demote_must_visit: 'akihabara' }])
  })
  it('disables the actions while busy or rate limited', () => {
    const w = mount(ConflictPanel, { props: { result: infeasible.result, prefs, placesById: byId, busy: true } })
    expect(w.findAll('button').every((b) => b.attributes('disabled') !== undefined)).toBe(true)
  })
})

describe('ErrorPanel is the separate component for real errors', () => {
  it('uses the error style with role=alert, the code, and the request id', () => {
    const w = mount(ErrorPanel, { props: { error: { display: 'Boom (request id r1)', code: 'internal_error', status: 500 } } })
    expect(w.classes()).toContain('notice-error')
    expect(w.attributes('role')).toBe('alert')
    expect(w.text()).toContain('request id r1')
    expect(w.text()).toContain('internal_error')
  })
  it('explains solver_busy as capacity exhaustion and does not imply an automatic retry', () => {
    const w = mount(ErrorPanel, { props: { error: { message: 'The itinerary solver is busy.', code: 'solver_busy', status: 423, retryAfter: 3 } } })
    expect(w.text()).toContain('Solver is busy')
    expect(w.text()).toContain('No request was started')
    expect(w.text()).toContain('3 seconds')
  })
})

describe('FeasibilityReport: a fail is never rendered as OK', () => {
  const report = (overall, checks) => ({ overall, checks, data_quality_warnings: [], summary: 'S' })
  const chk = (id, status) => ({ id, name: `Check ${id}`, status, message: `msg ${id}`, stop_ids: [] })

  it('shows Fail with an icon and text, no "Pass" for the failing check, and opens by default', () => {
    const w = mount(FeasibilityReport, { props: { report: report('fail', [chk('a', 'pass'), chk('b', 'fail')]) } })
    expect(w.get('[data-testid=overall]').text()).toContain('Fail')
    expect(w.get('[data-testid=overall]').text()).not.toContain('Pass')
    const failing = w.findAll('[data-testid=check]').find((c) => c.attributes('data-status') === 'fail')
    expect(failing.text()).toContain('Fail')
    expect(failing.text()).toContain('✕')
    expect(failing.text()).not.toContain('Pass')
    expect(w.get('details').attributes('open')).toBeDefined()
  })
  it('is open when there are only warnings, and collapsed only when everything passes', () => {
    const warn = mount(FeasibilityReport, { props: { report: report('pass_with_warnings', [chk('a', 'warning')]) } })
    expect(warn.get('details').attributes('open')).toBeDefined()
    expect(warn.get('[data-testid=overall]').text()).toContain('Pass with warnings')
    const ok = mount(FeasibilityReport, { props: { report: report('pass', [chk('a', 'pass')]) } })
    expect(ok.get('details').attributes('open')).toBeUndefined()
  })
  it('renders a real report with its data-quality warnings visible', () => {
    const w = mount(FeasibilityReport, { props: { report: feasible.feasibility_report } })
    expect(w.findAll('[data-testid=check]').length).toBe(feasible.feasibility_report.checks.length)
    expect(w.get('[data-testid=dq-warnings]').text()).toContain('ESTIMATES')
  })
})

describe('Wishlist cards render untrusted text as plain text', () => {
  it('a script tag in a description cannot execute or even appear (the description is not rendered), and a script in a NAME is escaped', () => {
    const evil = { ...byId.senso_ji, description: poisoned[0].description, name: '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2<\/script>Evil' }
    expect(poisoned[0].description).toContain('Ignore previous instructions')
    const w = mount(AttractionCard, { props: { place: evil, state: 'nice', startWeekday: 2, days: 2, fx } })
    expect(w.html()).not.toContain('Ignore previous instructions')
    expect(w.find('script').exists()).toBe(false)
    expect(w.find('img').exists()).toBe(false)
    expect(w.text()).toContain('<script>window.__pwned=2</script>Evil')
    expect(window.__pwned).toBeUndefined()
  })
  it('shows unknown price and unknown hours as "unknown", with a hours warning and badges', () => {
    const w = mount(AttractionCard, { props: { place: byId.teamlab_planets, state: 'nice', startWeekday: 2, days: 1, fx } })
    expect(w.text()).toContain('unknown')
    expect(w.text()).not.toContain('¥0')
    const a = mount(AttractionCard, { props: { place: byId.akihabara, state: 'nice', startWeekday: 2, days: 1, fx } })
    expect(a.get('[data-testid=hours-warnings]').text()).toContain('never assumed open')
    const closed = mount(AttractionCard, { props: { place: byId.tokyo_national_museum, state: 'nice', startWeekday: 0, days: 2, fx } })
    expect(closed.get('[data-testid=hours-warnings]').text()).toContain('Closed on day 1 (Monday)')
  })
  it('emits the chosen state', async () => {
    const w = mount(AttractionCard, { props: { place: byId.senso_ji, state: 'nice', startWeekday: 2, days: 1, fx } })
    await w.get('input[value=must]').setValue()
    expect(w.emitted('update:state')[0]).toEqual(['must'])
    expect(w.findAll('input[type=radio]')).toHaveLength(3)
  })
})

describe('NotScheduledPanel (decision 3)', () => {
  it('lists each skipped sight with the plain explanation and the penalty when given', () => {
    const w = mount(NotScheduledPanel, { props: { missedIds: ['meiji_jingu', 'tokyo_tower'], placesById: byId, penaltyJpy: 3000 } })
    expect(w.findAll('[data-testid=skipped-list] li')).toHaveLength(2)
    expect(w.text()).toContain('the result does not establish a single cause.')
    expect(w.get('[data-testid=penalty-note]').text()).toContain('¥3,000')
  })
  it('says nothing about a penalty it was not given, and has an empty state', () => {
    expect(mount(NotScheduledPanel, { props: { missedIds: ['meiji_jingu'], placesById: byId } }).find('[data-testid=penalty-note]').exists()).toBe(false)
    expect(mount(NotScheduledPanel, { props: { missedIds: [], placesById: byId } }).text()).toContain('No omitted optional sights are listed')
  })
})

describe('DayTimeline and badges', () => {
  it('shows an unknown admission price as "unknown" and "not in the total", never ¥0', () => {
    const day = { day: 1, weekday: 'mon', hotel_departure: '09:00', hotel_return: '15:44', return_travel_minutes: 38, return_transport_jpy: 330,
      stops: [{ attraction_id: 'teamlab_planets', start: '13:06', end: '15:06', travel_minutes_from_previous: 20, transport_jpy_from_previous: 220, cost: 0,
        cost_status: 'unknown', is_must_visit: true, uncertainty_flags: ['price_unknown', 'advance_booking_required'] }] }
    const w = mount(DayTimeline, { props: { days: [day], placesById: byId, fx, hotelName: 'Hotel X' } })
    const stop = w.get('[data-testid=stop-teamlab_planets]')
    expect(stop.text()).toContain('Admission: Price unknown')
    expect(stop.text()).toContain('not in the total')
    expect(stop.text()).not.toContain('¥0')
    expect(stop.text()).toContain('Must visit')
    expect(stop.text()).toContain('Price unknown')
    expect(stop.text()).toContain('2 h 00 min')
  })
  it('SourceBadge says unknown when provenance is missing', () => {
    expect(mount(SourceBadge, { props: { status: null } }).text()).toContain('unknown')
    expect(mount(SourceBadge, { props: { status: 'estimated' } }).text()).toContain('estimated')
    expect(mount(SourceBadge, { props: { status: 'made-up' } }).text()).toContain('unknown')
  })
})

describe('static safety guards (decision 10 and XSS)', () => {
  const files = []
  const walk = (dir) => readdirSync(dir).forEach((f) => { const p = join(dir, f); statSync(p).isDirectory() ? walk(p) : /\.(vue|js)$/.test(f) && files.push(p) })
  walk(join(process.cwd(), 'src'))
  it('never uses v-html or innerHTML assignment', () => {
    for (const f of files) {
      const t = readFileSync(f, 'utf8')
      expect(t, f).not.toMatch(/v-html/i)
      expect(t, f).not.toMatch(/\.innerHTML\s*=/)
    }
  })
  it('never touches browser storage, cookies or analytics', () => {
    for (const f of files) {
      const t = readFileSync(f, 'utf8')
      expect(t, f).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie|\bgtag\b|dataLayer|googletagmanager|plausible|mixpanel/i)
    }
  })
  it('reads only VITE_API_BASE from the environment and never mentions an LLM key', () => {
    const envs = new Set()
    for (const f of files) {
      const t = readFileSync(f, 'utf8')
      for (const m of t.matchAll(/import\.meta\.env\??\.([A-Z_]+)/g)) envs.add(m[1])
      expect(t, f).not.toMatch(/LLM_API_KEY|ANTHROPIC|api[_-]?key/i)
    }
    expect([...envs]).toEqual(['VITE_API_BASE'])
  })
})
