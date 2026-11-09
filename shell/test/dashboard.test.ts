import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import {
  Dashboard,
  type DashboardDeps,
  type KeyInput,
  PAGE_SIZES,
  pageSizeOf,
  pageStepOf,
  placeUrl,
  RELEASE_AFTER_MS,
  REFERENCE_WIDTH,
  steppedSize,
  type WebContentsLike,
  type WindowLike,
  WHEEL_TURN_MS,
  type WindowOptions,
  zoomFor,
} from '../src/dashboard.js'
import { readPlacement, type Bounds } from '../src/placement.js'

const LINK = 'http://127.0.0.1:51234/?t=made-up-launch-token'
const ORIGIN = 'http://127.0.0.1:51234'
const ICON = 'Z:\\repo\\shell\\assets\\icon.ico'
const WORK: Bounds[] = [
  { x: 0, y: 0, width: 3440, height: 1400 },
  { x: 3440, y: 0, width: 2560, height: 1400 },
]

test('the page zooms with the window’s width, from 1,640 up to twice', () => {
  expect(REFERENCE_WIDTH).toBe(1640)
  expect(RELEASE_AFTER_MS).toBe(2000)
  expect(zoomFor(1640)).toBe(1)
  expect(zoomFor(2460)).toBe(1.5)
  expect(zoomFor(4000)).toBe(2)
  expect(zoomFor(1200)).toBe(1)
})

test('the page size scales that zoom: 100 is the size for the window, as before the setting', () => {
  expect(PAGE_SIZES).toEqual([70, 80, 90, 100, 110, 120, 130]) // PAGE_SIZES in src/guildboard/config.py
  expect(zoomFor(2460, 100)).toBe(1.5)
  expect(zoomFor(2460, 80)).toBeCloseTo(1.2, 12)
  expect(zoomFor(2560, 90)).toBeCloseTo((2560 / 1640) * 0.9, 12)
  expect(zoomFor(1640, 70)).toBeCloseTo(0.7, 12) // the usual window draws smaller too
  expect(zoomFor(1200, 120)).toBeCloseTo(1.2, 12)
  expect(zoomFor(4000, 130)).toBeCloseTo(2.6, 12)
})

test('a page size the shell does not know is taken as 100', () => {
  for (const size of PAGE_SIZES) expect(pageSizeOf(size)).toBe(size)
  for (const odd of [0, 75, 140, '90', null, undefined, Number.NaN]) expect(pageSizeOf(odd)).toBe(100)
})

test('a step goes to the next size, stays at the ends, and reset goes back to 100', () => {
  expect(steppedSize(100, 'out')).toBe(90)
  expect(steppedSize(100, 'in')).toBe(110)
  expect(steppedSize(70, 'out')).toBe(70)
  expect(steppedSize(130, 'in')).toBe(130)
  expect(steppedSize(80, 'reset')).toBe(100)
  expect(steppedSize(75, 'in')).toBe(100) // not a size: back to 100
})

/** A key press as Electron reports it before the page sees it. */
function press(key: string, code: string, more: Partial<KeyInput> = {}): KeyInput {
  return { type: 'keyDown', key, code, control: true, alt: false, meta: false, shift: false, ...more }
}

test('Ctrl with + or -, also on the number pad, steps the size, and Ctrl with 0 resets it', () => {
  expect(pageStepOf(press('=', 'Equal'))).toBe('in')
  expect(pageStepOf(press('+', 'Equal', { shift: true }))).toBe('in')
  expect(pageStepOf(press('+', 'NumpadAdd'))).toBe('in')
  expect(pageStepOf(press('-', 'Minus'))).toBe('out')
  expect(pageStepOf(press('-', 'NumpadSubtract'))).toBe('out')
  expect(pageStepOf(press('0', 'Digit0'))).toBe('reset')
  expect(pageStepOf(press('0', 'Numpad0'))).toBe('reset')
})

