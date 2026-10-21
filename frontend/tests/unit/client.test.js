import { describe, expect, it, vi } from 'vitest'
import { ApiError, isAborted, normaliseError, request, setRateLimitHandler, waitForServer } from '../../src/api/client.js'
import { errBody, res } from './helpers.js'

describe('normaliseError', () => {
  it('reads the unified {error:{code,message,details,request_id}} body and keeps the request id', () => {
    const e = normaliseError({ status: 422, body: errBody('invalid_request', 'days must be at most 7', { field: 'days' }, 'abc123') })
    expect(e).toBeInstanceOf(ApiError)
    expect([e.code, e.message, e.status, e.requestId, e.details.field]).toEqual(['invalid_request', 'days must be at most 7', 422, 'abc123', 'days'])
    expect(e.display).toBe('days must be at most 7 (request id abc123)')
  })
  it('reads Retry-After seconds from the body on 429 (the browser cannot read the header cross-origin) and rounds up', () => {
    const e = normaliseError({ status: 429, body: errBody('rate_limited', 'Too many requests.', { retry_after_seconds: 7.2 }) })
    expect([e.code, e.retryAfter]).toEqual(['rate_limited', 8])
  })
  it('recognises solver capacity 423 responses without treating them as a host crash', () => {
    const e = normaliseError({ status: 423, body: errBody('solver_busy', 'Solver busy.', { retry_after_seconds: 3 }) })
    expect([e.code, e.status, e.retryAfter, e.message]).toEqual(['solver_busy', 423, 3, 'Solver busy.'])
  })
  it('routes the shared rate-limit engine by code: 429 -> ai_quota_reached with detail.queries_used, 503 -> demo_paused', () => {
    const q = normaliseError({ status: 429, body: { detail: { message: 'Rate limit exceeded.', queries_used: 5 }, ...errBody('ai_quota_reached', 'Used up.', { queries_used: 5 }) } })
    expect([q.code, q.queriesUsed, q.retryAfter]).toEqual(['ai_quota_reached', 5, null])
    expect(normaliseError({ status: 429, body: { detail: { message: 'Rate limit exceeded.', queries_used: 3 } } }).queriesUsed).toBe(3)
    const p = normaliseError({ status: 503, body: { detail: { message: 'Paused for maintenance.' }, ...errBody('demo_paused', 'Paused for maintenance.') } })
    expect([p.code, p.message]).toEqual(['demo_paused', 'Paused for maintenance.'])
    expect(normaliseError({ status: 503, body: { detail: { message: 'Paused.' } } }).code).toBe('demo_paused')
  })
  it('falls back to a safe wait when a 429 has no usable body', () => {
    expect(normaliseError({ status: 429, body: null }).retryAfter).toBe(10)
    expect(normaliseError({ status: 429, body: '<html>' }).code).toBe('rate_limited')
  })
  it('treats non-JSON 5xx (a sleeping or failing host) as server_unavailable, not as a crash', () => {
    const e = normaliseError({ status: 503, body: '<html>Service Unavailable</html>' })
    expect(e.code).toBe('server_unavailable')
    expect(e.message).toMatch(/waking up/)
  })
  it('handles unexpected shapes', () => {
    expect(normaliseError({ status: 404, body: { detail: 'nope' } }).code).toBe('unexpected_response')
    expect(normaliseError({ status: 400, body: { error: 'string' } }).code).toBe('unexpected_response')
  })
})

