// Lighthouse on the BUILT app (production build served by `vite preview` with a real backend in sample mode). Writes
// docs/lighthouse-summary.json and docs/lighthouse-results.md. Scores are exactly what Lighthouse reports; nothing is adjusted.
//   * home and proof: full navigation audits (performance, accessibility, best practices), mobile preset, simulated throttling
//   * Your trip: the plan lives in memory only, so a fresh navigation would show the empty state. It is measured as a SNAPSHOT of the real
//     page after the example flow (accessibility and best practices only: Lighthouse cannot measure performance for a snapshot).
// Usage: node scripts/run-lighthouse.mjs      (starts its own servers on :8011 and :4181)
import { spawn } from 'node:child_process'
import { closeSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from '@playwright/test'
import lighthouse, { startFlow } from 'lighthouse'
import puppeteer from 'puppeteer-core'

const FRONT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const BACK = resolve(FRONT, '../backend')
const DOCS = resolve(FRONT, '../docs')
const API = 'http://localhost:8011'
const APP = 'http://localhost:4181'
const procs = []
const LOG_DIR = resolve(FRONT, 'test-results')
mkdirSync(LOG_DIR, { recursive: true })
// Child output goes to a log file (not 'ignore') so a failed start, e.g. port in use, is reported instead of a bare timeout.
const run = (cmd, cwd, env = {}, name = 'child') => {
  const log = resolve(LOG_DIR, `lighthouse-${name}.log`)
  const fd = openSync(log, 'w')
  const p = spawn(cmd, { cwd, shell: true, env: { ...process.env, ...env }, stdio: ['ignore', fd, fd], detached: true })
  closeSync(fd)
  p.logFile = log
  procs.push(p)
}
const stopAll = () => procs.forEach((p) => { try { process.kill(-p.pid) } catch { /* gone */ } })
async function waitFor(url, ms = 120_000) {
  const t = Date.now()
  while (Date.now() - t < ms) {
    try { if ((await fetch(url)).ok) return } catch { /* not yet */ }
    const dead = procs.find((p) => p.exitCode !== null)
    if (dead) throw new Error(`a server exited with code ${dead.exitCode} before ${url} was ready:\n${readFileSync(dead.logFile, 'utf8').slice(-1500)}`)
    await new Promise((r) => setTimeout(r, 400))
  }
  throw new Error('timeout ' + url + '\n' + procs.map((p) => `--- ${p.logFile}\n${readFileSync(p.logFile, 'utf8').slice(-800)}`).join('\n'))
}
const nodesOf = (a) => (a.details?.items ?? []).slice(0, 4).map((i) => (i.node?.snippet ?? i.subItems?.items?.[0]?.node?.snippet ?? '').replace(/\s+/g, ' ').slice(0, 140)).filter(Boolean)
const failed = (lhr) => Object.values(lhr.audits).filter((a) => a.score !== null && a.score < 1 && a.scoreDisplayMode !== 'informative' && ['accessibility', 'best-practices'].some((c) => lhr.categories[c]?.auditRefs.some((x) => x.id === a.id)))
const describeFailed = (lhr) => failed(lhr).map((a) => `${a.id}: ${a.title}${nodesOf(a).length ? ' [e.g. ' + nodesOf(a).join(' | ') + ']' : ''}`)
const score = (lhr, key) => (lhr.categories[key]?.score === null || lhr.categories[key] === undefined ? null : Math.round(lhr.categories[key].score * 100))

const out = { generated: new Date().toISOString(), tool: 'lighthouse ' + (await import('lighthouse/package.json', { with: { type: 'json' } })).default.version, preset: 'mobile (default), simulated throttling', pages: [] }
try {
  run('.venv/bin/uvicorn app.main:app --port 8011 --log-level warning', BACK, { RATE_LIMIT_EXPENSIVE_PER_MIN: '500', RATE_LIMIT_PER_MIN: '1000', RATE_LIMIT_CHEAP_PER_MIN: '5000', SOLVER_CONCURRENCY: '8', CORS_ORIGINS: APP, OPENROUTER_API_KEY: '', LLM_MODEL: '' }, 'backend')
  run(`node scripts/sync-proof.mjs && VITE_API_BASE=${API} npx vite build --outDir dist-lh && VITE_API_BASE=${API} node scripts/write-host-config.mjs dist-lh && npx vite preview --outDir dist-lh --port 4181 --strictPort`, FRONT, {}, 'frontend')
  await waitFor(`${API}/health`)
  await waitFor(APP)

  const chrome = await chromium.launch({ args: ['--remote-debugging-port=9334'] })
  const browser = await puppeteer.connect({ browserURL: 'http://localhost:9334', defaultViewport: null })
  const flags = { logLevel: 'error', onlyCategories: ['performance', 'accessibility', 'best-practices'], port: 9334 }

  for (const [name, path] of [['Plan (home)', '/'], ['Proof', '/proof']]) {
    const page = await browser.newPage()
    const r = await lighthouse(APP + path, flags, undefined, page)
    const lhr = r.lhr
    out.pages.push({ name, url: path, mode: 'navigation', performance: score(lhr, 'performance'), accessibility: score(lhr, 'accessibility'), bestPractices: score(lhr, 'best-practices'),
      metrics: { 'First Contentful Paint': lhr.audits['first-contentful-paint']?.displayValue, 'Largest Contentful Paint': lhr.audits['largest-contentful-paint']?.displayValue,
        'Total Blocking Time': lhr.audits['total-blocking-time']?.displayValue, 'Cumulative Layout Shift': lhr.audits['cumulative-layout-shift']?.displayValue },
      failedAudits: describeFailed(lhr) })
    await page.close()
  }

  // Your trip: snapshot of the real page after the example flow
  const pwPage = await chrome.newPage()
  const ppPage = (await browser.pages()).find((p) => p.url() === 'about:blank') ?? (await browser.newPage())
  await ppPage.goto(APP + '/', { waitUntil: 'networkidle0' })
  await ppPage.waitForSelector('[data-testid=load-example]')
  await ppPage.click('[data-testid=load-example]')
  await ppPage.click('[data-testid=plan-trip]')            // one click: compares the hotels, then plans the days, then lands on Your trip
  await ppPage.waitForSelector('[data-testid=timeline]', { timeout: 120_000 })
  await ppPage.waitForSelector('.leaflet-container .pin')
  await new Promise((r) => setTimeout(r, 1500))
  const flow = await startFlow(ppPage, { config: { extends: 'lighthouse:default', settings: { onlyCategories: ['accessibility', 'best-practices'] } } })
  await flow.snapshot({ name: 'Your trip' })
  const lhr = (await flow.createFlowResult()).steps[0].lhr
  out.pages.push({ name: 'Your trip (after the example flow)', url: '/trip', mode: 'snapshot', performance: null, accessibility: score(lhr, 'accessibility'), bestPractices: score(lhr, 'best-practices'), metrics: {},
    failedAudits: describeFailed(lhr) })
  await pwPage.close(); await browser.disconnect(); await chrome.close()
} finally { stopAll() }

mkdirSync(DOCS, { recursive: true })
writeFileSync(resolve(DOCS, 'lighthouse-summary.json'), JSON.stringify(out, null, 2) + '\n')
const cell = (v) => (v === null ? 'not measured' : v)
const md = ['# Lighthouse results (measured)', '', `Generated ${out.generated} by \`frontend/scripts/run-lighthouse.mjs\` with ${out.tool}, ${out.preset}. Production build, real backend in sample mode, local machine. Scores vary run to run and with the machine; they are what Lighthouse reported, unadjusted.`, '',
  '| Page | Mode | Performance | Accessibility | Best practices |', '|---|---|---|---|---|', ...out.pages.map((p) => `| ${p.name} | ${p.mode} | ${cell(p.performance)} | ${cell(p.accessibility)} | ${cell(p.bestPractices)} |`), '',
  'The Your trip page is measured as a snapshot of the real page after the example flow (its data lives in memory only, so a fresh navigation would show the empty state). Lighthouse does not measure performance for a snapshot, so that cell is "not measured".', '']
for (const p of out.pages) {
  if (Object.values(p.metrics).some(Boolean)) md.push(`**${p.name} metrics:** ${Object.entries(p.metrics).map(([k, v]) => `${k} ${v}`).join(', ')}`, '')
  md.push(p.failedAudits.length ? `**${p.name}: audits below 100:** ${p.failedAudits.join('; ')}` : `**${p.name}:** no failed accessibility or best-practices audits.`, '')
}
writeFileSync(resolve(DOCS, 'lighthouse-results.md'), md.join('\n'))
console.log(md.join('\n'))
