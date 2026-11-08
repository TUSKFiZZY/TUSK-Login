/**
 * Guild Board's Electron shell (app.py: ShellApp, run_shell). It starts the Python core as a child, hears its events, and
 * wires every module of this folder to Electron: the dashboard, the overlay and its hotkey, the map's views, the login and
 * verification windows, the tray with its balloons, Start with Windows and the desktop's dialogs. The decisions are in those
 * modules and tested there; this file only connects them.
 */
import { spawn } from 'node:child_process'
import { rmSync } from 'node:fs'
import { join, win32 as winPath } from 'node:path'
import {
  app,
  BaseWindow,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  type MenuItemConstructorOptions,
  nativeImage,
  screen,
  session,
  shell,
  Tray,
  WebContentsView,
} from 'electron'
import { parseArgs } from './args.js'
import { GameState, prepareCore, showRequest } from './bootstrap.js'
import { CoreProcess, endedByWindows, killCore, spawnCore } from './core.js'
import { Dashboard, type WindowLike } from './dashboard.js'
import { askDialog, type DialogDeps, type DialogWindowLike } from './dialog.js'
import { Desktop } from './desktop.js'
import { FramedWindow, type FrameDeps, type FrameViewLike, type FrameWindowLike } from './frame.js'
import { hardenApp, hardenDefaultSession, hardenSession, withoutAppName, type Partition, type SessionLike } from './guard.js'
import { Hotkey } from './hotkey.js'
import { claim, type Message, messageFrom, PendingMessages, quitReport } from './instance.js'
import { LoginFlow, type LoginSessionLike, type LoginWindowLike } from './login.js'
import { type ContentViewLike, MapHost, type MapSessionLike, type MapViewLike } from './map.js'
import { Notifier } from './notify.js'
import { exportBackup, savePicture } from './outcomes.js'
import { Overlay, type OverlayWindowLike, type ScreenLike } from './overlay.js'
import { appIconPath, dataFolder, coreCommand } from './paths.js'
import { aliveByTasklist, imageNameOf, pidFile, readPid, removePidIfMine, tryWritePid } from './pid.js'
import type { CallName, CoreEvent, Settings } from './protocol.js'
import { errorName, ShellLog } from './shelllog.js'
import { applyStartWithWindows, isStandardFolder, startupShortcut, type StartupDeps } from './startup.js'
import { TrayMenu, type TrayItem } from './tray.js'
import { type VerifyDeps, VerifyWindow, type VerifyWindowLike } from './verify.js'

// Texts of the Qt shell (app.py).
const STOPPED = 'Guild Board stopped unexpectedly. The log file says why.'
const FRIENDS_PLACE = 'friends' // where an opened invite link is answered
// The one line a refused start writes. Never the argument itself: it can hold 32,000 characters, newlines included.
const REFUSED = 'A debugging switch was refused outside the demo.'
// The switches of Chromium and Node that open a debugging door. They are in `app.commandLine` and not in `process.argv`,
// so `parseArgs` cannot see every spelling (Chromium trims the whole command line before it splits it).
const DEBUGGING_SWITCHES = ['remote-debugging-port', 'remote-debugging-pipe', 'inspect', 'inspect-brk', 'inspect-port']

const log = new ShellLog()
const args = parseArgs(process.argv.slice(app.isPackaged ? 1 : 2))

/** An error in a handler goes to the log by its name alone, and no further: one throw would otherwise end the main process. */
function guarded(what: string, fn: () => unknown): void {
  const failed = (error: unknown): void => log.error(`The ${what} failed (${errorName(error)}).`)
  try {
    const result = fn()
    if (result instanceof Promise) result.catch(failed)
  } catch (error) {
    failed(error)
  }
}

if (!args.demo && (args.refused.length > 0 || DEBUGGING_SWITCHES.some((name) => app.commandLine.hasSwitch(name)))) {
  log.error(REFUSED)
  app.exit(1)
} else {
  void main().catch((error: unknown) => {
    log.error(`Guild Board could not start (${errorName(error)}).`)
    app.exit(1)
  })
}

