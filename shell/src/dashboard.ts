/**
 * The dashboard window (dashboard.py): the interface in a window that may show only the core's own pages. Closing hides
 * it, and the page is released a moment later, so only the core keeps running. The page zooms with the window's width,
 * from the usual 1,640 pixels up to twice the size, times the player's page size (Settings, or Ctrl and + or -), which a
 * maximized window has apart. Nothing here touches Electron: `main.ts` hands the window in through
 * `DashboardDeps.createWindow`.
 */
import { opensOutside, sameOrigin } from './links.js'
import { type Bounds, readPlacement, restoredBounds, writePlacement } from './placement.js'

// A closed window's page is let go this long after it hides: what it sends as it hides, a note's save among them, is
// not cut off with it (Notes.tsx).
export const RELEASE_AFTER_MS = 2_000
// The window width at which the page draws at its own size: the player's usual window. A wider window zooms the page to
// fill it, and a narrower one keeps today's narrow layout.
export const REFERENCE_WIDTH = 1640
// `--page-dark` (web/src/styles/tokens.css): the window opens in the page's colour, with no white flash.
export const BACKGROUND = '#100E18'

// The page sizes, in percent of the size the page grows to with the window: 100 fills the window as before the setting
// (PAGE_SIZES in src/guildboard/config.py). Ctrl and + or -, and Ctrl and the mouse wheel, step through them.
export const PAGE_SIZES: readonly number[] = [70, 80, 90, 100, 110, 120, 130]
const USUAL_SIZE = 100
// One turn of the mouse wheel with Ctrl: the engine asks for the zoom twice for it, a fraction of a millisecond apart (seen
// in the shell's e2e test with Electron 45), so a second ask this soon after a step is the same turn. A fast spin takes a
// step every this many milliseconds.
export const WHEEL_TURN_MS = 50

/** How large the page draws in a window this wide: the width over 1,640, from 1 to 2, times the page size. */
export function zoomFor(width: number, pageSize = USUAL_SIZE): number {
  return (Math.min(2, Math.max(1, width / REFERENCE_WIDTH)) * pageSize) / 100
}

/** The page size a setting holds: one of PAGE_SIZES, and 100 for anything else. */
export function pageSizeOf(value: unknown): number {
  return typeof value === 'number' && PAGE_SIZES.includes(value) ? value : USUAL_SIZE
}

export type PageStep = 'in' | 'out' | 'reset'

/** The size a step leads to: the next larger or smaller, staying at the ends; a reset, and any size not listed, is 100. */
export function steppedSize(size: number, step: PageStep): number {
  const at = PAGE_SIZES.indexOf(size)
  if (step === 'reset' || at < 0) return USUAL_SIZE
  const next = at + (step === 'in' ? 1 : -1)
  return PAGE_SIZES[Math.max(0, Math.min(PAGE_SIZES.length - 1, next))]
}

/** A key press as Electron reports it before the page sees it (`before-input-event`). */
export interface KeyInput {
  type: string
  key: string
  code: string
  control: boolean
  alt: boolean
  meta: boolean
  shift?: boolean
}

/**
 * The step a key press asks for, as a browser's: Ctrl with + (or =), with - and with 0, also on the number pad, by the
 * character the key types. Null for anything else: the number pad's 0 with Num Lock off is Insert, and Ctrl+Insert copies.
 * Null for a press with Alt too: Windows reports AltGr, which types on the player's Polish layout, as Ctrl+Alt. Ctrl+0 can
 * be no overlay hotkey (hotkeys.py).
 */
export function pageStepOf(input: KeyInput): PageStep | null {
  if (input.type !== 'keyDown' || !input.control || input.alt || input.meta) return null
  if (input.key === '+' || input.key === '=') return 'in'
  if (input.key === '-') return 'out'
  if (input.key === '0') return 'reset'
  return null
}

/**
 * The address to load for a place such as "achievements/timers". A window not shown yet takes the place with its launch
 * link; a shown one goes there with a fragment, which the page follows without loading again.
 */
export function placeUrl(link: string, place: string | null, shown: boolean): string {
  if (place === null) return link
  if (shown) return `${new URL(link).origin}/#/${place}`
  return `${link}&to=${place}`
}

interface NavigationEvent {
  preventDefault(): void
  url?: string
}

/** The part of an Electron web contents that the dashboard uses. */
export interface WebContentsLike {
  loadURL(url: string): Promise<void>
  getURL(): string
  setZoomFactor(factor: number): void
  getZoomFactor(): number
  on(event: string, listener: (...args: any[]) => void): unknown
  setWindowOpenHandler(handler: (details: { url: string }) => { action: 'deny' }): unknown
}

