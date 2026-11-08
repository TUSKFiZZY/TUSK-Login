import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { expect, test } from 'vitest'
import {
  askDialog,
  CANCEL_CHANNEL,
  type DialogDeps,
  type DialogWindowOptions,
  type IpcLike,
  listen,
  pageUrl,
  SUBMIT_CHANNEL,
} from '../src/dialog.js'

const HERE = dirname(fileURLToPath(import.meta.url))
const PAGES_SRC = join(HERE, '..', 'src', 'pages')
const PAGES = resolve('app', 'dist', 'pages')
const ICON = 'Z:\\repo\\shell\\assets\\icon.ico'

type Listener = (event: { sender: unknown }, ...args: unknown[]) => void

class FakeIpc implements IpcLike {
  listeners = new Map<string, Listener[]>()
  on(channel: string, listener: Listener): void {
    this.listeners.set(channel, [...(this.listeners.get(channel) ?? []), listener])
  }
  removeListener(channel: string, listener: Listener): void {
    this.listeners.set(channel, (this.listeners.get(channel) ?? []).filter((item) => item !== listener))
  }
  send(channel: string, sender: unknown, ...args: unknown[]): void {
    for (const listener of [...(this.listeners.get(channel) ?? [])]) listener({ sender }, ...args)
  }
  count(): number {
    return [...this.listeners.values()].reduce((sum, list) => sum + list.length, 0)
  }
}

test('pageUrl is a file address in the pages folder, with the parameters in its query', () => {
  const url = new URL(pageUrl(PAGES, 'choose', { labels: '["A, UID 1", "B & C"]' }))
  expect(url.protocol).toBe('file:')
  expect(url.href.startsWith(pathToFileURL(join(PAGES, 'choose.html')).href)).toBe(true)
  expect(url.searchParams.get('labels')).toBe('["A, UID 1", "B & C"]')
  expect(pageUrl(PAGES, 'paste')).toBe(pathToFileURL(join(PAGES, 'paste.html')).href)
})

test('listen hears its own contents alone, and only a string or a number', () => {
  const ipc = new FakeIpc()
  const mine = {}
  const other = {}
  const heard: unknown[] = []
  const stop = listen(ipc, mine, { submit: (v) => heard.push(v), cancel: () => heard.push('cancel') })
  ipc.send(SUBMIT_CHANNEL, other, 'x')
  ipc.send(CANCEL_CHANNEL, other)
  ipc.send(SUBMIT_CHANNEL, mine, { object: true })
  ipc.send(SUBMIT_CHANNEL, mine, null)
  ipc.send(SUBMIT_CHANNEL, mine, Number.NaN)
  ipc.send(SUBMIT_CHANNEL, mine, 'x'.repeat(70_000))
  expect(heard).toEqual([])
  ipc.send(SUBMIT_CHANNEL, mine, 'hello')
  ipc.send(SUBMIT_CHANNEL, mine, 2)
  ipc.send(CANCEL_CHANNEL, mine)
  expect(heard).toEqual(['hello', 2, 'cancel'])
  stop()
  expect(ipc.count()).toBe(0)
})

class FakeWin {
  closedListeners: (() => void)[] = []
  closed = false
  shown = false
  contents = {
    loaded: [] as string[],
    handlers: new Map<string, (event: { preventDefault(): void }) => void>(),
    opener: null as null | (() => { action: string }),
    loadURL: (url: string) => { this.contents.loaded.push(url); return Promise.resolve() },
    on: (name: string, handler: (event: { preventDefault(): void }) => void) => { this.contents.handlers.set(name, handler) },
    setWindowOpenHandler: (handler: () => { action: string }) => { this.contents.opener = handler },
  }
  get webContents() { return this.contents }
  on(event: string, listener: () => void): void {
    if (event === 'closed') this.closedListeners.push(listener)
  }
  setMenu(): void {}
  show(): void { this.shown = true }
  focus(): void {}
  isDestroyed(): boolean { return this.closed }
  close(): void {
    if (this.closed) return
    this.closed = true
    for (const listener of this.closedListeners) listener()
  }
}

