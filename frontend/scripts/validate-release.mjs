// Release-only validation for a static frontend build. It is intentionally separate from `npm run build`
// so local development builds can still run without production evidence or an HTTPS API URL.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { execFileSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'

export class ReleaseValidationError extends Error {}

const SECRET_PATTERNS = [
  /sk-[A-Za-z0-9_-]{20,}/,
  /sk-ant-[A-Za-z0-9_-]{20,}/,
  /anthropic[_-]?api[_-]?key/i,
  /llm[_-]?api[_-]?key/i,
]

function walk(dir) {
  const out = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    const st = statSync(p)
    if (st.isDirectory()) out.push(...walk(p))
    else out.push(p)
  }
  return out
}

function gitRev(root) {
  try { return execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }).trim() } catch { return null }
}

export function validateRelease({ root = resolve(import.meta.dirname, '..', '..'), dist = resolve(root, 'frontend', 'dist'), apiBase = process.env.VITE_API_BASE } = {}) {
  const errors = []
  const api = (() => { try { return apiBase ? new URL(apiBase) : null } catch { return null } })()
  if (!api) errors.push('VITE_API_BASE must be set to the production backend URL.')
  else if (api.protocol !== 'https:') errors.push('VITE_API_BASE must use HTTPS for a production release.')
  else if (/localhost|127\.0\.0\.1|\[::1\]/.test(api.host)) errors.push('VITE_API_BASE must not point at a local development server.')

  for (const rel of ['_headers', '_redirects', 'proof-data/manifest.json', 'proof-data/backend-tests-full.md', 'proof-data/eval-results.json', 'proof-data/hardening-results.md', 'proof-data/frontend-test-summary.json']) {
    if (!existsSync(join(dist, rel))) errors.push(`dist is missing ${rel}. Run npm run build:release after generating evidence.`)
  }

  const rev = gitRev(root)
  const evalPath = join(dist, 'proof-data', 'eval-results.json')
  if (rev && existsSync(evalPath)) {
    const ev = JSON.parse(readFileSync(evalPath, 'utf8'))
    const evRev = String(ev?.meta?.commit ?? '').split(/\s+/)[0]
    if (evRev && !(rev.startsWith(evRev) || evRev.startsWith(rev))) errors.push(`evaluation evidence was generated for ${evRev}, not current HEAD ${rev}.`)
  }
  const frontPath = join(dist, 'proof-data', 'frontend-test-summary.json')
  if (rev && existsSync(frontPath)) {
    const ft = JSON.parse(readFileSync(frontPath, 'utf8'))
    const ftRev = String(ft?.git_hash ?? '').split(/\s+/)[0]
    if (ftRev && ftRev !== rev) errors.push(`frontend test summary was generated for ${ftRev}, not current HEAD ${rev}.`)
  }

  if (existsSync(dist)) {
    for (const file of walk(dist)) {
      const text = readFileSync(file, 'utf8')
      if (SECRET_PATTERNS.some((r) => r.test(text))) errors.push(`possible secret found in built asset: ${file}`)
    }
  } else errors.push('dist does not exist. Run npm run build first.')

  if (errors.length) throw new ReleaseValidationError(errors.join('\n'))
  return { ok: true, apiOrigin: api.origin, revision: rev }
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? '').href) {
  try {
    const result = validateRelease({})
    console.log(`release validation passed for ${result.revision ?? 'unknown revision'} (${result.apiOrigin})`)
  } catch (e) {
    if (e instanceof ReleaseValidationError) { console.error(e.message); process.exit(1) }
    throw e
  }
}