test('anything else is no step: a key let go, no Ctrl, AltGr (Ctrl and Alt), the Windows key, another key', () => {
  expect(pageStepOf(press('-', 'Minus', { type: 'keyUp' }))).toBeNull()
  expect(pageStepOf(press('-', 'Minus', { control: false }))).toBeNull()
  expect(pageStepOf(press('-', 'Minus', { alt: true }))).toBeNull()
  expect(pageStepOf(press('-', 'Minus', { meta: true }))).toBeNull()
  expect(pageStepOf(press('a', 'KeyA'))).toBeNull()
  expect(pageStepOf(press('9', 'Digit9'))).toBeNull()
  // The number pad's 0 with Num Lock off is Insert, and Ctrl+Insert copies.
  expect(pageStepOf(press('Insert', 'Numpad0'))).toBeNull()
})

test('placeUrl: the launch link, with the place; on a shown window, the origin and a fragment', () => {
  expect(placeUrl(LINK, null, false)).toBe(LINK)
  expect(placeUrl(LINK, 'board', false)).toBe(`${LINK}&to=board`)
  expect(placeUrl(LINK, 'achievements/timers', false)).toBe(`${LINK}&to=achievements/timers`)
  expect(placeUrl(LINK, 'board', true)).toBe(`${ORIGIN}/#/board`)
  expect(placeUrl(LINK, 'achievements/timers', true)).toBe(`${ORIGIN}/#/achievements/timers`)
  expect(placeUrl(LINK, null, true)).toBe(LINK)
})

type Listener = (...args: any[]) => void

class FakeContents implements WebContentsLike {
  listeners = new Map<string, Listener[]>()
  loaded: string[] = []
  zoomSet: number[] = []
  zoomNow = 1
  url = ''
  openHandler: ((details: { url: string }) => { action: 'deny' }) | null = null
  loadURL(url: string): Promise<void> {
    this.loaded.push(url)
    this.url = url
    return Promise.resolve()
  }
  getURL(): string {
    return this.url
  }
  setZoomFactor(factor: number): void {
    this.zoomSet.push(factor)
    this.zoomNow = factor
  }
  getZoomFactor(): number {
    return this.zoomNow
  }
  on(event: string, listener: Listener): void {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), listener])
  }
  setWindowOpenHandler(handler: (details: { url: string }) => { action: 'deny' }): void {
    this.openHandler = handler
  }
  emit(event: string, ...args: any[]): void {
    for (const listener of this.listeners.get(event) ?? []) listener(...args)
  }
}

class FakeWindow implements WindowLike {
  webContents = new FakeContents()
  listeners = new Map<string, Listener[]>()
  calls: string[] = []
  width = 1440
  maximized = false
  minimized = false
  visible = false
  destroyed = false
  menu: unknown = 'unset'
  normal: Bounds
  constructor(public options: WindowOptions) {
    this.normal = { x: options.x, y: options.y, width: options.width, height: options.height }
    this.width = options.width
  }
  on(event: string, listener: Listener): void {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), listener])
  }
  emit(event: string, ...args: any[]): void {
    for (const listener of this.listeners.get(event) ?? []) listener(...args)
  }
  setMenu(menu: null): void {
    this.menu = menu
  }
  getContentSize(): [number, number] {
    return [this.width, 900]
  }
  getNormalBounds(): Bounds {
    return this.normal
  }
  isMaximized(): boolean {
    return this.maximized
  }
  isMinimized(): boolean {
    return this.minimized
  }
  isVisible(): boolean {
    return this.visible
  }
  isDestroyed(): boolean {
    return this.destroyed
  }
  maximize(): void {
    this.calls.push('maximize')
    this.maximized = true
  }
  restore(): void {
    this.calls.push('restore')
    this.minimized = false
  }
  show(): void {
    this.calls.push('show')
    this.visible = true
  }
  hide(): void {
    this.calls.push('hide')
    this.visible = false
  }
  focus(): void {
    this.calls.push('focus')
  }
  destroy(): void {
    this.calls.push('destroy')
    this.destroyed = true
  }
}

let folder = ''
let windows: FakeWindow[] = []
let external: string[] = []
let ended = 0

beforeEach(() => {
  folder = mkdtempSync(join(tmpdir(), 'gb-dashboard-'))
  windows = []
  external = []
  ended = 0
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  rmSync(folder, { recursive: true, force: true })
})

