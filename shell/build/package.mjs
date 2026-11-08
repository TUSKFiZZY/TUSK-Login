// Packs the shell and the core into shell/out/TUSKFiZZY-win32-x64, the folder the installer carries.
//
//     node build/package.mjs --core <the core's folder> --version <version>
//
// Run from shell/ by build-installer.ps1 after `npm run build` (dist/ must exist) and after PyInstaller has made the core's
// folder. The core's folder is copied into the resources, so it lands at resources\<its name>\guildboard-core.exe; the
// folder is named `core` (packaging/guildboard.spec), which is the path shell/src/paths.ts expects.
//
// The app is packed into resources\app.asar with package.json and dist/ alone. The app's icon, the tray's and the
// windows', is a plain file, resources\icon.ico (an extraResource): Electron copies an .ico out of an asar into a temporary
// file to load it, which is a write outside the data folder. Then the program's fuses are set; they are a guard, not a
// formality: with RunAsNode, NODE_OPTIONS and --inspect off, an installed Guild Board cannot be started as a plain Node,
// or with a debugging door, by another program.
import { basename, dirname, join, resolve } from 'node:path'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'
import { packager } from '@electron/packager'
import { FuseV1Options, FuseVersion, FuseState, flipFuses, getCurrentFuseWire } from '@electron/fuses'

const shellRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const { values } = parseArgs({ options: { core: { type: 'string' }, version: { type: 'string' } } })
if (!values.core || !values.version) {
  console.error('Usage: node build/package.mjs --core <the core folder> --version <version>')
  process.exit(2)
}
const core = resolve(values.core)
// The folder is copied by its own name into resources, and shell/src/paths.ts looks for resources\core\guildboard-core.exe.
if (basename(core) !== 'core') {
  console.error(`The core's folder must be named core, and it is named ${basename(core)}.`)
  process.exit(2)
}
if (!existsSync(join(core, 'guildboard-core.exe'))) {
  console.error(`${core} holds no guildboard-core.exe.`)
  process.exit(2)
}
if (!existsSync(join(shellRoot, 'dist', 'main.js'))) {
  console.error('shell/dist is not built. Run npm run build in shell first.')
  process.exit(2)
}

/** What goes into app.asar: only what is named here, so a new folder in shell/ never ships by accident. `name` is relative to shell/ and starts with a slash. */
function keeps(name) {
  if (name === '' || name === '/package.json' || name === '/dist') return true
  return name.startsWith('/dist/') && !name.endsWith('.map')
}

const [app] = await packager({
  dir: shellRoot,
  name: 'TUSKFiZZY',
  executableName: 'guildboard-app',
  platform: 'win32',
  arch: 'x64',
  asar: true,
  prune: true,
  icon: join(shellRoot, 'assets', 'icon.ico'),
  extraResource: [core, join(shellRoot, 'assets', 'icon.ico')],
  appVersion: values.version,
  win32metadata: { ProductName: 'TUSKFiZZY', FileDescription: 'TUSKFiZZY Genshin companion' },
  ignore: (name) => !keeps(name),
  out: join(shellRoot, 'out'),
  overwrite: true,
})

const exe = join(app, 'tuskfizzy-app.exe')
const wanted = {
  [FuseV1Options.RunAsNode]: false,
  [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
  [FuseV1Options.EnableNodeCliInspectArguments]: false,
  [FuseV1Options.EnableEmbeddedAsarIntegrityValidation]: true,
  [FuseV1Options.OnlyLoadAppFromAsar]: true,
  [FuseV1Options.EnableCookieEncryption]: true,
}
await flipFuses(exe, { version: FuseVersion.V1, ...wanted })

// Read them back: a fuse that did not stick stops the build.
const found = await getCurrentFuseWire(exe)
for (const [option, on] of Object.entries(wanted)) {
  const state = found[option]
  if (state !== (on ? FuseState.ENABLE : FuseState.DISABLE)) {
    console.error(`The fuse ${FuseV1Options[option]} reads back as ${FuseState[state]}, not ${on ? 'ENABLE' : 'DISABLE'}.`)
    process.exit(1)
  }
}
if (
  !existsSync(join(app, 'resources', 'app.asar')) ||
  !existsSync(join(app, 'resources', 'icon.ico')) ||
  !existsSync(join(app, 'resources', basename(core), 'guildboard-core.exe'))
) {
  console.error('The packed folder lacks resources\\app.asar, resources\\icon.ico or the core.')
  process.exit(1)
}
console.log(`Packed ${app}`)