function setup() {
  const ipc = new FakeIpc()
  const windows: FakeWin[] = []
  const options: DialogWindowOptions[] = []
  const deps: DialogDeps = {
    pagesFolder: PAGES,
    preload: 'C:\\app\\dist\\pages\\dialog-preload.js',
    icon: ICON,
    ipc,
    createWindow: (o) => {
      options.push(o)
      const win = new FakeWin()
      windows.push(win)
      return win
    },
  }
  return { ipc, windows, options, deps }
}

test('a paste dialog opens in gb-dialog with the bridge preload, and resolves with the text', async () => {
  const { ipc, windows, options, deps } = setup()
  const parent = {}
  const answer = askDialog(deps, 'paste', { steps: '1. Go.' }, parent)
  expect(options[0]).toMatchObject({
    title: 'Paste a HoYoLAB login',
    parent,
    icon: ICON,
    webPreferences: {
      partition: 'gb-dialog',
      preload: 'C:\\app\\dist\\pages\\dialog-preload.js',
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  })
  expect(windows[0].contents.loaded[0]).toBe(pageUrl(PAGES, 'paste', { steps: '1. Go.' }))
  expect(windows[0].shown).toBe(true)
  expect(windows[0].contents.opener!()).toEqual({ action: 'deny' })
  ipc.send(SUBMIT_CHANNEL, windows[0].contents, 'ltoken_v2=made-up-ltoken')
  expect(await answer).toBe('ltoken_v2=made-up-ltoken')
  expect(windows[0].closed).toBe(true)
  expect(ipc.count()).toBe(0)
})

test('a dialog of one kind takes only its kind of value', async () => {
  const paste = setup()
  const a = askDialog(paste.deps, 'paste', {}, null)
  paste.ipc.send(SUBMIT_CHANNEL, paste.windows[0].contents, 3) // a number is no pasted text
  expect(paste.windows[0].closed).toBe(false)
  paste.ipc.send(CANCEL_CHANNEL, paste.windows[0].contents)
  expect(await a).toBeNull()
  const choose = setup()
  const b = askDialog(choose.deps, 'choose', { labels: '[]' }, null)
  choose.ipc.send(SUBMIT_CHANNEL, choose.windows[0].contents, 'x')
  choose.ipc.send(SUBMIT_CHANNEL, choose.windows[0].contents, 1.5)
  choose.ipc.send(SUBMIT_CHANNEL, choose.windows[0].contents, -1)
  expect(choose.windows[0].closed).toBe(false)
  choose.ipc.send(SUBMIT_CHANNEL, choose.windows[0].contents, 1)
  expect(await b).toBe(1)
})

test('the chooser has the app’s icon, as the paste dialog has', async () => {
  const { ipc, windows, options, deps } = setup()
  const answer = askDialog(deps, 'choose', { labels: '[]' }, null)
  expect(options[0]).toMatchObject({ title: 'Choose a game account', icon: ICON })
  ipc.send(CANCEL_CHANNEL, windows[0].contents)
  expect(await answer).toBeNull()
})

test('closing the window cancels, and a dialog never navigates away from its page', async () => {
  const { windows, deps } = setup()
  const answer = askDialog(deps, 'choose', { labels: '[]' }, null)
  for (const name of ['will-navigate', 'will-frame-navigate', 'will-redirect']) {
    let prevented = false
    windows[0].contents.handlers.get(name)!({ preventDefault: () => { prevented = true } })
    expect(prevented, name).toBe(true)
  }
  windows[0].close()
  expect(await answer).toBeNull()
})

test('a page that does not load cancels the dialog', async () => {
  const { windows, deps } = setup()
  deps.createWindow = (): FakeWin => {
    const win = new FakeWin()
    win.contents.loadURL = () => Promise.reject(new Error('gone'))
    windows.push(win)
    return win
  }
  expect(await askDialog(deps, 'paste', {}, null)).toBeNull()
  expect(windows[0].closed).toBe(true)
})

// ---- the preload and the pages

function runDialogPreload() {
  const source = readFileSync(join(PAGES_SRC, 'dialog-preload.ts'), 'utf8')
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const exposed: Record<string, Record<string, (...args: unknown[]) => void>> = {}
  const sent: unknown[][] = []
  const electron = {
    contextBridge: { exposeInMainWorld: (name: string, api: Record<string, (...args: unknown[]) => void>) => { exposed[name] = api } },
    ipcRenderer: { send: (...args: unknown[]) => { sent.push(args) } },
  }
  runInNewContext(js, { require: (name: string) => (name === 'electron' ? electron : undefined) })
  return { exposed, sent }
}

test('the dialog preload exposes window.gbDialog = { submit, cancel } and nothing else', () => {
  const { exposed, sent } = runDialogPreload()
  expect(Object.keys(exposed)).toEqual(['gbDialog'])
  expect(Object.keys(exposed.gbDialog).sort()).toEqual(['cancel', 'submit'])
  exposed.gbDialog.submit('text')
  exposed.gbDialog.submit(2)
  exposed.gbDialog.submit({ evil: true })
  exposed.gbDialog.cancel()
  expect(sent).toEqual([[SUBMIT_CHANNEL, 'text'], [SUBMIT_CHANNEL, 2], [CANCEL_CHANNEL]])
})

test('the channel names in the preload are the ones the shell listens on', () => {
  const source = readFileSync(join(PAGES_SRC, 'dialog-preload.ts'), 'utf8')
  expect(source).toContain(`'${SUBMIT_CHANNEL}'`)
  expect(source).toContain(`'${CANCEL_CHANNEL}'`)
})

test('every page carries the CSP default-src \'self\', and fetches nothing remote', () => {
  const pages = readdirSync(PAGES_SRC).filter((name) => name.endsWith('.html'))
  expect(pages.sort()).toEqual(['bar.html', 'choose.html', 'paste.html'])
  for (const name of pages) {
    const html = readFileSync(join(PAGES_SRC, name), 'utf8')
    expect(html, name).toContain(`<meta http-equiv="Content-Security-Policy" content="default-src 'self'">`)
    for (const tag of html.match(/<script\b[^>]*>/gi) ?? []) expect(tag, `${name} has an inline script`).toMatch(/\bsrc="[a-z-]+\.js"/)
    expect(/<script\b[^>]*>\s*[^<\s]/i.test(html), `${name} has an inline script`).toBe(false)
    expect(/\bstyle\s*=/i.test(html), `${name} has an inline style`).toBe(false)
    expect(/<style/i.test(html), `${name} has a style element`).toBe(false)
  }
  for (const name of readdirSync(PAGES_SRC).filter((n) => /\.(html|css|js)$/.test(n))) {
    const text = readFileSync(join(PAGES_SRC, name), 'utf8')
    expect(/https?:\/\//i.test(text), `${name} names a remote address`).toBe(false)
    expect(/\bfetch\s*\(|XMLHttpRequest|WebSocket|import\s*\(/.test(text), `${name} fetches`).toBe(false)
    expect(/innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\s*\(/.test(text), `${name} writes markup`).toBe(false)
  }
})

test('the pages are in the interface’s colours and say the Qt dialogs’ words', () => {
  const css = readFileSync(join(PAGES_SRC, 'dialog.css'), 'utf8')
  expect(css).toContain('#100E18')
  expect(css).toContain('#39334C')
  const paste = readFileSync(join(PAGES_SRC, 'paste.html'), 'utf8')
  expect(paste).toContain('ltoken_v2=...; ltuid_v2=...; ltmid_v2=...')
  expect(paste).toContain('Use this login')
  expect(paste).toContain('Cancel')
  const choose = readFileSync(join(PAGES_SRC, 'choose.html'), 'utf8')
  expect(choose).toContain('This HoYoLAB account holds more than one Genshin Impact game account. TUSKFiZZY follows one of them.')
  expect(choose).toContain('Follow this account')
  expect(choose).toContain('Cancel login')
  const bar = readFileSync(join(PAGES_SRC, 'bar.html'), 'utf8')
  expect(bar).toContain('Try again')
  expect(bar).toContain('Paste a login instead')
})

// ---- the keyboard: the pages' scripts run against a small stand-in for the document

type KeyHandler = (event: { key: string; target?: { tagName: string }; preventDefault(): void }) => void

function runPage(name: string, search: string) {
  const sent: unknown[][] = []
  const elements = new Map<string, any>()
  const element = (id: string) => {
    if (!elements.has(id)) {
      elements.set(id, { id, value: '', textContent: '', clicks: [] as (() => void)[], addEventListener(type: string, fn: () => void) { if (type === 'click') this.clicks.push(fn) }, focus() {} })
    }
    return elements.get(id)
  }
  const keys: KeyHandler[] = []
  const rows: any[] = []
  const list = element('list')
  list.append = (row: any) => rows.push(row)
  list.querySelector = () => {
    const checked = rows.map((row) => row.radio).find((radio) => radio.checked)
    return checked ?? null
  }
  const document = {
    getElementById: element,
    createElement: () => {
      const made: any = { addEventListener() {}, append(radio: any) { made.radio = radio } }
      return made
    },
    addEventListener: (type: string, fn: KeyHandler) => { if (type === 'keydown') keys.push(fn) },
  }
  const window = { gbDialog: { submit: (...args: unknown[]) => sent.push(['submit', ...args]), cancel: () => sent.push(['cancel']) } }
  runInNewContext(readFileSync(join(PAGES_SRC, name), 'utf8'), { document, window, location: { search }, URLSearchParams, JSON, Array, String, Number })
  const press = (key: string, tagName?: string) => {
    let prevented = false
    for (const fn of keys) fn({ key, target: tagName ? { tagName } : undefined, preventDefault: () => { prevented = true } })
    return prevented
  }
  return { sent, press, element }
}

test('Escape cancels the chooser, and Enter presses its default button unless a button has the focus', () => {
  const page = runPage('choose.js', `?labels=${encodeURIComponent(JSON.stringify(['A', 'B']))}`)
  page.press('Enter', 'INPUT')
  page.press('Enter', 'DIV')
  expect(page.sent).toEqual([['submit', 0], ['submit', 0]])
  page.press('Enter', 'BUTTON') // the button presses itself
  page.press('a')
  expect(page.sent).toHaveLength(2)
  page.press('Escape')
  expect(page.sent.at(-1)).toEqual(['cancel'])
  page.element('follow').clicks[0]()
  expect(page.sent.at(-1)).toEqual(['submit', 0])
  page.element('cancel').clicks[0]()
  expect(page.sent.at(-1)).toEqual(['cancel'])
})

test('Escape cancels the paste dialog, and Enter is left to the box as a new line', () => {
  const page = runPage('paste.js', `?steps=${encodeURIComponent('1. Go.')}`)
  expect(page.element('steps').textContent).toBe('1. Go.')
  expect(page.press('Enter', 'TEXTAREA')).toBe(false)
  expect(page.sent).toEqual([])
  page.press('Escape')
  expect(page.sent).toEqual([['cancel']])
  page.element('box').value = 'ltoken_v2=made-up-ltoken'
  page.element('use').clicks[0]()
  expect(page.sent.at(-1)).toEqual(['submit', 'ltoken_v2=made-up-ltoken'])
  expect(page.element('box').value).toBe('')
})