async function main(): Promise<void> {
  // First of all: a session takes its user agent when it is made.
  app.userAgentFallback = withoutAppName(app.userAgentFallback)
  const folder = dataFolder(args, process.env)
  app.setPath('userData', winPath.join(folder, 'shell'))
  const pidPath = pidFile(folder)

  const started = await claim(
    args,
    {
      requestLock: (message) => app.requestSingleInstanceLock(message),
      readPid: () => readPid(pidPath),
      // Only `--quit` reads it: a folder that cannot be written is a warning, and the start goes on.
      writePid: (pid) => tryWritePid(pidPath, pid, (text) => log.warn(text)),
      alive: (pid) => aliveByTasklist(pid, imageNameOf(app.isPackaged)),
      sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
      now: () => performance.now(),
    },
    process.pid,
  )
  if (started !== 'run') {
    // Another Guild Board holds the folder (and has been handed the message), or `--quit` has done its work: no window.
    if (started === 'not_running') app.releaseSingleInstanceLock()
    const report = quitReport(started)
    if (report.text !== null) process.stdout.write(`${report.text}\n`)
    app.exit(report.code)
    return
  }
  process.on('exit', () => removePidIfMine(pidPath, process.pid))

  // ---- what this run holds

  const core = new CoreProcess(coreCommand(args, folder, app.isPackaged, process.resourcesPath, join(app.getAppPath(), '..')), spawnCore, (pid) => killCore(pid, app.isPackaged))
  const pending = new PendingMessages()
  let settings: Settings | null = null
  let ending = false
  let restarting = false
  let hintGiven = false
  const game = new GameState()
  let dashboard: Dashboard | null = null
  let dashboardWindow: BrowserWindow | null = null
  let mapHost: MapHost | null = null
  let overlay: Overlay | null = null
  let tray: Tray | null = null
  let trayMenu: TrayMenu | null = null
  let notifier: Notifier | null = null

  const coreOrigin = (): string | null => (core.link === null ? null : new URL(core.link).origin)
  const call = (name: CallName, callArgs: object = {}): Promise<unknown> => core.call(name, callArgs)
  const warn = (text: string): void => log.warn(text)

  // ---- the desktop

  const appFolder = app.isPackaged ? null : app.getAppPath()
  const desktop = new Desktop({
    documents: app.getPath('documents'),
    pictures: app.getPath('pictures'),
    execPath: process.execPath,
    appFolder,
    showSaveDialog: (options) => dialog.showSaveDialog(options),
    showMessageBox: (options) => dialog.showMessageBox(options),
    openPath: (path) => shell.openPath(path),
    spawnDetached: (program, programArgs, onError) => {
      try {
        const child = spawn(program, programArgs, { detached: true, stdio: 'ignore' })
        child.once('error', onError) // a program that cannot start is told as an error event
        child.unref()
      } catch {
        onError()
      }
    },
    relaunch: (options) => app.relaunch(options),
    warn,
  })

  // ---- Start with Windows: the shortcut of the standard data folder alone

  const standardFolder = isStandardFolder(folder, args.demo, process.env)
  const startupDeps: StartupDeps = {
    shortcut: startupShortcut(process.env),
    execPath: process.execPath,
    appFolder,
    writeShortcutLink: (shortcut, operation, details) => shell.writeShortcutLink(shortcut, operation, details),
    remove: (file) => rmSync(file, { force: true }),
    warn,
  }

  // ---- the places a click or a message opens

  const openDashboard = (place: string | null): void => {
    if (dashboard === null || ending) return
    // The open overlay stays on top of its display, and a dashboard shown there would be under it: it closes first.
    overlay?.close()
    dashboard.open(place)
  }

  const offerInvite = (code: string): void => {
    call('offer_invite', { code }).catch(() => warn('An invite link could not be handed to the core.'))
  }

  const handle = (message: Message): void => {
    if (message.action === 'show') {
      openDashboard(null)
    } else if (message.action === 'invite') {
      offerInvite(message.code)
      openDashboard(FRIENDS_PLACE)
    }
  }

  // A later start hands its message over as the lock's additional data. A `quit` bypasses what waits for the core.
  app.on('second-instance', (_event, _argv, _directory, data) => {
    guarded('second start', () => {
      const message = messageFrom(data)
      if (message === null) return
      if (message.action === 'quit') void quit(0)
      else pending.take(message, handle)
    })
  })
  // The tray's Open and the icon's click before the core is ready wait with the later starts' messages (Qt: app.py:314).
  const requestShow = showRequest(pending, handle)
  if (args.invite !== null) pending.take({ action: 'invite', code: args.invite }, handle)

  // ---- ending

  /**
   * Everything goes: the overlay first, so that nothing stays over the game taking its clicks, then the windows and the
   * tray, then the core. `alertText` is shown once the windows are gone.
   */
  async function quit(code: number, alertText: string | null = null): Promise<void> {
    if (ending) return
    ending = true // before the alert: it is modal, and the core's end can arrive while it is open
    try {
      guarded('overlay stop', () => overlay?.stop())
      guarded('window close', () => dashboard?.destroy())
      guarded('map close', () => mapHost?.dispose())
      guarded('tray close', () => tray?.destroy())
      if (alertText !== null) await desktop.alert(alertText).catch(() => undefined)
      log.info('Guild Board is ending')
      await core.shutdown().catch(() => undefined)
      log.detach()
      removePidIfMine(pidPath, process.pid)
      if (restarting) {
        app.releaseSingleInstanceLock() // the new one finds the folder free
        guarded('restart', () => desktop.startAgain(args.dataDir === null ? null : folder))
      }
    } finally {
      app.exit(code)
    }
  }

  // ---- Electron

  hardenApp(app)
  // The dashboard hides when closed and its page is released later: no window left must not end the app.
  app.on('window-all-closed', () => undefined)
  app.on('before-quit', (event) => {
    if (ending) {
      dashboard?.destroy()
    } else {
      event.preventDefault()
      void quit(0)
    }
  })
  app.on('browser-window-blur', () => overlay?.windowBlurred())
  process.on('uncaughtException', (error) => log.error(`An uncaught error in the shell (${errorName(error)}).`))
  process.on('unhandledRejection', (error) => log.error(`An unhandled rejection in the shell (${errorName(error)}).`))

  await app.whenReady()
  Menu.setApplicationMenu(null)

  hardenDefaultSession(session.defaultSession as unknown as SessionLike)
  const pagesFolder = join(app.getAppPath(), 'dist', 'pages')
  const partitions: Partition[] = ['gb-core', 'gb-overlay', 'gb-map', 'gb-login', 'gb-verify', 'gb-dialog']
  for (const partition of partitions) {
    hardenSession(session.fromPartition(partition) as unknown as SessionLike, partition, coreOrigin, partition === 'gb-dialog' ? pagesFolder : null)
  }

  // ---- the app's icon: the tray's, and every window's. In a checkout Electron's own program runs, whose windows would
  // otherwise show Electron's icon on the taskbar.

  const appIcon = appIconPath(app.isPackaged, process.resourcesPath, app.getAppPath())

  // ---- the tray

  try {
    const icon = nativeImage.createFromPath(appIcon)
    tray = new Tray(icon)
    tray.setToolTip('TUSKFiZZY')
    const made = tray
    notifier = new Notifier(made, (place) => openDashboard(place))
    trayMenu = new TrayMenu({
      demo: args.demo,
      openWindow: requestShow,
      syncNow: () => call('sync_now'),
      setPaused: (paused) => call('set_paused', { paused }),
      setPinned: (pinned) => call('update_settings', { changes: { overlay_pinned: pinned } }),
      toMainDisplay: () => {
        const asked = call('update_settings', { changes: { overlay_display: '' } })
        // It goes there now, also when the setting names the main display already, and the setting keeps it there.
        const main = screen.getPrimaryDisplay()
        overlay?.placeOn({ id: main.id, bounds: main.bounds, scaleFactor: main.scaleFactor, primary: true })
        overlay?.setZoom(settings?.overlay_size ?? 100)
        return asked
      },
      quit: () => void quit(0),
      setMenu: (items) => made.setContextMenu(Menu.buildFromTemplate(items.map(nativeItem))),
      onClick: (cb) => made.on('click', cb),
      warn,
    })
  } catch {
    // No tray: the window shows at once, so that a hidden start is not an invisible one.
    tray = null
    notifier = null
    trayMenu = null
    warn('The tray icon could not be made.')
  }

  // ---- the login, the verification and the dialogs they use

  const dialogDeps: DialogDeps = {
    pagesFolder,
    preload: join(pagesFolder, 'dialog-preload.js'),
    icon: appIcon,
    ipc: ipcMain,
    createWindow: (options) =>
      new BrowserWindow({ ...options, parent: (options.parent as BaseWindow | null) ?? undefined }) as unknown as DialogWindowLike,
  }
  const frameDeps: FrameDeps = {
    pagesFolder,
    preload: dialogDeps.preload,
    icon: appIcon,
    ipc: ipcMain,
    createWindow: (options) => {
      const window = new BaseWindow(options)
      // A BaseWindow is not covered by `browser-window-blur`.
      window.on('blur', () => overlay?.windowBlurred())
      return window as unknown as FrameWindowLike
    },
    createView: (options) => new WebContentsView({ webPreferences: options.webPreferences }) as unknown as FrameViewLike,
  }
  const login = new LoginFlow({
    call,
    session: session.fromPartition('gb-login') as unknown as LoginSessionLike,
    createWindow: (options) => new FramedWindow(frameDeps, options) as unknown as LoginWindowLike,
    ask: (page, params, parent) => askDialog(dialogDeps, page, params, parent),
    onLoggedIn: () => openDashboard(null),
    icon: appIcon,
    warn,
  })
  const verify = new VerifyWindow({
    call,
    session: session.fromPartition('gb-verify') as unknown as VerifyDeps['session'],
    createWindow: (options) => new FramedWindow(frameDeps, options) as unknown as VerifyWindowLike,
    warn,
  })

  // ---- what the core says

  core.on('event', (event: CoreEvent) => guarded(`${event.event} event`, () => onEvent(event)))
  core.on('stopped', ({ unexpected }: { unexpected: boolean }) => {
    log.detach()
    if (!unexpected || ending) return
    // Windows ends the core at sign-out or shutdown (the console control end), and the shell with it: no alert for that.
    if (endedByWindows(core.exitCode)) void quit(0)
    else void quit(1, STOPPED) // the core died, or never started
  })
  core.start()

  function onEvent(event: CoreEvent): unknown {
    switch (event.event) {
      case 'settings': {
        const previous = settings
        settings = event.settings
        trayMenu?.update(settings)
        if (previous === null || previous.start_with_windows !== settings.start_with_windows) {
          applyStartWithWindows(settings.start_with_windows, standardFolder, startupDeps)
        }
        overlay?.applySettings(settings)
        dashboard?.pageSizeChanged()
        return undefined
      }
      case 'ready':
        return onReady(event.link)
      case 'failed':
        return quit(1, event.text)
      case 'window':
        return onWindow(event.kind)
      case 'notify':
        if (!ending) {
          const shown = notifier?.show(event.kind, event.text, event.place) ?? false
          log.info(shown ? `A ${event.kind} notification was handed to Windows` : `A ${event.kind} notification was not shown: there is no tray icon`)
        }
        return undefined
      case 'game':
        game.heard(event.running) // kept when there is no overlay yet: it is told when it exists
        return undefined
      case 'map':
        mapHost?.follow(event.snapshot)
        return undefined
      case 'finished':
        return undefined // the exit that follows tells whether it was asked for
    }
  }

  function onWindow(kind: string): unknown {
    switch (kind) {
      case 'login':
        login.open()
        return undefined
      case 'verify':
        verify.open()
        return undefined
      case 'data_folder':
        return desktop.openFolder(folder)
      case 'demo':
        if (args.demo) openDashboard(null)
        else desktop.startDemo()
        return undefined
      case 'export_backup':
        return exportBackup({ call, desktop, notify: (k, text, place) => notifier?.show(k, text, place) ?? false, warn })
      case 'save_picture':
        return savePicture({ call, desktop, notify: (k, text, place) => notifier?.show(k, text, place) ?? false, warn })
      case 'restart':
        restarting = true
        return quit(0)
      default:
        return undefined
    }
  }

  /** The window being closed: the first close tells the player that Guild Board goes on in the tray (app.py: _on_hidden_to_tray). */
  function onDashboardHidden(): void {
    mapHost?.windowGone() // the window's page is gone, and the map views with it
    if (hintGiven || settings === null || settings.tray_hint_shown || notifier === null) return
    hintGiven = true
    call('update_settings', { changes: { tray_hint_shown: true } }).catch(() => warn('The tray hint could not be marked as seen.'))
    notifier.hint()
  }

  async function onReady(link: string): Promise<void> {
    if (ending) return
    log.attach(core)
    log.info('Guild Board is starting its windows')
    // The page says that closing the window keeps Guild Board in the tray; in a demo closing the window ends it (app.py:
    // `has_tray = not demo and ...`), so a demo says it has none.
    await prepareCore({ demo: args.demo, acknowledged: args.acknowledged, hasTray: tray !== null && !args.demo, call, warn })
    if (ending) return

    // What waited for the core: a later start's show or invite, and this start's own invite link.
    let show = !args.hidden || tray === null
    let place: string | null = null
    let invite: string | null = null
    pending.ready((message) => {
      if (message.action === 'show') {
        show = true
      } else if (message.action === 'invite') {
        show = true
        place = FRIENDS_PLACE
        invite = message.code
      }
    })

    dashboard = new Dashboard({
      link,
      demo: args.demo,
      icon: appIcon,
      canHide: tray !== null && !args.demo, // with no tray nothing could show a hidden window again
      placementFile: winPath.join(folder, 'window.json'),
      displays: () => {
        const all = screen.getAllDisplays()
        const main = screen.getPrimaryDisplay().id
        return { workAreas: all.map((display) => display.workArea), primary: Math.max(0, all.findIndex((display) => display.id === main)) }
      },
      createWindow: (options) => {
        const window = new BrowserWindow(options)
        dashboardWindow = window
        window.on('close', () => {
          if (!ending && !args.demo && dashboardWindow === window) guarded('window hide', onDashboardHidden)
        })
        window.on('closed', () => {
          if (dashboardWindow === window) {
            dashboardWindow = null
            mapHost?.windowGone()
          }
        })
        return window as unknown as WindowLike
      },
      openExternal: (url) => void shell.openExternal(url),
      onEnded: () => void quit(0),
      warn,
      pageSize: (maximized) => (maximized ? settings?.page_size_maximized : settings?.page_size) ?? 100,
      choosePageSize: (size, maximized) =>
        call('update_settings', { changes: { [maximized ? 'page_size_maximized' : 'page_size']: size } }),
      reportMaximized: (maximized) => {
        call('window_state', { maximized }).catch(() => warn('Whether the window is maximized could not be told to the core.'))
      },
    })
    mapHost = new MapHost({
      call,
      session: session.fromPartition('gb-map') as unknown as MapSessionLike,
      holder: () => (dashboard !== null && dashboard.isOpen() && dashboardWindow !== null && !dashboardWindow.isDestroyed() ? (dashboardWindow.contentView as unknown as ContentViewLike) : null),
      zoom: () => dashboard?.zoom() ?? 1,
      createView: (options) => new WebContentsView({ webPreferences: options.webPreferences }) as unknown as MapViewLike,
      preload: join(app.getAppPath(), 'dist', 'map-preload.js'),
      warn,
    })
    dashboard.onFragment((fragment) => mapHost?.fragment(fragment))
    dashboard.onResized(() => mapHost?.resized(dashboard?.zoom() ?? 1))
    if (show) openDashboard(place)
    call('map_snapshot').then(
      (snapshot) => mapHost?.follow(snapshot as Parameters<MapHost['follow']>[0]),
      () => warn('The map tabs could not be read.'),
    )

    // The overlay is for the game, and so is its hotkey: a demo takes no key from the player.
    if (!args.demo) {
      const hotkey = new Hotkey(
        (accelerator, cb) => globalShortcut.register(accelerator, cb),
        (accelerator) => globalShortcut.unregister(accelerator),
      )
      overlay = new Overlay(
        {
          createWindow: (options) => new BrowserWindow(options) as unknown as OverlayWindowLike,
          icon: appIcon,
          screen: screen as unknown as ScreenLike,
          hotkey,
          report: (displays, hotkeyState) => {
            call('overlay_shell', { displays, hotkey_state: hotkeyState }).catch(() => warn('The overlay’s report could not be told to the core.'))
          },
          anyWindowFocused: () => BaseWindow.getFocusedWindow() !== null,
          openExternal: (url) => void shell.openExternal(url),
          warn,
        },
        link,
      )
      if (settings !== null) overlay.applySettings(settings)
      overlay.start()
      game.attach((running) => overlay?.setGameRunning(running)) // what a `game` event said before the overlay existed
      call('game_running').then(
        (running) => game.answered(running === true), // an event heard meanwhile is newer
        () => warn('Whether the game runs could not be read.'),
      )
    }
    if (invite !== null) offerInvite(invite)
    log.info('Guild Board is running')
  }
}

/** A tray item as Electron's menu takes it. */
function nativeItem(item: TrayItem): MenuItemConstructorOptions {
  if (item.type === 'separator') return { type: 'separator' }
  return {
    label: item.label,
    type: item.type ?? 'normal',
    checked: item.checked,
    click: (menuItem) => item.click?.({ checked: menuItem.checked }),
  }
}