/** The part of an Electron BrowserWindow that the dashboard uses. */
export interface WindowLike {
  webContents: WebContentsLike
  on(event: string, listener: (...args: any[]) => void): unknown
  setMenu(menu: null): void
  getContentSize(): [number, number]
  getNormalBounds(): Bounds
  isMaximized(): boolean
  isMinimized(): boolean
  isVisible(): boolean
  isDestroyed(): boolean
  maximize(): void
  restore(): void
  show(): void
  hide(): void
  focus(): void
  destroy(): void
}

export interface WindowOptions extends Bounds {
  title: string
  /** The app's icon file (`appIconPath`), for the taskbar and the title bar. */
  icon: string
  backgroundColor: string
  show: false
  webPreferences: {
    partition: 'gb-core'
    contextIsolation: true
    sandbox: true
    nodeIntegration: false
    webSecurity: true
    spellcheck: false
  }
}

export interface DashboardDeps {
  /** The core's launch link. */
  link: string
  demo: boolean
  /** The app's icon file (`appIconPath`). */
  icon: string
  /**
   * Whether a close only hides the window, the app going on in the tray. False when there is no tray: nothing could
   * show the window again, so the close ends the app, as the demo's does. Default true.
   */
  canHide?: boolean
  /** `<data folder>\window.json`. */
  placementFile: string
  /** The displays' work areas, left to right as the system lists them, and the index of the main one. */
  displays(): { workAreas: Bounds[]; primary: number }
  createWindow(options: WindowOptions): WindowLike
  /** Opens an address in the player's own browser. */
  openExternal(url: string): void
  /** The app ends: the demo's window or a window with no tray to hide in was closed, or Windows is ending the session. */
  onEnded(): void
  /** A line for the log; the placement file that cannot be written is told of here. */
  warn?(text: string): void
  /** The saved page size of a window, or of a maximized one (the settings `page_size` and `page_size_maximized`). Default 100. */
  pageSize?(maximized: boolean): number
  /** Saves a page size the player stepped to in the window, a maximized one's or not. */
  choosePageSize?(size: number, maximized: boolean): Promise<unknown>
  /** Tells the core whether the window is maximized (`window_state`): the Board keeps an arrangement for each. */
  reportMaximized?(maximized: boolean): void
}

/** The page's two sizes: one for a window, and one for a maximized window, which a full screen may want smaller. */
interface PageSizes {
  window: number
  maximized: number
}

export class Dashboard {
  private window: WindowLike | null = null
  // Windows that were closed and wait to be released: a window is destroyed RELEASE_AFTER_MS after it hides.
  private readonly releasing = new Map<WindowLike, ReturnType<typeof setTimeout>>()
  private readonly resizedCallbacks: (() => void)[] = []
  private readonly fragmentCallbacks: ((fragment: string) => void)[] = []
  private ending = false // the app is quitting, or the demo's window was closed: a close is no longer turned into a hide
  private ended = false
  private sizes: PageSizes // the page sizes the page draws at
  // The page sizes stepped to in the window, saved one after another. While any is unsaved, the sizes the core reports are
  // older ones, and the player's latest step stays.
  private saving: Promise<unknown> = Promise.resolve()
  private unsaved = 0
  private wheeled = Number.NEGATIVE_INFINITY // when the wheel last took a step
  private reported: boolean | null = null // whether the window is maximized, as the core was last told

  constructor(private readonly deps: DashboardDeps) {
    this.sizes = this.savedSizes()
  }

  isOpen(): boolean {
    return this.window !== null && !this.window.isDestroyed()
  }

  webContents(): WebContentsLike | null {
    return this.isOpen() ? this.window!.webContents : null
  }

  /** How large the page draws now: 1 while there is no window. */
  zoom(): number {
    return this.isOpen() ? zoomFor(this.window!.getContentSize()[0], this.sizeOf(this.window!)) : 1
  }

  /** The saved page sizes changed, as when one is chosen in Settings: the page takes them, unless a step is still saving. */
  pageSizeChanged(): void {
    if (this.unsaved === 0) this.resize(this.savedSizes())
  }

  private savedSizes(): PageSizes {
    return {
      window: pageSizeOf(this.deps.pageSize?.(false) ?? USUAL_SIZE),
      maximized: pageSizeOf(this.deps.pageSize?.(true) ?? USUAL_SIZE),
    }
  }

  /** The page size for the window as it is now, maximized or not. */
  private sizeOf(win: WindowLike): number {
    return win.isMaximized() ? this.sizes.maximized : this.sizes.window
  }