function make(overrides: Partial<DashboardDeps> = {}): Dashboard {
  return new Dashboard({
    link: LINK,
    demo: false,
    icon: ICON,
    placementFile: join(folder, 'window.json'),
    displays: () => ({ workAreas: WORK, primary: 0 }),
    createWindow: (options) => {
      const win = new FakeWindow(options)
      windows.push(win)
      return win
    },
    openExternal: (url) => external.push(url),
    onEnded: () => {
      ended += 1
    },
    ...overrides,
  })
}

test('open on a closed dashboard makes the window and loads the link with the place', () => {
  const dashboard = make()
  expect(dashboard.isOpen()).toBe(false)
  expect(dashboard.webContents()).toBeNull()
  dashboard.open('board')
  expect(windows).toHaveLength(1)
  expect(windows[0].webContents.loaded).toEqual([`${LINK}&to=board`])
  expect(dashboard.isOpen()).toBe(true)
  expect(dashboard.webContents()).toBe(windows[0].webContents)
})

test('open with no place loads the plain link', () => {
  make().open()
  expect(windows[0].webContents.loaded).toEqual([LINK])
})

test('open on a shown window goes to the place without a second window', () => {
  const dashboard = make()
  dashboard.open('board')
  dashboard.open('achievements/timers')
  expect(windows).toHaveLength(1)
  expect(windows[0].webContents.loaded).toEqual([`${LINK}&to=board`, `${ORIGIN}/#/achievements/timers`])
  dashboard.open()
  expect(windows[0].webContents.loaded).toHaveLength(2)
})

test('the window: title, background, session, settings and no menu', () => {
  make().open()
  const { options, menu } = windows[0]
  expect(options.title).toBe('TUSKFiZZY')
  expect(options.backgroundColor).toBe('#100E18')
  expect(options.show).toBe(false)
  expect(options.webPreferences).toEqual({
    partition: 'gb-core',
    contextIsolation: true,
    sandbox: true,
    nodeIntegration: false,
    webSecurity: true,
    spellcheck: false,
  })
  expect(menu).toBeNull()
})

test('the demo’s window says so', () => {
  make({ demo: true }).open()
  expect(windows[0].options.title).toBe('TUSKFiZZY - demo data')
})

test('the window has the app’s icon, on the taskbar and in its title bar, and so has one made again', () => {
  const dashboard = make()
  dashboard.open()
  expect(windows[0].options.icon).toBe(ICON)
  dashboard.close()
  vi.advanceTimersByTime(RELEASE_AFTER_MS)
  dashboard.open()
  expect(windows).toHaveLength(2)
  expect(windows[1].options.icon).toBe(ICON)
})

test('the page cannot change the title', () => {
  make().open()
  const event = { preventDefault: vi.fn() }
  windows[0].emit('page-title-updated', event)
  expect(event.preventDefault).toHaveBeenCalledOnce()
})

test('the first time it opens maximized on the second display, then where it was left', () => {
  const first = make()
  first.open()
  expect(windows[0].options).toMatchObject({ x: 3440, y: 0, width: 2560, height: 1400 })
  expect(windows[0].calls).toContain('maximize')
  expect(windows[0].calls).toContain('show')
  windows[0].normal = { x: 3500, y: 60, width: 1640, height: 1100 }
  windows[0].maximized = false
  first.close()
  expect(readPlacement(join(folder, 'window.json'))).toEqual({ x: 3500, y: 60, width: 1640, height: 1100, maximized: false })
  vi.advanceTimersByTime(RELEASE_AFTER_MS)
  first.open()
  expect(windows[1].options).toMatchObject({ x: 3500, y: 60, width: 1640, height: 1100 })
  expect(windows[1].calls).not.toContain('maximize')
})

test('open brings a minimized window back and focuses it', () => {
  const dashboard = make()
  dashboard.open()
  windows[0].minimized = true
  dashboard.open()
  expect(windows[0].calls).toContain('restore')
  expect(windows[0].calls.at(-1)).toBe('focus')
})

