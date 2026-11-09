import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, callsAreAnswered, messageOf } from './api.ts'
import { setPictureQuality } from './ui/pictureQuality.ts'
import type { PullsImportEvent, Settings, Status } from './types.ts'

/**
 * The core's events on which the page reads the status and the settings again, and the pages their models. The overlay
 * hears of a change to its settings from elsewhere, and of the shell's report of its hotkey (plan 2A-4). The Pulls page
 * hears of a change to what it shows, an import's end included (the Pulls plan). The Characters pages hear of a change to
 * the focus, a plan, a tick, a card, a typed amount, the Theater's pick, or what HoYoLAB or the wiki gave (the Characters plan).
 * Settings hears of a sign-in that ended in the system browser (the online plan). Stats' Endgame card hears that its modes'
 * wiki entries or header pictures were kept (the Stats design, 3.2).
 */
export const EVENTS = [
  'sync_started', 'sync_finished', 'state_changed', 'game_started', 'game_stopped', 'achievements_changed',
  'timers_changed', 'layout_changed', 'settings_changed', 'overlay_changed', 'pulls_changed', 'update_changed', 'map_changed', 'codes_changed',
  'characters_changed', 'online_changed', 'checkin_changed', 'tuskfizzy_changed', 'tuskfizzy_imported', 'stats_changed', 'window_changed',
]
const REFRESH_EVERY_MS = 30_000

/** The core's time, carried forward by the time that passed here since the answer arrived. */
export function coreClock(coreNow: string, receivedAt: number, elapsed: () => number = () => performance.now()): () => Date {
  const base = Date.parse(coreNow)
  return () => new Date(base + (elapsed() - receivedAt))
}

/**
 * For requests that can overlap. Each call marks a new request as the newest and returns a check
 * that says whether it still is, so that an old answer arriving late is not shown over a newer one.
 */
export function newestOnly(): () => () => boolean {
  let newest = 0
  return () => {
    const mine = (newest += 1)
    return () => mine === newest
  }
}

/**
 * Which answers the page takes, of the reads of the status and the settings, and of the event stream's hello, which
 * carries the status as the core had it when the stream opened. A read counts while no read was asked for after it:
 * its settings are taken, and its failure is shown. Its status is taken only while nothing set the status since it was
 * asked for, the hello included: a read asked for before the hello can be answered after it, with an older status.
 */
export function liveOrder(): {
  read(): { isNewest: () => boolean; statusIsNewest: () => boolean }
  hello(): void
} {
  const reads = newestOnly()
  const statuses = newestOnly() // what sets the status: the reads and the hellos
  return {
    read: () => ({ isNewest: reads(), statusIsNewest: statuses() }),
    hello: () => {
      statuses()
    },
  }
}

/**
 * Whether the pages read their models again on an event of the stream, its hello included. A hello opens the stream,
 * at the start and after every reconnect, and what the core announced while the stream was not listening reached no
 * page, so the pages read again then: one read more for each time the stream opens. A sync that starts has changed
 * nothing yet.
 */
export function readsAgain(event: string): boolean {
  // An update's progress changes no page's data, nor does the window that is maximized or restored, which the status says.
  return event !== 'sync_started' && event !== 'update_changed' && event !== 'window_changed'
}

/** How long the page gathers the core's events before it acts on them: one sync sends several in a moment (audit L23). */
export const EVENTS_TOGETHER_MS = 100

/**
 * Gathers what comes within `waitMs` of the first of it, and hands it all to `flush` at once. `stop` drops what still
 * waits, as when the page goes.
 */
export function coalescer<T>(flush: (items: readonly T[]) => void, waitMs: number): { add(item: T): void; stop(): void } {
  let items: T[] = []
  let timer: ReturnType<typeof setTimeout> | null = null
  return {
    add(item) {
      items.push(item)
      if (timer !== null) return
      timer = setTimeout(() => {
        timer = null
        const taken = items
        items = []
        flush(taken)
      }, waitMs)
    },
    stop() {
      if (timer !== null) clearTimeout(timer)
      timer = null
      items = []
    },
  }
}

/**
 * What events of the core taken together ask for, besides one read of the status and the settings, which each of them
 * asks for: one new read of the pages' models if any of them reads again (readsAgain), however many came, and `online`
 * for the pages whose read reaches the online server (Compare), which only the online account's news moves (audit L24).
 */
export function askedBy(events: readonly string[]): { pages: boolean; online: boolean } {
  return { pages: events.some(readsAgain), online: events.includes('online_changed') }
}

/**
 * Runs `task` one at a time (audit L23). A call while it runs asks for one more run after it, which every call meanwhile
 * shares and which begins after the call; `supersede` hears of each such call, so that what the run on its way brings
 * back is taken as old.
 */
export function oneAtATime(task: () => Promise<void>, supersede: () => void = () => undefined): () => Promise<void> {
  let running: Promise<void> | null = null
  let queued: Promise<void> | null = null
  const start = (): Promise<void> => {
    const run: Promise<void> = task().finally(() => {
      if (running === run) running = null
    })
    running = run
    return run
  }
  return () => {
    if (running === null) return start()
    supersede()
    if (queued === null) {
      const next = () => {
        queued = null
        return start()
      }
      queued = running.then(next, next)
    }
    return queued
  }
}

