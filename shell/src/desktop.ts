/**
 * What TUSKFiZZY asks of Windows itself (app.py: Desktop): the Save dialogs, an alert, the data folder in Explorer, and
 * TUSKFiZZY started again, on demo data or after a restore. Nothing here touches Electron: `main.ts` hands the dialogs,
 * the spawn and `app.relaunch` in through `DesktopDeps`.
 */
import { win32 as path } from 'node:path'

/**
 * The arguments that start this program with `extra` after them. Packaged, `guildboard-app.exe` runs alone; in development
 * `electron.exe` is the program and the app's folder must follow it as the first argument.
 */
export function shellArguments(appFolder: string | null, extra: readonly string[]): string[] {
  return appFolder === null ? [...extra] : [appFolder, ...extra]
}

export interface SaveDialogOptions {
  title: string
  defaultPath: string
  filters: { name: string; extensions: string[] }[]
}

export interface DesktopDeps {
  /** The player's Documents and Pictures folders. */
  documents: string
  pictures: string
  /** The program that runs now: `guildboard-app.exe` when packaged, `electron.exe` in development. */
  execPath: string
  /** The app's folder in development, null when packaged. */
  appFolder: string | null
  showSaveDialog(options: SaveDialogOptions): Promise<{ canceled: boolean; filePath?: string }>
  showMessageBox(options: { type: 'error'; title: string; message: string; buttons: string[] }): Promise<unknown>
  /** `shell.openPath`: an empty text when it opened, else why not. */
  openPath(folder: string): Promise<string>
  /** Starts a program of its own, detached from this one. `onError` runs when it cannot be started. */
  spawnDetached(program: string, args: string[], onError: () => void): void
  /** `app.relaunch`: starts this program again once it has exited. */
  relaunch(options: { args: string[] }): void
  /** A line for the log. */
  warn?(text: string): void
}

export class Desktop {
  constructor(private readonly deps: DesktopDeps) {}

  /** The Save dialog for an exported backup, in the Documents folder: the path the player chose, null if they cancelled. */
  chooseBackupFile(name: string): Promise<string | null> {
    return this.save({
      title: 'Export backup',
      defaultPath: path.join(this.deps.documents, name),
      filters: [{ name: 'TUSKFiZZY backup', extensions: ['gbbackup'] }],
    })
  }

  /** The Save dialog for the Wrapped picture, in the Pictures folder: the path the player chose, null if they cancelled. */
  choosePictureFile(name: string): Promise<string | null> {
    return this.save({
      title: 'Save picture',
      defaultPath: path.join(this.deps.pictures, name),
      filters: [{ name: 'PNG picture', extensions: ['png'] }],
    })
  }

  async alert(text: string): Promise<void> {
    await this.deps.showMessageBox({ type: 'error', title: 'TUSKFiZZY', message: text, buttons: ['OK'] })
  }

  async openFolder(folder: string): Promise<void> {
    let failure = ''
    try {
      failure = await this.deps.openPath(folder)
    } catch {
      failure = 'failed'
    }
    if (failure !== '') this.deps.warn?.('The data folder could not be opened.')
  }

  /** TUSKFiZZY on demo data, in a process of its own, with the statement the player has just read. */
  startDemo(): void {
    this.deps.spawnDetached(this.deps.execPath, shellArguments(this.deps.appFolder, ['--demo', '--acknowledged']), () =>
      this.deps.warn?.('The demo could not be started.'),
    )
  }

  /**
   * TUSKFiZZY again, once this one has exited, with its window open. Only the data folder is kept: never `--hidden` (a
   * start with Windows is hidden, and a restart that came back hidden would look like a crash), nor `--demo`.
   */
  startAgain(dataDirArg: string | null): void {
    this.deps.relaunch({ args: shellArguments(this.deps.appFolder, dataDirArg === null ? [] : ['--data-dir', dataDirArg]) })
  }

  private async save(options: SaveDialogOptions): Promise<string | null> {
    const answer = await this.deps.showSaveDialog(options)
    return answer.canceled || !answer.filePath ? null : answer.filePath
  }
}
