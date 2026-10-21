import { defineConfig, devices } from '@playwright/test'

// End-to-end tests run against the REAL backend in sample mode (no LLM key) on port 8010 with relaxed rate limits (so the suite does not
// trip its own limiter; the 429 behaviour is tested separately by intercepting a response), and a production build of the frontend.
export default defineConfig({
  testDir: './tests/e2e',
  outputDir: './pw-artifacts',   // not test-results/, which holds the JSON summaries used as evidence
  timeout: 90_000,
  expect: { timeout: 15_000 },
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: { baseURL: 'http://localhost:4173', trace: 'off', ...devices['Desktop Chrome'] },
  webServer: [
    {
      command: 'RATE_LIMIT_EXPENSIVE_PER_MIN=500 RATE_LIMIT_PER_MIN=1000 RATE_LIMIT_CHEAP_PER_MIN=5000 SOLVER_CONCURRENCY=8 CORS_ORIGINS=http://localhost:4173,http://localhost:4175 OPENROUTER_API_KEY= LLM_MODEL= .venv/bin/uvicorn app.main:app --port 8010 --log-level warning',
      cwd: '../backend', url: 'http://localhost:8010/health', reuseExistingServer: false, timeout: 60_000,
    },
    {
      command: 'node scripts/sync-proof.mjs && VITE_API_BASE=http://localhost:8010 npx vite build --outDir dist-e2e && VITE_API_BASE=http://localhost:8010 node scripts/write-host-config.mjs dist-e2e && npx vite preview --outDir dist-e2e --port 4173 --strictPort',
      url: 'http://localhost:4173', reuseExistingServer: false, timeout: 120_000,
    },
  ],
})