/** A line over the game, as an overlay_line event of the core said it, with an id of its own for the list's keys. */
export type OverlayLine = { id: number; text: string }

/** How long a line stays over the game after it came, and how many show at once. */
const LINE_MS = 6_000
const MOST_LINES = 3

/**
 * The lines over the game (plan 2A-4; stage 2A spec 10): each overlay_line event of the stream adds its text, newest
 * last, which goes six seconds after it came; only the last three show. `heard` takes the event's data and leaves out
 * what holds no text. `show` hears of every change, and `stop` clears what is still waiting to go.
 */
export function overlayLines(show: (lines: readonly OverlayLine[]) => void): {
  heard(data: string): void
  stop(): void
} {
  let lines: readonly OverlayLine[] = []
  let count = 0
  const timers = new Set<ReturnType<typeof setTimeout>>()
  const set = (next: readonly OverlayLine[]) => {
    lines = next
    show(lines)
  }
  return {
    heard(data) {
      let event: unknown
      try {
        event = JSON.parse(data)
      } catch {
        return
      }
      const text = typeof event === 'object' && event !== null ? (event as { text?: unknown }).text : undefined
      if (typeof text !== 'string' || text === '') return
      const line = { id: (count += 1), text }
      set([...lines, line].slice(-MOST_LINES))
      const timer = setTimeout(() => {
        timers.delete(timer)
        if (lines.includes(line)) set(lines.filter((one) => one !== line)) // else a newer one put it out already
      }, LINE_MS)
      timers.add(timer)
    },
    stop() {
      for (const timer of timers) clearTimeout(timer)
      timers.clear()
    },
  }
}

function isImportState(value: unknown): value is PullsImportEvent['state'] {
  return value === 'running' || value === 'done' || value === 'failed'
}

/**
 * The progress of an import (the Pulls plan): each pulls_import event of the stream replaces the one before. `heard`
 * takes the event's data and leaves out what does not say plainly what the import is doing. Progress changes no data, so
 * no page reads again for it; the end of an import publishes pulls_changed as well, and that does. `opened` is the
 * stream's hello, at its start and after a reconnect: the progress from before it is old news, so none is shown, and the
 * pages read the model again, whose import block says how the core's import goes.
 */
export function pullsImports(show: (event: PullsImportEvent | null) => void): {
  heard(data: string): void
  opened(): void
} {
  return {
    opened() {
      show(null)
    },
    heard(data) {
      let event: unknown
      try {
        event = JSON.parse(data)
      } catch {
        return
      }
      if (typeof event !== 'object' || event === null) return
      const { state, pool, pages, found, message } = event as Record<string, unknown>
      if (!isImportState(state)) return
      if (pool !== null && typeof pool !== 'string') return
      if (typeof pages !== 'number' || typeof found !== 'number') return
      if (message !== null && typeof message !== 'string') return
      show({ state, pool, pages, found, message })
    },
  }
}

/** The part of the browser's EventSource that the page listens through, so that a test can send events by hand. */
export type StreamLike = {
  addEventListener(name: string, listener: (event: MessageEvent<string>) => void): void
}

/** The stream of a page that has no core to listen to (the site's recording): it never says anything, and closing it is nothing. */
function idleStream(): StreamLike & { close(): void; onerror: unknown } {
  return { addEventListener: () => undefined, close: () => undefined, onerror: null }
}

/** What the page does with the events of the stream. */
export type StreamHandlers = {
  /** The stream opened: the status as the core had it, as JSON. */
  hello(data: string): void
  /** One of EVENTS came: the pages read again. */
  changed(name: string): void
  /** An overlay_line event, as JSON. */
  line(data: string): void
  /** A pulls_import event, as JSON. No page reads again for it. */
  importing(data: string): void
}

/** Gives each event of the stream to what hears it. */
export function listenTo(stream: StreamLike, on: StreamHandlers): void {
  stream.addEventListener('hello', (event) => on.hello(event.data))
  for (const name of EVENTS) {
    stream.addEventListener(name, () => on.changed(name))
  }
  // A line over the game and the progress of an import change no data: no page reads again for them.
  stream.addEventListener('overlay_line', (event) => on.line(event.data))
  stream.addEventListener('pulls_import', (event) => on.importing(event.data))
}

export type Live = {
  status: Status | null
  settings: Settings | null
  /** Set while the core does not answer. The last status stays on screen. */
  problem: string | null
  /** Goes up whenever the pages should read their data again. */
  version: number
  /** Goes up when the online account's view changes (online_changed), and when the event stream opens. */
  onlineVersion: number
  /** The lines over the game, the last three that came in the last six seconds, newest last (overlayLines). */
  lines: readonly OverlayLine[]
  /** The newest progress of an import, as the stream gave it; null before the first (pullsImports). */
  pullsImport: PullsImportEvent | null
  /** The core's time. */
  now(): Date
  refresh(): Promise<void>
  saveSettings(changes: Partial<Settings>): Promise<void>
}

