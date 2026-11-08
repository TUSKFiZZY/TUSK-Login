/**
 * Requests to the local API. Every failure becomes an ApiError with a sentence fit for the player. A body is sent as JSON,
 * and a file (a Blob) as it is.
 */

import type { LedgerHistory, LedgerKind } from './ledgertypes.ts'

export const NOT_ANSWERING = 'TUSKFiZZY is not answering. It may have been closed.'
export const NO_SESSION = 'Open TUSKFiZZY from its own window or launch link.'

export class ApiError extends Error {
  /** The HTTP status, or 0 when the core did not answer at all. */
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function sentence(status: number, data: unknown): string {
  const fields: object = typeof data === 'object' && data !== null ? data : {}
  if ('error' in fields && typeof fields.error === 'string') return fields.error
  if (status === 401) return NO_SESSION
  // Why a hotkey is refused (PUT /api/settings). FastAPI's own refusals put a list here, which is not a sentence.
  if ('detail' in fields && typeof fields.detail === 'string') return fields.detail
  return `TUSKFiZZY answered with an error (${status}).`
}

/**
 * How long the page waits for the core's answer before it says that the core is not answering (audit L25). The core
 * answers a local request in a moment; one it never answers would otherwise hold every change queued behind it on the
 * page (ui/changes.tsx), with no word said.
 */
export const ANSWER_WITHIN_MS = 15_000

/**
 * How long the page waits where the core's answer waits on the internet or on a large file: the online account's requests
 * (online, profile, friends), the check for a new achievement list, the claim of the daily check-in and a restore. The core's
 * own requests there give up after 20 to 30 seconds a step (netpolicy.py), and a sync takes several steps, so the core says
 * why first.
 */
export const SLOW_ANSWER_WITHIN_MS = 120_000

const SLOW_PATHS = /^\/api\/(online|profile|friends|achievements\/list\/check|checkin\/claim|backup\/restore|tuskfizzy\/enka)(?=[/?]|$)/

/** How long the page waits for the answer to a request to `path`. */
export function answerWithin(path: string): number {
  return SLOW_PATHS.test(path) ? SLOW_ANSWER_WITHIN_MS : ANSWER_WITHIN_MS
}

/**
 * What a request can ask for besides its body: keepalive, for a change that must outlive the page, sent as the page goes;
 * and a signal that calls it off, as a read that a newer one took the place of is. A request called off rejects with the
 * signal's reason, an AbortError, which is no failure of the core's and no ApiError.
 */
export type RequestOptions = {
  keepalive?: boolean
  signal?: AbortSignal
  /** The Content-Type of a file sent as the body (a Blob): application/octet-stream where it is left out. */
  type?: string
}

type Method = 'GET' | 'POST' | 'PUT' | 'DELETE'

/**
 * Answers a call in place of the core. The installed app has none: only the site's browser demo sets one (sitedemo.ts, built in
 * only when VITE_SITE_DEMO is set), which reads recorded answers and refuses what the page sends. Nothing sets it otherwise.
 */
export type Transport = (method: Method, path: string, body: unknown) => Promise<unknown>

let transport: Transport | null = null

export function setTransport(next: Transport | null): void {
  transport = next
}

/** Whether the calls are answered by a transport and there is no core, so no stream of its events either. */
export function callsAreAnswered(): boolean {
  return transport !== null
}

async function request<T>(method: Method, path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
  const { signal } = options
  signal?.throwIfAborted()
  if (transport !== null) return (await transport(method, path, body)) as T
  const limit = new AbortController()
  const timer = setTimeout(() => limit.abort(), answerWithin(path))
  const callOff = () => limit.abort()
  signal?.addEventListener('abort', callOff)
  /** Why the request ended without an answer: called off by the page, or the core did not answer in time or at all. */
  const unanswered = (): unknown => (signal?.aborted ? signal.reason : new ApiError(0, NOT_ANSWERING))
  const init: RequestInit = { method, signal: limit.signal }
  if (options.keepalive === true) init.keepalive = true
  if (body instanceof Blob) {
    // A file goes as it is: the browser sends it from where it lies, and the page holds no copy of it.
    init.headers = { 'Content-Type': options.type ?? 'application/octet-stream' }
    init.body = body
  } else if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  try {
    let response: Response
    try {
      response = await fetch(path, init)
    } catch {
      throw unanswered()
    }
    let data: unknown = null
    try {
      data = await response.json()
    } catch {
      // the answer was not JSON; the status decides
    }
    if (limit.signal.aborted) throw unanswered() // the answer began but did not end in time, or was called off
    if (!response.ok) throw new ApiError(response.status, sentence(response.status, data))
    return data as T
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', callOff)
  }
}

export const api = {
  get: <T = unknown>(path: string, options?: RequestOptions) => request<T>('GET', path, undefined, options),
  post: <T = unknown>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T = unknown>(path: string, body: unknown, options?: RequestOptions) => request<T>('PUT', path, body, options),
  delete: <T = unknown>(path: string) => request<T>('DELETE', path),
}

/**
 * Restores a backup file the player chose (audit L27): the file is the request's body as it is, and its name goes in the
 * address. Read whole, turned into a string, base64 and JSON, a file was held about four times in the window.
 */
export function restoreFile(file: File): Promise<{ restarting: boolean }> {
  return request('POST', `/api/backup/restore?file_name=${encodeURIComponent(file.name)}`, file)
}

/**
 * Hands the Wrapped picture, a PNG the page made, to the core under the name the Save dialog offers (the Months and Wrapped
 * design, 6.4). The core holds it and asks the window for its Save dialog; with no window it answers 409.
 */
export function savePicture(name: string, picture: Blob): Promise<{ opened: boolean }> {
  return request('POST', `/api/stats/year/picture?name=${encodeURIComponent(name)}`, picture, { type: 'image/png' })
}

/**
 * The Ledger page's calls (the ledger plan, Task 6). Its model is read at LEDGER_PATH. A page of a log's history is
 * newest first, before an entry's id; Import is the Pulls page's, which reads the wishes and then the game's logs.
 */
export const LEDGER_PATH = '/api/ledger'

export const ledgerApi = {
  history: (kind: LedgerKind, before: string | null, limit = 50) =>
    api.get<LedgerHistory>(
      `/api/ledger/history?kind=${kind}&limit=${limit}${before === null ? '' : `&before=${encodeURIComponent(before)}`}`,
    ),
  importFromGame: () => api.post('/api/pulls/import'),
}

/** The sentence to show for anything that was thrown. */
export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}