test('zoom follows the width on every resize, and after every load', () => {
  const dashboard = make()
  dashboard.open()
  const win = windows[0]
  expect(win.webContents.zoomNow).toBeCloseTo(2560 / 1640, 10) // the first-run window is 2,560 wide
  win.width = 2460
  win.emit('resize')
  expect(win.webContents.zoomNow).toBe(1.5)
  expect(dashboard.zoom()).toBe(1.5)
  win.webContents.zoomNow = 1 // a page loaded again may have dropped it
  win.webContents.emit('did-finish-load')
  expect(win.webContents.zoomNow).toBe(1.5)
  const sets = win.webContents.zoomSet.length
  win.emit('resize') // already at that zoom: nothing is set again
  expect(win.webContents.zoomSet).toHaveLength(sets)
})

test('onResized is called after the zoom is applied', () => {
  const dashboard = make()
  const seen: number[] = []
  dashboard.onResized(() => seen.push(windows[0].webContents.zoomNow))
  dashboard.open()
  windows[0].width = 3280
  windows[0].emit('resize')
  expect(seen).toEqual([2])
})

/** Lets the promises that are due run: the page size's saves. */
async function settle(): Promise<void> {
  for (let i = 0; i < 10; i += 1) await Promise.resolve()
}

const FULL = 2560 / 1640 // the zoom of the first-run window, maximized and 2,560 wide, at 100

/**
 * A dashboard whose saved page sizes are `saved.window` and `saved.maximized`, and whose saves are kept in `saves` to answer.
 * The first-run window is maximized: its steps are the maximized size's.
 */
function sized(window = 100, maximized = 100) {
  const saved = { window, maximized }
  const saves: { size: number; maximized: boolean; done: () => void; fail: () => void }[] = []
  const warnings: string[] = []
  const dashboard = make({
    pageSize: (whenMaximized) => (whenMaximized ? saved.maximized : saved.window),
    choosePageSize: (chosen, whenMaximized) =>
      new Promise<void>((resolve, reject) => {
        saves.push({ size: chosen, maximized: whenMaximized, done: resolve, fail: () => reject(new Error('refused')) })
      }),
    warn: (text) => warnings.push(text),
  })
  return { dashboard, saved, saves, warnings }
}

/** The window is restored to the usual window, 1,640 wide. */
function restore(win: FakeWindow): void {
  win.maximized = false
  win.width = 1640
  win.emit('unmaximize')
  win.emit('resize')
}

/** The window is maximized again on its 2,560 display. */
function maximizeAgain(win: FakeWindow): void {
  win.maximized = true
  win.width = 2560
  win.emit('maximize')
  win.emit('resize')
}

test('a window and a maximized window each draw at their own saved size', () => {
  const { dashboard } = sized(100, 80)
  dashboard.open()
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
  expect(dashboard.zoom()).toBeCloseTo(FULL * 0.8, 12)
  restore(windows[0])
  expect(windows[0].webContents.zoomNow).toBe(1) // the usual window keeps its size
  maximizeAgain(windows[0])
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
})

test('the core hears whether the window is maximized: when it is made, and each time that changes', () => {
  const told: boolean[] = []
  const dashboard = make({ reportMaximized: (maximized) => told.push(maximized) })
  dashboard.open()
  expect(told).toEqual([true]) // the first run's window is maximized
  windows[0].emit('resize') // no change
  restore(windows[0])
  expect(told).toEqual([true, false])
  windows[0].emit('resize')
  maximizeAgain(windows[0])
  expect(told).toEqual([true, false, true])
  dashboard.close()
  vi.advanceTimersByTime(RELEASE_AFTER_MS)
  dashboard.open() // a window made again is told again, whatever was told before
  expect(told).toEqual([true, false, true, true])
})