export function useLive(): Live {
  const [status, setStatus] = useState<Status | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [version, setVersion] = useState(0)
  const [onlineVersion, setOnlineVersion] = useState(0)
  const [lines, setLines] = useState<readonly OverlayLine[]>([])
  const [pullsImport, setPullsImport] = useState<PullsImportEvent | null>(null)
  const [order] = useState(liveOrder)
  const clock = useRef<() => Date>(() => new Date())

  const take = useCallback((next: Status) => {
    clock.current = coreClock(next.now, performance.now())
    setStatus(next)
    setProblem(null)
  }, [])

  const readNow = useCallback(async () => {
    const { isNewest, statusIsNewest } = order.read()
    try {
      // The settings are read along with the status: pausing and resuming change them from elsewhere.
      const [nextStatus, nextSettings] = await Promise.all([
        api.get<Status>('/api/status'),
        api.get<Settings>('/api/settings'),
      ])
      if (!isNewest()) return
      if (statusIsNewest()) take(nextStatus) // else the stream's hello came meanwhile, and its status stays
      setSettings(nextSettings)
    } catch (error) {
      if (isNewest()) setProblem(messageOf(error))
    }
  }, [order, take])
  // One read of the two at a time. One asked for meanwhile runs once after it, and what the read on its way brings back is
  // left, as a read asked for after it is newer (audit L23).
  const refresh = useMemo(
    () =>
      oneAtATime(readNow, () => {
        order.read()
      }),
    [order, readNow],
  )

  // The pictures follow the picture quality at once, whichever window changed it.
  const quality = settings?.picture_quality
  useEffect(() => setPictureQuality(quality), [quality])

  useEffect(() => {
    void refresh()
    const stream = callsAreAnswered() ? idleStream() : new EventSource('/api/events')
    // The events of a tenth of a second are taken together: one read of the status and one new read of the pages for all
    // of them, where a sync's four events made four of each (audit L23).
    const events = coalescer<string>((names) => {
      const asked = askedBy(names)
      void refresh()
      if (asked.pages) setVersion((current) => current + 1)
      if (asked.online) setOnlineVersion((current) => current + 1)
    }, EVENTS_TOGETHER_MS)
    // A line over the game changes no data: the overlay shows it, and no page reads again. Nor does an import's progress.
    const keeper = overlayLines(setLines)
    const progress = pullsImports(setPullsImport)
    listenTo(stream, {
      // The hello comes once each time the stream opens and carries the status itself: the pages read again at once, as
      // what came while the stream was closed reached no page, the online pages too.
      hello: (data) => {
        order.hello() // over the status of every read asked for before it
        take(JSON.parse(data) as Status)
        progress.opened() // an import's progress from before a reconnect does not stay on screen
        if (readsAgain('hello')) setVersion((current) => current + 1)
        setOnlineVersion((current) => current + 1)
      },
      changed: (name) => events.add(name),
      line: (data) => keeper.heard(data),
      importing: (data) => progress.heard(data),
    })
    // The stream reconnects by itself. Asking once here finds out at once whether the core is gone.
    stream.onerror = () => void refresh()
    const timer = window.setInterval(() => void refresh(), REFRESH_EVERY_MS)
    return () => {
      stream.close()
      keeper.stop()
      events.stop()
      window.clearInterval(timer)
    }
  }, [order, refresh, take])

  const saveSettings = useCallback(
    async (changes: Partial<Settings>) => {
      await api.put<Settings>('/api/settings', changes)
      await refresh() // the newest refresh shows what was saved; one that began earlier is ignored
    },
    [refresh],
  )

  return {
    status, settings, problem, version, onlineVersion, lines, pullsImport, now: () => clock.current(), refresh, saveSettings,
  }
}

let readsAsked = 0

/** The number of the newest page model read asked for so far. Reads are numbered in the order they are asked for. */
export function newestRead(): number {
  return readsAsked
}

/**
 * Reads a page model, and reads it again when `path` or `version` changes. The previous data stays meanwhile. `read`
 * is the number of the read the data came from (see newestRead), 0 before the first.
 */
export function useModel<T>(path: string, version: number): { data: T | null; error: string | null; read: number } {
  const [answer, setAnswer] = useState<{ data: T; read: number } | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let current = true
    const reading = new AbortController()
    const read = (readsAsked += 1)
    api.get<T>(path, { signal: reading.signal }).then(
      (next) => {
        if (!current) return
        setAnswer({ data: next, read })
        setError(null)
      },
      (failure: unknown) => {
        if (current) setError(messageOf(failure))
      },
    )
    return () => {
      current = false
      reading.abort() // a read that a newer one took the place of is called off, not only left unused (audit L23)
    }
  }, [path, version])
  return { data: answer === null ? null : answer.data, error, read: answer === null ? 0 : answer.read }
}

/**
 * Re-renders the component that calls it once per interval, for words that depend on the time. It answers how many
 * intervals have passed, which only goes up: a model read on it is read again at each one.
 */
export function useTick(everyMs: number): number {
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const timer = window.setInterval(() => setTick((count) => count + 1), everyMs)
    return () => window.clearInterval(timer)
  }, [everyMs])
  return tick
}
