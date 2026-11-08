/** Where the data folder is, and the command that starts the core. Windows paths throughout. */
import * as os from 'node:os'
import { win32 as path } from 'node:path'
import type { ShellArgs } from './args.js'

/**
 * The core's own rule: `--data-dir`, else `TUSKFIZZY_DATA_DIR`, else `%LOCALAPPDATA%\GuildBoard` (the demo: `TUSKFiZZY-demo`).
 * A relative folder is made absolute here: the core would read it against its own folder, and Electron refuses it.
 */
export function dataFolder(args: ShellArgs, env: NodeJS.ProcessEnv): string {
  if (args.dataDir) return path.resolve(args.dataDir)
  if (env.TUSKFIZZY_DATA_DIR) return path.resolve(env.TUSKFIZZY_DATA_DIR)
  const local = env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
  return path.join(local, args.demo ? 'TUSKFiZZY-demo' : 'TUSKFiZZY')
}

export interface CoreCommand {
  program: string
  args: string[]
  cwd: string
}

/** Packaged: the frozen core in `<resources>\core`. In development: the repository's venv, `-m guildboard`, run in the repository. */
export function coreCommand(args: ShellArgs, folder: string, packaged: boolean, resourcesPath: string, repoRoot: string): CoreCommand {
  const channel = ['--shell', '--data-dir', folder]
  if (args.demo) channel.push('--demo')
  if (args.demoNow) channel.push('--demo-now', args.demoNow)
  if (args.weekAndEndgame) channel.push('--week-and-endgame')
  if (packaged) {
    const cwd = path.join(resourcesPath, 'core')
    return { program: path.join(cwd, 'tuskfizzy-core.exe'), args: channel, cwd }
  }
  return {
    program: path.join(repoRoot, '.venv', 'Scripts', 'python.exe'),
    args: ['-m', 'guildboard', ...channel],
    cwd: repoRoot,
  }
}

/**
 * The app's icon: the tray's, and every window's, which the taskbar, Alt+Tab and the title bar show. Packaged: a plain
 * file in the resources (an extraResource), because Electron copies an `.ico` out of an asar into a temporary file to load
 * it, which is a write outside the data folder. In development: the app's folder.
 */
export function appIconPath(packaged: boolean, resourcesPath: string, appPath: string): string {
  return packaged ? path.join(resourcesPath, 'icon.ico') : path.join(appPath, 'assets', 'icon.ico')
}