test('a maximized window that is minimized changes nothing: no zoom, no report, no move of the map views', () => {
  const told: boolean[] = []
  const resized: number[] = []
  const dashboard = make({ reportMaximized: (maximized) => told.push(maximized), pageSize: (maximized) => (maximized ? 80 : 100) })
  dashboard.onResized(() => resized.push(dashboard.zoom()))
  dashboard.open()
  const win = windows[0]
  const zoom = win.webContents.zoomNow
  expect(told).toEqual([true])
  // As Electron 45 has it on Windows: a resize and a minimize, the window saying it is not maximized.
  win.minimized = true
  win.maximized = false
  win.width = 0
  win.emit('resize')
  win.emit('minimize')
  win.emit('unmaximize')
  expect(win.webContents.zoomNow).toBe(zoom)
  expect(told).toEqual([true])
  expect(resized).toEqual([])
  win.webContents.zoomNow = 1 // a load while minimized takes no zoom either
  win.webContents.emit('did-finish-load')
  expect(win.webContents.zoomNow).toBe(1)
  // Restored, maximized as before.
  win.minimized = false
  win.maximized = true
  win.width = 2560
  win.emit('resize')
  win.emit('restore')
  win.emit('maximize')
  expect(win.webContents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
  expect(told).toEqual([true])
})

test('a maximize alone, before any resize, takes the other size', () => {
  const { dashboard } = sized(110, 80)
  dashboard.open()
  windows[0].maximized = false
  windows[0].emit('unmaximize')
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 1.1, 12)
  windows[0].maximized = true
  windows[0].emit('maximize')
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
})

test('Ctrl and - in the window makes the page a step smaller at once, keeps the press from the page, and saves it', async () => {
  const { dashboard, saves } = sized()
  const seen: number[] = []
  dashboard.onResized(() => seen.push(dashboard.zoom()))
  dashboard.open()
  const event = { preventDefault: vi.fn() }
  windows[0].webContents.emit('before-input-event', event, press('-', 'Minus'))
  expect(event.preventDefault).toHaveBeenCalledOnce()
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 0.9, 12)
  expect(seen).toHaveLength(1) // the map views follow the new zoom
  expect(seen[0]).toBeCloseTo(FULL * 0.9, 12)
  await settle()
  expect(saves.map(({ size, maximized }) => [size, maximized])).toEqual([[90, true]])
})

test('a step changes the size of the state the window is in, and leaves the other', async () => {
  const { dashboard, saved, saves } = sized()
  dashboard.open()
  const contents = windows[0].webContents
  contents.emit('before-input-event', { preventDefault: vi.fn() }, press('-', 'Minus')) // maximized: 90
  await settle()
  saved.maximized = 90 // the core keeps it, and then answers
  saves[0].done()
  await settle()
  restore(windows[0])
  expect(contents.zoomNow).toBe(1) // the window's own size is still 100
  contents.emit('before-input-event', { preventDefault: vi.fn() }, press('+', 'Equal', { shift: true })) // a window: 110
  expect(contents.zoomNow).toBeCloseTo(1.1, 12)
  await settle()
  expect(saves.map(({ size, maximized }) => [size, maximized])).toEqual([
    [90, true],
    [110, false],
  ])
  saved.window = 110
  saves[1].done()
  await settle()
  maximizeAgain(windows[0])
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.9, 12)
})

test('another key reaches the page untouched', () => {
  const { dashboard, saves } = sized()
  dashboard.open()
  const event = { preventDefault: vi.fn() }
  windows[0].webContents.emit('before-input-event', event, press('a', 'KeyA'))
  expect(event.preventDefault).not.toHaveBeenCalled()
  expect(saves).toEqual([])
})

test('Ctrl and the mouse wheel step the size as + and - do', async () => {
  const { dashboard, saves } = sized()
  dashboard.open()
  windows[0].webContents.emit('zoom-changed', {}, 'in')
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 1.1, 12)
  await settle()
  expect(saves.map((save) => save.size)).toEqual([110])
})

test('one turn of the wheel is one step, though the engine asks twice for it at once', async () => {
  const { dashboard, saves } = sized()
  dashboard.open()
  const contents = windows[0].webContents
  contents.emit('zoom-changed', {}, 'out')
  contents.emit('zoom-changed', {}, 'out') // the same turn, a fraction of a millisecond later
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.9, 12)
  vi.advanceTimersByTime(WHEEL_TURN_MS)
  contents.emit('zoom-changed', {}, 'out') // the next turn
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
  contents.emit('before-input-event', { preventDefault: vi.fn() }, press('-', 'Minus')) // keys are never held back
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.7, 12)
  await settle()
  expect(saves.map((save) => save.size)).toEqual([90])
})

