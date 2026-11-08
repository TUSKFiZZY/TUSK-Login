import { win32 } from 'node:path'
import { expect, test } from 'vitest'
import { parseArgs } from '../src/args.js'
import { appIconPath, coreCommand, dataFolder } from '../src/paths.js'

test('the data folder follows the core’s rule', () => {
  const env = { LOCALAPPDATA: 'C:\\L' }
  expect(dataFolder(parseArgs([]), env)).toBe('C:\\L\\TUSKFiZZY')
  expect(dataFolder(parseArgs(['--demo']), env)).toBe('C:\\L\\TUSKFiZZY-demo')
  expect(dataFolder(parseArgs([]), { ...env, TUSKFIZZY_DATA_DIR: 'D:\\g' })).toBe('D:\\g')
  expect(dataFolder(parseArgs(['--data-dir', 'E:\\h']), env)).toBe('E:\\h')
})

test('a relative data folder is made absolute, as one folder for the core and for Electron', () => {
  const env = { LOCALAPPDATA: 'C:\\L' }
  const absolute = (p: string): boolean => /^[A-Za-z]:\\/.test(p)
  expect(dataFolder(parseArgs(['--data-dir', 'rel\\x']), env)).toBe(win32.resolve('rel\\x'))
  expect(absolute(dataFolder(parseArgs(['--data-dir', 'rel\\x']), env))).toBe(true)
  expect(dataFolder(parseArgs([]), { ...env, TUSKFIZZY_DATA_DIR: 'rel\\y' })).toBe(win32.resolve('rel\\y'))
  expect(dataFolder(parseArgs(['--data-dir', 'E:\\h\\..\\i']), env)).toBe('E:\\i')
})

test('the core’s command, packaged and in development', () => {
  expect(coreCommand(parseArgs(['--demo']), 'D', true, 'R', 'X'))
    .toEqual({ program: 'R\\core\\tuskfizzy-core.exe', args: ['--shell', '--data-dir', 'D', '--demo'], cwd: 'R\\core' })
  expect(coreCommand(parseArgs([]), 'D', false, 'R', 'X').args.slice(0, 3)).toEqual(['-m', 'guildboard', '--shell'])
})

test('the development command runs the venv’s python in the repository', () => {
  expect(coreCommand(parseArgs([]), 'D', false, 'R', 'X')).toEqual({
    program: 'X\\.venv\\Scripts\\python.exe',
    args: ['-m', 'guildboard', '--shell', '--data-dir', 'D'],
    cwd: 'X',
  })
})

test('the demo’s switches reach the core', () => {
  const args = parseArgs(['--demo', '--demo-now', '2026-10-13T08:00:00+00:00', '--week-and-endgame'])
  expect(coreCommand(args, 'D', true, 'R', 'X').args).toEqual(
    ['--shell', '--data-dir', 'D', '--demo', '--demo-now', '2026-10-13T08:00:00+00:00', '--week-and-endgame'])
})

test('the app’s icon, the tray’s and every window’s, is a file beside the asar when packaged, so that Electron copies nothing out of the asar', () => {
  expect(appIconPath(true, 'R', 'R\\app.asar')).toBe('R\\icon.ico')
  expect(appIconPath(false, 'R', 'X\\shell')).toBe('X\\shell\\assets\\icon.ico')
})