  /**
   * A step in the window, of the size for the window as it is now: the page takes it at once, and it is saved after any
   * step before it.
   */
  private step(win: WindowLike, step: PageStep): void {
    if (this.window !== win) return // a window that is closing
    const maximized = win.isMaximized()
    const current = this.sizeOf(win)
    const size = steppedSize(current, step)
    if (size === current) return
    this.resize(maximized ? { ...this.sizes, maximized: size } : { ...this.sizes, window: size })
    this.unsaved += 1
    this.saving = this.saving
      .then(() => this.deps.choosePageSize?.(size, maximized))
      .then(
        () => undefined,
        () => this.deps.warn?.('The page size could not be saved.'),
      )
      .finally(() => {
        this.unsaved -= 1
        this.pageSizeChanged() // the saved size, which is the latest step unless a save failed
      })
  }

  /** Keeps these page sizes, and the page takes the one for its window. */
  private resize(sizes: PageSizes): void {
    if (sizes.window === this.sizes.window && sizes.maximized === this.sizes.maximized) return
    this.sizes = sizes
    this.rezoom()
  }

  /** The page takes the zoom for its window as it is now; when that is a new zoom, the map views follow it. */
  private rezoom(): void {
    const win = this.window
    if (win === null || win.isDestroyed()) return
    if (this.applyZoom(win)) for (const cb of this.resizedCallbacks) cb()
  }

  /** `cb` is called after the window changed size and the page has taken its zoom: the map views follow. */
  onResized(cb: () => void): void {
    this.resizedCallbacks.push(cb)
  }

  /** `cb` is called with the page's fragment, such as "#/map" (empty when there is none), after each navigation. */
  onFragment(cb: (fragment: string) => void): void {
    this.fragmentCallbacks.push(cb)
  }

  /**
   * Shows the window; with a place, such as "achievements/timers", on that place. A window not made yet takes the place
   * with its launch link, and a shown one goes there.
   */
  open(place: string | null = null): void {
    if (!this.isOpen()) {
      this.make(place)
    } else if (place !== null) {
      void this.window!.webContents.loadURL(placeUrl(this.deps.link, place, true)).catch(() => undefined)
    }
    const win = this.window!
    if (win.isMinimized()) win.restore()
    win.show()
    win.focus()
  }

  /** Whether a close hides the window: with a tray, and not in the demo. Otherwise it ends the app. */
  private get hides(): boolean {
    return !this.deps.demo && this.deps.canHide !== false
  }

  /** Saves the place, hides the window and releases it later. In the demo, and with no tray, closing ends the app. */
  close(): void {
    const win = this.window
    if (!this.hides) {
      if (win !== null && !win.isDestroyed()) this.savePlacement(win)
      this.end()
      return
    }
    if (win === null || win.isDestroyed()) return
    this.savePlacement(win)
    win.hide()
    this.window = null
    this.releasing.set(
      win,
      setTimeout(() => this.release(win), RELEASE_AFTER_MS),
    )
  }

  /** As the app quits: every window goes at once, and a close is no longer held back. */
  destroy(): void {
    this.ending = true
    // As a quit from the tray with the window open: its place is kept first (a hidden window's is already saved).
    if (this.window !== null && !this.window.isDestroyed()) this.savePlacement(this.window)
    for (const [win, timer] of this.releasing) {
      clearTimeout(timer)
      this.releasing.delete(win)
      if (!win.isDestroyed()) win.destroy()
    }
    const win = this.window
    this.window = null
    if (win !== null && !win.isDestroyed()) win.destroy()
  }

  private make(place: string | null): void {
    const { workAreas, primary } = this.deps.displays()
    const { bounds, maximized } = restoredBounds(readPlacement(this.deps.placementFile), workAreas, primary)
    const win = this.deps.createWindow({
      ...bounds,
      title: this.deps.demo ? 'Guild Board - demo data' : 'Guild Board',
      icon: this.deps.icon,
      backgroundColor: BACKGROUND,
      show: false,
      webPreferences: {
        partition: 'gb-core',
        contextIsolation: true,
        sandbox: true,
        nodeIntegration: false,
        webSecurity: true,
        spellcheck: false,
      },
    })
    this.window = win
    win.setMenu(null)
    this.wire(win)
    void win.webContents.loadURL(placeUrl(this.deps.link, place, false)).catch(() => undefined)
    if (maximized) win.maximize()
    // At once, so that the page does not paint at 100% until it has loaded, and after the maximize: a maximized window has
    // a page size of its own.
    this.applyZoom(win)
    this.reported = null // a window made again is told of, whatever was told before
    this.report(win)
  }

  /** Tells the core whether the window is maximized, when that is not what it was told last. */
  private report(win: WindowLike): void {
    if (win.isDestroyed() || win.isMinimized()) return // minimized, it says it is not maximized: the restore tells
    const maximized = win.isMaximized()
    if (maximized === this.reported) return
    this.reported = maximized
    this.deps.reportMaximized?.(maximized)
  }