test('a step that goes nowhere saves nothing: Ctrl and 0 at 100, Ctrl and - at the smallest', async () => {
  const { dashboard, saves } = sized()
  dashboard.open()
  windows[0].webContents.emit('before-input-event', { preventDefault: vi.fn() }, press('0', 'Digit0'))
  const small = sized(100, 70)
  small.dashboard.open()
  windows[1].webContents.emit('before-input-event', { preventDefault: vi.fn() }, press('-', 'Minus'))
  await settle()
  expect(saves).toEqual([])
  expect(small.saves).toEqual([])
})

test('the saves go one after another, and an older saved size does not take the page back meanwhile', async () => {
  const { dashboard, saved, saves } = sized()
  dashboard.open()
  const contents = windows[0].webContents
  contents.emit('before-input-event', { preventDefault: vi.fn() }, press('-', 'Minus'))
  contents.emit('before-input-event', { preventDefault: vi.fn() }, press('-', 'Minus'))
  await settle()
  expect(saves.map((save) => save.size)).toEqual([90]) // the second waits for the first
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
  saved.maximized = 90 // the core says what the first save made it
  dashboard.pageSizeChanged()
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.8, 12) // the player's latest choice stays
  saves[0].done()
  await settle()
  expect(saves.map((save) => save.size)).toEqual([90, 80])
  saves[1].done()
  saved.maximized = 80
  await settle()
  dashboard.pageSizeChanged()
  expect(contents.zoomNow).toBeCloseTo(FULL * 0.8, 12)
  saved.maximized = 120 // chosen in Settings
  dashboard.pageSizeChanged()
  expect(contents.zoomNow).toBeCloseTo(FULL * 1.2, 12)
})

test('a size chosen in Settings takes the open window there, and the map views follow', () => {
  const { dashboard, saved } = sized()
  const seen: number[] = []
  dashboard.onResized(() => seen.push(dashboard.zoom()))
  dashboard.open()
  saved.window = 70 // the other state's: nothing moves
  dashboard.pageSizeChanged()
  expect(seen).toHaveLength(0)
  saved.maximized = 70
  dashboard.pageSizeChanged()
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 0.7, 12)
  expect(seen).toHaveLength(1)
  dashboard.pageSizeChanged() // the same sizes again: nothing moves
  expect(seen).toHaveLength(1)
})

test('a size chosen while the window is closed is the one it opens at', () => {
  const { dashboard, saved } = sized()
  saved.maximized = 110
  dashboard.pageSizeChanged()
  dashboard.open()
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 1.1, 12)
})

test('a save that fails is told in the log, and the page goes back to the saved size', async () => {
  const { dashboard, saves, warnings } = sized()
  dashboard.open()
  windows[0].webContents.emit('before-input-event', { preventDefault: vi.fn() }, press('+', 'NumpadAdd'))
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL * 1.1, 12)
  await settle()
  saves[0].fail()
  await settle()
  expect(warnings).toEqual(['The page size could not be saved.'])
  expect(windows[0].webContents.zoomNow).toBeCloseTo(FULL, 12)
})

test('a closing window takes no step', async () => {
  const { dashboard, saves } = sized()
  dashboard.open()
  const old = windows[0]
  dashboard.close()
  old.webContents.emit('before-input-event', { preventDefault: vi.fn() }, press('-', 'Minus'))
  old.webContents.emit('zoom-changed', {}, 'out')
  await settle()
  expect(saves).toEqual([])
})

test('onFragment reports the fragment after a navigation and an in-page navigation', () => {
  const dashboard = make()
  const seen: string[] = []
  dashboard.onFragment((fragment) => seen.push(fragment))
  dashboard.open()
  windows[0].webContents.emit('did-navigate', {}, `${ORIGIN}/#/map`)
  windows[0].webContents.emit('did-navigate-in-page', {}, `${ORIGIN}/#/board`)
  windows[0].webContents.emit('did-navigate-in-page', {}, `${ORIGIN}/`)
  expect(seen).toEqual(['#/map', '#/board', ''])
})

