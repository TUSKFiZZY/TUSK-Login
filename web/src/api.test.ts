import { afterEach, expect, type Mock, test, vi } from 'vitest'
import {
  ANSWER_WITHIN_MS, answerWithin, api, ApiError, NO_SESSION, NOT_ANSWERING, restoreFile, SLOW_ANSWER_WITHIN_MS,
} from './api.ts'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

function answers(status: number, body: string) {
  const fake = vi.fn(async (_path: string, _init: RequestInit) => new Response(body, { status }))
  vi.stubGlobal('fetch', fake)
  return fake
}

/** A call of the fake fetch: its address, and what it was given apart from the signal that ends it, which every request has. */
function sent(fake: Mock<(path: string, init: RequestInit) => Promise<Response>>, call: number): [string, RequestInit] {
  const [path, { signal, ...init }] = fake.mock.calls[call]
  expect(signal).toBeInstanceOf(AbortSignal)
  return [path, init]
}

/** A core that takes a request and never answers it, as one that hangs: only the request's signal ends the wait. */
function neverAnswers() {
  const fake = vi.fn(
    (_path: string, init: RequestInit) =>
      new Promise<Response>((_, reject) => {
        init.signal?.addEventListener('abort', () => reject(new DOMException('The operation was aborted.', 'AbortError')))
      }),
  )
  vi.stubGlobal('fetch', fake)
  return fake
}

async function failure(attempt: Promise<unknown>): Promise<ApiError> {
  try {
    await attempt
  } catch (error) {
    if (error instanceof ApiError) return error
    throw error
  }
  throw new Error('the request did not fail')
}

test('a good answer is the data', async () => {
  const fake = answers(200, '{"mode":"demo"}')
  expect(await api.get('/api/status')).toEqual({ mode: 'demo' })
  expect(sent(fake, 0)).toEqual(['/api/status', { method: 'GET' }])
})

test('a body is sent as JSON', async () => {
  const fake = answers(200, '{"in_session_interval_min":30}')
  await api.put('/api/settings', { in_session_interval_min: 30 })
  expect(sent(fake, 0)).toEqual([
    '/api/settings',
    { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: '{"in_session_interval_min":30}' },
  ])
  await api.post('/api/sync')
  expect(sent(fake, 1)).toEqual(['/api/sync', { method: 'POST' }])
})

test('a change that must outlive the page asks for keepalive, and no other request does', async () => {
  const fake = answers(200, '{}')
  await api.put('/api/characters/sandrone/plan', { level: 90 }, { keepalive: true })
  expect(sent(fake, 0)).toEqual([
    '/api/characters/sandrone/plan',
    { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: '{"level":90}', keepalive: true },
  ])
  await api.put('/api/characters/sandrone/plan', { level: 90 })
  expect(sent(fake, 1)[1]).not.toHaveProperty('keepalive')
})

test("a backup file to restore is the request's body as it is, with its name in the address", async () => {
  const fake = answers(200, '{"restarting":true}')
  const file = new File([new Uint8Array([71, 66, 1, 2])], 'Guild Board 2026-10-31 #2.gbbackup')
  expect(await restoreFile(file)).toEqual({ restarting: true })
  const [path, init] = sent(fake, 0)
  expect(path).toBe('/api/backup/restore?file_name=Guild%20Board%202026-10-31%20%232.gbbackup')
  expect(init.method).toBe('POST')
  expect(init.headers).toEqual({ 'Content-Type': 'application/octet-stream' })
  // The file itself, never read here: no copy of it as text, as base64 or as JSON is made in the window.
  expect(init.body).toBeInstanceOf(Blob)
  expect(init.body).toBe(file)
})

test('a request with no answer fails as not answering, so that the changes queued behind it go on', async () => {
  vi.useFakeTimers()
  neverAnswers()
  const waiting = failure(api.post('/api/achievements/stage/81732', { status: 'done' }))
  await vi.advanceTimersByTimeAsync(ANSWER_WITHIN_MS - 1)
  let settled = false
  void waiting.then(() => {
    settled = true
  })
  await vi.advanceTimersByTimeAsync(0)
  expect(settled).toBe(false) // still waiting, a moment before the limit
  await vi.advanceTimersByTimeAsync(1)
  const error = await waiting
  expect(error.status).toBe(0)
  expect(error.message).toBe(NOT_ANSWERING)
})

