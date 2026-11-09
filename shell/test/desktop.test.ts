import { expect, test } from 'vitest'
import { Desktop, type DesktopDeps } from '../src/desktop.js'

function setup(options: { packaged?: boolean; chosen?: string | null; opened?: string; spawnFails?: boolean } = {}) {
  const packaged = options.packaged !== false
  const saves: unknown[] = []
  const boxes: unknown[] = []
  const opened: string[] = []
  const spawned: { program: string; args: string[] }[] = []
  const relaunched: unknown[] = []
  const warnings: string[] = []
  const deps: DesktopDeps = {
    documents: 'C:\\Users\\Mia\\Documents',
    pictures: 'C:\\Users\\Mia\\Pictures',
    execPath: packaged ? 'C:\\TUSKFiZZY\\guildboard-app.exe' : 'Z:\\repo\\shell\\node_modules\\electron\\dist\\electron.exe',
    appFolder: packaged ? null : 'Z:\\repo\\shell',
    async showSaveDialog(dialogOptions) {
      saves.push(dialogOptions)
      const chosen = options.chosen === undefined ? 'C:\\Users\\Mia\\Documents\\chosen.gbbackup' : options.chosen
      return chosen === null ? { canceled: true } : { canceled: false, filePath: chosen }
    },
    async showMessageBox(boxOptions) {
      boxes.push(boxOptions)
    },
    async openPath(path) {
      opened.push(path)
      return options.opened ?? ''
    },
    spawnDetached(program, args, onError) {
      spawned.push({ program, args })
      if (options.spawnFails) onError()
    },
    relaunch(relaunchOptions) {
      relaunched.push(relaunchOptions)
    },
    warn: (text) => warnings.push(text),
  }
  return { desktop: new Desktop(deps), saves, boxes, opened, spawned, relaunched, warnings }
}

test('the backup dialog is Export backup in Documents, for a TUSKFiZZY backup (*.gbbackup)', async () => {
  const { desktop, saves } = setup()
  expect(await desktop.chooseBackupFile('guild-board-2026-11-07.gbbackup')).toBe('C:\\Users\\Mia\\Documents\\chosen.gbbackup')
  expect(saves).toEqual([
    {
      title: 'Export backup',
      defaultPath: 'C:\\Users\\Mia\\Documents\\guild-board-2026-11-07.gbbackup',
      filters: [{ name: 'TUSKFiZZY backup', extensions: ['gbbackup'] }],
    },
  ])
})

test('the picture dialog is Save picture in Pictures, for a PNG picture (*.png)', async () => {
  const { desktop, saves } = setup()
  await desktop.choosePictureFile('wrapped.png')
  expect(saves).toEqual([
    {
      title: 'Save picture',
      defaultPath: 'C:\\Users\\Mia\\Pictures\\wrapped.png',
      filters: [{ name: 'PNG picture', extensions: ['png'] }],
    },
  ])
})

test('a cancelled dialog answers null', async () => {
  const { desktop } = setup({ chosen: null })
  expect(await desktop.chooseBackupFile('a.gbbackup')).toBeNull()
  expect(await desktop.choosePictureFile('a.png')).toBeNull()
})

test('an alert is an error box titled TUSKFiZZY with the text', async () => {
  const { desktop, boxes } = setup()
  await desktop.alert('TUSKFiZZY stopped unexpectedly. The log file says why.')
  expect(boxes).toEqual([
    { type: 'error', title: 'TUSKFiZZY', message: 'TUSKFiZZY stopped unexpectedly. The log file says why.', buttons: ['OK'] },
  ])
})

test('the data folder opens in Explorer', async () => {
  const { desktop, opened } = setup()
  await desktop.openFolder('C:\\Users\\Mia\\AppData\\Local\\TUSKFiZZY')
  expect(opened).toEqual(['C:\\Users\\Mia\\AppData\\Local\\TUSKFiZZY'])
})

test('a folder that will not open is told to the log, not thrown', async () => {
  const { desktop, warnings } = setup({ opened: 'Failed to open path' })
  await desktop.openFolder('C:\\nowhere')
  expect(warnings).toEqual(['The data folder could not be opened.'])
})

test('the demo starts as guildboard-app.exe --demo --acknowledged', () => {
  const { desktop, spawned } = setup()
  desktop.startDemo()
  expect(spawned).toEqual([{ program: 'C:\\TUSKFiZZY\\guildboard-app.exe', args: ['--demo', '--acknowledged'] }])
})

test('in development the demo starts electron.exe with the app’s folder first', () => {
  const { desktop, spawned } = setup({ packaged: false })
  desktop.startDemo()
  expect(spawned).toEqual([
    { program: 'Z:\\repo\\shell\\node_modules\\electron\\dist\\electron.exe', args: ['Z:\\repo\\shell', '--demo', '--acknowledged'] },
  ])
})

test('start again keeps --data-dir only: not --hidden, not --demo', () => {
  const { desktop, relaunched } = setup()
  desktop.startAgain('D:\\TUSKFiZZY data')
  expect(relaunched).toEqual([{ args: ['--data-dir', 'D:\\TUSKFiZZY data'] }])
})

test('start again with no data folder given keeps nothing', () => {
  const { desktop, relaunched } = setup()
  desktop.startAgain(null)
  expect(relaunched).toEqual([{ args: [] }])
})

test('in development start again names the app’s folder first', () => {
  const { desktop, relaunched } = setup({ packaged: false })
  desktop.startAgain('D:\\x')
  expect(relaunched).toEqual([{ args: ['Z:\\repo\\shell', '--data-dir', 'D:\\x'] }])
})

test('a demo that cannot be started is told to the log, and nothing throws', () => {
  const { desktop, warnings } = setup({ spawnFails: true })
  expect(() => desktop.startDemo()).not.toThrow()
  expect(warnings).toEqual(['The demo could not be started.'])
})