test('a navigation away from the core’s origin is prevented', () => {
  make().open()
  const contents = windows[0].webContents
  const away = { preventDefault: vi.fn(), url: 'https://example.com/' }
  contents.emit('will-navigate', away, away.url)
  expect(away.preventDefault).toHaveBeenCalledOnce()
  const same = { preventDefault: vi.fn(), url: `${ORIGIN}/#/board` }
  contents.emit('will-navigate', same, same.url)
  expect(same.preventDefault).not.toHaveBeenCalled()
  const redirect = { preventDefault: vi.fn(), url: 'https://example.com/x' }
  contents.emit('will-redirect', redirect)
  expect(redirect.preventDefault).toHaveBeenCalledOnce()
  const lookAlike = { preventDefault: vi.fn(), url: 'http://127.0.0.1:51234.evil.example/' }
  contents.emit('will-navigate', lookAlike, lookAlike.url)
  expect(lookAlike.preventDefault).toHaveBeenCalledOnce()
})

test('a new window opens outside only for the listed addresses, and is always refused', () => {
  make().open()
  const handler = windows[0].webContents.openHandler
  expect(handler).not.toBeNull()
  expect(handler!({ url: 'https://enka.network/u/1' })).toEqual({ action: 'deny' })
  expect(external).toEqual(['https://enka.network/u/1'])
  expect(handler!({ url: 'https://example.com/' })).toEqual({ action: 'deny' })
  expect(handler!({ url: 'file:///C:/Windows/system32/calc.exe' })).toEqual({ action: 'deny' })
  expect(external).toEqual(['https://enka.network/u/1'])
})

test('close hides at once, saves the place, and destroys after 2,000 ms', () => {
  const dashboard = make()
  dashboard.open()
  const win = windows[0]
  win.visible = true
  dashboard.close()
  expect(win.calls.at(-1)).toBe('hide')
  expect(win.destroyed).toBe(false)
  expect(dashboard.isOpen()).toBe(false)
  expect(dashboard.webContents()).toBeNull()
  expect(readPlacement(join(folder, 'window.json'))).not.toBeNull()
  vi.advanceTimersByTime(RELEASE_AFTER_MS - 1)
  expect(win.destroyed).toBe(false)
  vi.advanceTimersByTime(1)
  expect(win.destroyed).toBe(true)
  expect(ended).toBe(0)
})

test('the window’s own close (the X, Alt+F4) is turned into close', () => {
  const dashboard = make()
  dashboard.open()
  const win = windows[0]
  const event = { preventDefault: vi.fn() }
  win.emit('close', event)
  expect(event.preventDefault).toHaveBeenCalledOnce()
  expect(win.calls).toContain('hide')
  expect(dashboard.isOpen()).toBe(false)
})

test('opening again before the old window is released makes a new one, and the old is still released', () => {
  const dashboard = make()
  dashboard.open()
  dashboard.close()
  dashboard.open('board')
  expect(windows).toHaveLength(2)
  expect(windows[1].webContents.loaded).toEqual([`${LINK}&to=board`])
  vi.advanceTimersByTime(RELEASE_AFTER_MS)
  expect(windows[0].destroyed).toBe(true)
  expect(windows[1].destroyed).toBe(false)
  expect(dashboard.isOpen()).toBe(true)
})

test('closing a window that was never shown saves no place', () => {
  const dashboard = make()
  dashboard.open()
  windows[0].visible = false
  dashboard.close()
  expect(readPlacement(join(folder, 'window.json'))).toBeNull()
})

test('in the demo, closing ends the app and the window is not turned into a hide', () => {
  const dashboard = make({ demo: true })
  dashboard.open()
  const win = windows[0]
  win.visible = true
  const event = { preventDefault: vi.fn() }
  win.emit('close', event)
  expect(event.preventDefault).not.toHaveBeenCalled()
  expect(ended).toBe(1)
  dashboard.close()
  expect(ended).toBe(1)
  expect(win.calls).not.toContain('hide')
  expect(readPlacement(join(folder, 'window.json'))).not.toBeNull()
})

test('with no tray (canHide false) a close ends the app as the demo’s does, and the title is not the demo’s', () => {
  const dashboard = make({ canHide: false })
  dashboard.open()
  const win = windows[0]
  win.visible = true
  expect(win.options.title).toBe('TUSKFiZZY')
  const event = { preventDefault: vi.fn() }
  win.emit('close', event)
  expect(event.preventDefault).not.toHaveBeenCalled()
  expect(ended).toBe(1)
  expect(win.calls).not.toContain('hide')
  expect(readPlacement(join(folder, 'window.json'))).not.toBeNull()
})