test('a read the page calls off ends at once, and not as Guild Board not answering', async () => {
  vi.useFakeTimers()
  neverAnswers()
  const reading = new AbortController()
  const read = api.get('/api/board', { signal: reading.signal })
  reading.abort()
  const error: unknown = await read.catch((thrown: unknown) => thrown)
  expect(error).not.toBeInstanceOf(ApiError)
  expect((error as Error).name).toBe('AbortError')
  expect(vi.getTimerCount()).toBe(0)
  // One called off before it was sent is not sent.
  const fake = answers(200, '{}')
  const before = new AbortController()
  before.abort()
  await expect(api.get('/api/board', { signal: before.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(fake).not.toHaveBeenCalled()
})

test('a request that is answered in time leaves no timer behind', async () => {
  vi.useFakeTimers()
  answers(200, '{}')
  await api.get('/api/board')
  expect(vi.getTimerCount()).toBe(0)
  answers(500, '{"error":"No."}')
  await failure(api.get('/api/board'))
  expect(vi.getTimerCount()).toBe(0)
})

test("the online account's requests, the list check and a restore wait longer: their answers wait on the internet or a large file", () => {
  expect(ANSWER_WITHIN_MS).toBe(15_000)
  for (const path of [
    '/api/online/sync', '/api/online/first-sync', '/api/friends?fresh=1', '/api/friends/00000000-0000-4000-8000-000000007687/compare',
    '/api/profile', '/api/profile/pins', '/api/achievements/list/check', '/api/checkin/claim', '/api/backup/restore',
    '/api/backup/restore?file_name=a.gbbackup',
  ]) {
    expect(answerWithin(path), path).toBe(SLOW_ANSWER_WITHIN_MS)
  }
  for (const path of ['/api/status', '/api/onlinex', '/api/profiles', '/api/achievements/list', '/api/checkin', '/api/backups', '/api/backup']) {
    expect(answerWithin(path), path).toBe(ANSWER_WITHIN_MS)
  }
})

test("the core's own sentence is the error message", async () => {
  answers(409, '{"error":"Demo mode has no login."}')
  const error = await failure(api.post('/api/auth/login'))
  expect(error.status).toBe(409)
  expect(error.message).toBe('Demo mode has no login.')
})

test("a refused hotkey's detail is the error message, and FastAPI's own list there is not", async () => {
  answers(422, '{"detail":"Ctrl+Alt+E types a letter on your keyboard. Choose another key."}')
  const refused = await failure(api.put('/api/settings', { overlay_hotkey: 'Ctrl+Alt+E' }))
  expect(refused.status).toBe(422)
  expect(refused.message).toBe('Ctrl+Alt+E types a letter on your keyboard. Choose another key.')

  answers(422, '{"detail":[{"loc":["body"],"msg":"Field required","type":"missing"}]}')
  const listed = await failure(api.put('/api/settings', {}))
  expect(listed.message).toBe('TUSKFiZZY answered with an error (422).')
})

test("the core's own sentence and a missing session come before a detail", async () => {
  answers(409, '{"error":"Demo mode has no login.","detail":"Something else."}')
  expect((await failure(api.post('/api/auth/login'))).message).toBe('Demo mode has no login.')
  answers(401, '{"detail":"Not authenticated"}')
  expect((await failure(api.get('/api/status'))).message).toBe(NO_SESSION)
})

test('a missing session and a dead core give plain messages', async () => {
  answers(401, 'no cookie')
  const noSession = await failure(api.get('/api/status'))
  expect(noSession.status).toBe(401)
  expect(noSession.message).toBe(NO_SESSION)

  vi.stubGlobal('fetch', vi.fn(async () => {
    throw new TypeError('Failed to fetch')
  }))
  const dead = await failure(api.get('/api/status'))
  expect(dead.status).toBe(0)
  expect(dead.message).toBe(NOT_ANSWERING)

  answers(500, '<html>Internal Server Error</html>')
  const broken = await failure(api.get('/api/backlog'))
  expect(broken.message).toBe('TUSKFiZZY answered with an error (500).')
})