  private wire(win: WindowLike): void {
    const contents = win.webContents
    const origin = new URL(this.deps.link).origin
    win.on('page-title-updated', (event: NavigationEvent) => event.preventDefault()) // the title is ours
    win.on('resize', () => {
      // A maximized window that is minimized sends a resize and says it is not maximized (Electron 45 on Windows): nothing
      // changes until it is restored, so that the page, the map views and the Board's arrangement stay as they are.
      if (win.isMinimized()) return
      this.applyZoom(win) // a maximize, a restore and any change of the window's width arrive as resizes
      if (this.window !== win) return // a window that is closing
      for (const cb of this.resizedCallbacks) cb()
      this.report(win)
    })
    // A maximized window and one that is not have a page size each, and the Board an arrangement each: whichever comes
    // first, the resize or these, the page takes the size of the state the window is in, and the core hears of it.
    const changedState = (): void => {
      if (this.window !== win || win.isMinimized()) return
      this.rezoom()
      this.report(win)
    }
    win.on('maximize', changedState)
    win.on('unmaximize', changedState)
    win.on('close', (event: NavigationEvent) => {
      if (this.ending) return
      if (!this.hides) {
        this.close()
        return
      }
      event.preventDefault() // the X and Alt+F4 hide the window; the app goes on in the tray
      this.close()
    })
    // Windows is ending the session (logoff or shutdown): the place is kept at once, and the app starts to end. Best effort:
    // Windows may end the process soon after.
    win.on('session-end', () => {
      if (!win.isDestroyed()) this.savePlacement(win)
      this.end()
    })
    win.on('closed', () => {
      if (this.window === win) this.window = null
    })
    // Chromium keeps a zoom with the page it has loaded, so a page loaded again may have dropped it: each load asks again.
    contents.on('did-finish-load', () => this.applyZoom(win))
    // Ctrl and + or -, and Ctrl and 0, step the page size, and the page never sees the press. Ctrl and the mouse wheel only
    // ask for a zoom (Electron zooms nothing itself): they step it too.
    contents.on('before-input-event', (event: NavigationEvent, input: KeyInput) => {
      const step = pageStepOf(input)
      if (step === null) return
      event.preventDefault()
      this.step(win, step)
    })
    contents.on('zoom-changed', (_event: unknown, direction: string) => {
      const now = performance.now() // never goes back, as the clock can when it is set
      if (now - this.wheeled < WHEEL_TURN_MS) return
      this.wheeled = now
      this.step(win, direction === 'in' ? 'in' : 'out')
    })
    const kept = (event: NavigationEvent, url?: string): void => {
      if (!sameOrigin(url ?? event.url ?? '', origin)) event.preventDefault()
    }
    contents.on('will-navigate', kept)
    contents.on('will-redirect', kept)
    contents.setWindowOpenHandler(({ url }) => {
      if (opensOutside(url)) this.deps.openExternal(url)
      return { action: 'deny' }
    })
    const fragment = (_event: unknown, url: string): void => {
      if (this.window !== win) return // a window that is closing no longer tells where the page is
      let hash: string
      try {
        hash = new URL(url).hash
      } catch {
        return
      }
      for (const cb of this.fragmentCallbacks) cb(hash)
    }
    contents.on('did-navigate', fragment)
    contents.on('did-navigate-in-page', fragment)
  }

  /**
   * Zooms the page to the window's width and the page size when it is not at that zoom already: the page is asked, not a
   * note kept.
   */
  private applyZoom(win: WindowLike): boolean {
    if (win.isDestroyed() || win.isMinimized()) return false // the restore applies it
    const factor = zoomFor(win.getContentSize()[0], this.sizeOf(win))
    // The engine gives a factor back with a rounding error in its last digit.
    if (Math.abs(win.webContents.getZoomFactor() - factor) <= 1e-6) return false
    win.webContents.setZoomFactor(factor)
    return true
  }

  private savePlacement(win: WindowLike): void {
    if (!win.isVisible()) return // a window never shown has no place to remember
    try {
      writePlacement(this.deps.placementFile, { ...win.getNormalBounds(), maximized: win.isMaximized() })
    } catch {
      this.deps.warn?.('The window’s place could not be kept in the data folder.')
    }
  }

  private release(win: WindowLike): void {
    this.releasing.delete(win)
    if (!win.isDestroyed()) win.destroy()
  }

  private end(): void {
    this.ending = true
    if (this.ended) return
    this.ended = true
    this.deps.onEnded()
  }
}