test('with a tray (canHide true) a close hides, and the app goes on', () => {
  const dashboard = make({ canHide: true })
  dashboard.open()
  const event = { preventDefault: vi.fn() }
  windows[0].emit('close', event)
  expect(event.preventDefault).toHaveBeenCalledOnce()
  expect(ended).toBe(0)
})

test('Windows ending the session saves the place at once and ends the app', () => {
  const dashboard = make()
  dashboard.open()
  const win = windows[0]
  win.visible = true
  expect(readPlacement(join(folder, 'window.json'))).toBeNull()
  win.emit('session-end')
  expect(readPlacement(join(folder, 'window.json'))).not.toBeNull()
  expect(ended).toBe(1)
  win.emit('session-end')
  expect(ended).toBe(1)
})

test('destroy lets the app quit: the window goes at once and a later close is not held back', () => {
  const dashboard = make()
  dashboard.open()
  const win = windows[0]
  dashboard.destroy()
  expect(win.destroyed).toBe(true)
  expect(dashboard.isOpen()).toBe(false)
  const event = { preventDefault: vi.fn() }
  win.emit('close', event)
  expect(event.preventDefault).not.toHaveBeenCalled()
})

test('a window that is already gone is not touched again', () => {
  const dashboard = make()
  dashboard.open()
  dashboard.close()
  windows[0].destroyed = true
  expect(() => vi.advanceTimersByTime(RELEASE_AFTER_MS)).not.toThrow()
  expect(windows[0].calls.filter((call) => call === 'destroy')).toHaveLength(0)
})

test('a placement file that cannot be written does not stop the close', () => {
  const dashboard = make({ placementFile: join(folder, 'no-such-folder', 'window.json') })
  dashboard.open()
  windows[0].visible = true
  expect(() => dashboard.close()).not.toThrow()
  expect(windows[0].calls.at(-1)).toBe('hide')
})

test('destroy saves the place of the open window first, so a quit from the tray keeps it', () => {
  const dashboard = make()
  dashboard.open()
  const win = windows[0]
  win.normal = { x: 3600, y: 80, width: 1700, height: 1000 }
  win.maximized = false
  dashboard.destroy()
  expect(readPlacement(join(folder, 'window.json'))).toEqual({ x: 3600, y: 80, width: 1700, height: 1000, maximized: false })
  expect(win.destroyed).toBe(true)
})

test('destroy with no open window saves nothing, and with a hidden one keeps the place close saved', () => {
  const dashboard = make()
  dashboard.destroy()
  expect(readPlacement(join(folder, 'window.json'))).toBeNull()
  const second = make()
  second.open()
  windows[0].normal = { x: 3500, y: 60, width: 1640, height: 1100 }
  second.close()
  windows[0].normal = { x: 1, y: 1, width: 500, height: 500 } // a hidden window's bounds are not the player's place
  second.destroy()
  expect(readPlacement(join(folder, 'window.json'))).toMatchObject({ x: 3500, y: 60 })
})

test('a closing window no longer feeds onFragment or onResized', () => {
  const dashboard = make()
  const fragments: string[] = []
  let resizes = 0
  dashboard.onFragment((fragment) => fragments.push(fragment))
  dashboard.onResized(() => {
    resizes += 1
  })
  dashboard.open()
  const old = windows[0]
  old.emit('resize')
  old.webContents.emit('did-navigate', {}, `${ORIGIN}/#/map`)
  expect(resizes).toBe(1)
  expect(fragments).toEqual(['#/map'])
  dashboard.close()
  old.emit('resize')
  old.webContents.emit('did-navigate-in-page', {}, `${ORIGIN}/#/board`)
  expect(resizes).toBe(1)
  expect(fragments).toEqual(['#/map'])
  dashboard.open() // the next window feeds them
  windows[1].emit('resize')
  windows[1].webContents.emit('did-navigate', {}, `${ORIGIN}/#/stats`)
  expect(resizes).toBe(2)
  expect(fragments).toEqual(['#/map', '#/stats'])
})