describe('request', () => {
  it('on 429 starts the countdown handler with the server-provided seconds and throws a rate_limited error', async () => {
    const handler = vi.fn()
    setRateLimitHandler(handler)
    const fetchImpl = vi.fn(async () => res(429, errBody('rate_limited', 'Too many requests.', { retry_after_seconds: 7 })))
    await expect(request('POST', '/optimise', { body: {}, fetchImpl })).rejects.toMatchObject({ code: 'rate_limited', retryAfter: 7, status: 429 })
    expect(handler).toHaveBeenCalledWith(7)
    setRateLimitHandler(() => {})
  })

  it('never retries a POST: a failing request is attempted exactly once', async () => {
    const fetchImpl = vi.fn(async () => res(500, errBody('internal_error', 'Internal error.')))
    await expect(request('POST', '/optimise', { body: { a: 1 }, fetchImpl })).rejects.toMatchObject({ code: 'internal_error' })
    expect(fetchImpl).toHaveBeenCalledTimes(1)
    const net = vi.fn(async () => { throw new TypeError('Failed to fetch') })
    await expect(request('POST', '/compare-hotels', { body: {}, fetchImpl: net })).rejects.toMatchObject({ code: 'network' })
    expect(net).toHaveBeenCalledTimes(1)
  })

  it('a newer request with the same key aborts the older one; a stale response can never overwrite a newer one', async () => {
    let releaseA
    const fetchA = vi.fn(() => new Promise((resolve) => { releaseA = () => resolve(res(200, { which: 'old' })) })) // ignores abort on purpose
    const a = request('POST', '/optimise', { key: 'k', body: {}, fetchImpl: fetchA })
    const b = await request('POST', '/optimise', { key: 'k', body: {}, fetchImpl: async () => res(200, { which: 'new' }) })
    expect(b).toEqual({ which: 'new' })
    releaseA()
    const err = await a.catch((e) => e)
    expect(isAborted(err)).toBe(true)
  })

  it('times out with a clear message and cancels the request', async () => {
    const fetchImpl = (url, { signal }) => new Promise((_, reject) => signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))))
    await expect(request('GET', '/health', { timeoutMs: 15, fetchImpl })).rejects.toMatchObject({ code: 'timeout' })
  })

  it('sends no credentials, no referrer and JSON content type only when there is a body', async () => {
    const fetchImpl = vi.fn(async () => res(200, { ok: true }))
    await request('POST', '/x', { body: { a: 1 }, fetchImpl })
    await request('GET', '/y', { fetchImpl })
    const [, post] = fetchImpl.mock.calls[0]
    const [, get] = fetchImpl.mock.calls[1]
    expect(post).toMatchObject({ credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' })
    expect(post.headers['Content-Type']).toBe('application/json')
    expect(get.headers['Content-Type']).toBeUndefined()
  })

  it('rejects an unreadable 200 body instead of returning garbage', async () => {
    await expect(request('GET', '/h', { fetchImpl: async () => res(200, 'not json') })).rejects.toMatchObject({ code: 'unexpected_response' })
  })
})

describe('waitForServer (cold start)', () => {
  const unavailable = () => new ApiError({ code: 'network', message: 'down' })

  it('retries with backoff while the server wakes, tells the UI once, then succeeds', async () => {
    const ping = vi.fn().mockRejectedValueOnce(unavailable()).mockRejectedValueOnce(unavailable()).mockResolvedValue({ status: 'ok', mode: 'sample' })
    const sleeps = []
    const onWaking = vi.fn()
    const out = await waitForServer({ ping, sleep: async (ms) => { sleeps.push(ms) }, onWaking, slowAfterMs: 10_000 })
    expect(out).toEqual({ health: { status: 'ok', mode: 'sample' }, waited: true })
    expect(sleeps).toEqual([1000, 2000])
    expect(onWaking).toHaveBeenCalledTimes(1)
    expect(ping).toHaveBeenCalledTimes(3)
  })

  it('does not report waking when the first answer is fast', async () => {
    const onWaking = vi.fn()
    const out = await waitForServer({ ping: async () => ({ status: 'ok' }), onWaking })
    expect(out.waited).toBe(false)
    expect(onWaking).not.toHaveBeenCalled()
  })

  it('reports waking when the first request is merely slow, without treating it as an error', async () => {
    const onWaking = vi.fn()
    const out = await waitForServer({ ping: () => new Promise((r) => setTimeout(() => r({ status: 'ok' }), 40)), slowAfterMs: 5, onWaking })
    expect(onWaking).toHaveBeenCalledTimes(1)
    expect(out.health).toEqual({ status: 'ok' })
  })

  it('gives up with server_unreachable only after the maximum wait (about 90 s)', async () => {
    let t = 0
    const ping = vi.fn(async () => { throw unavailable() })
    const err = await waitForServer({ ping, now: () => t, sleep: async (ms) => { t += ms + 30_000 }, maxMs: 90_000, slowAfterMs: 10_000 }).catch((e) => e)
    expect(err).toMatchObject({ code: 'server_unreachable' })
    expect(ping.mock.calls.length).toBeGreaterThan(1)
    expect(ping.mock.calls.length).toBeLessThan(10)
  })

  it('treats a 429 as "the server is awake"', async () => {
    const out = await waitForServer({ ping: async () => { throw new ApiError({ code: 'rate_limited', message: 'x', status: 429, retryAfter: 5 }) } })
    expect(out.health).toBeNull()
  })
})
