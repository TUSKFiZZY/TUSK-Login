import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, test } from 'vitest'
import type { Live } from '../live.ts'
// The stylesheet is read as text with Vite's ?raw, as in ui/motion.test.ts.
import frameCss from '../styles/frame.css?raw'
import { Sign, type SignName } from '../ui/Sign.tsx'
import { Frame } from './Frame.tsx'

/** frame.css without its comments. */
const css = frameCss.replace(/\/\*[\s\S]*?\*\//g, '')

/** A core that has not answered yet: the frame then draws the rail and the top bar alone. */
const live: Live = {
  status: null,
  settings: null,
  problem: null,
  version: 0,
  onlineVersion: 0,
  lines: [],
  pullsImport: null,
  now: () => new Date(0),
  refresh: async () => undefined,
  saveSettings: async () => undefined,
}

/** The rail as the page draws it. */
function rail(): string {
  const markup = renderToStaticMarkup(createElement(Frame, { page: 'board', live, children: null }))
  return /<nav [^>]*>[\s\S]*<\/nav>/.exec(markup)?.[0] ?? ''
}

const sign = (name: SignName) => renderToStaticMarkup(createElement(Sign, { name }))

/** The text of the rule of frame.css with this selector, between its braces; '' where there is none. */
function ruleOf(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? ''
}

test('each page of the rail has its sign in front of its name', () => {
  const pages: [string, SignName][] = [
    ['Board', 'cells'],
    ['Daily Tracker', 'cells'],
    ['Achievements', 'cup'],
    ['Characters', 'bust'],
    ['Backlog', 'map'],
    ['Stats', 'bars'],
    ['Pulls', 'star'],
    ['Ledger', 'scroll'],
    ['Friends', 'people'],
    ['Settings', 'gear'],
  ]
  for (const [name, drawing] of pages) {
    expect(rail(), name).toContain(`${sign(drawing)}${name}</`)
  }
})

test('Your week is not in the rail until the core says it is shown', () => {
  expect(rail()).not.toContain('Your week')
})

test('the signs are drawn for the eye alone, so the links keep their names', () => {
  const links = [...rail().matchAll(/<a [^>]*>([\s\S]*?)<\/a>/g)].map((link) => link[1].replace(/<svg[\s\S]*?<\/svg>/g, ''))
  expect(links).toEqual(['Board', 'Daily Tracker', 'Achievements', 'Characters', 'Backlog', 'Map', 'Stats', 'Pulls', 'Ledger', 'Friends', 'Settings'])
})

test('every page of the rail is a link to its address, Pulls among them (the Pulls plan, calls item 18)', () => {
  const addresses = [...rail().matchAll(/<a [^>]*href="([^"]*)"/g)].map((link) => link[1])
  expect(addresses).toEqual(['#/board', '#/tuskfizzy', '#/achievements', '#/characters', '#/backlog', '#/map', '#/stats', '#/pulls', '#/ledger', '#/friends', '#/settings'])
  expect(rail()).not.toContain('aria-disabled') // no page is shown as one for later
})

test('the rail draws its sign in its muted colour, and in gold on the current page', () => {
  expect(ruleOf('.gb-nav .sign')).toMatch(/flex:\s*none/)
  expect(ruleOf('.gb-nav.on .sign')).toMatch(/color:\s*var\(--gold\)/)
  expect(ruleOf('.gb-nav')).toMatch(/color:\s*var\(--rail-muted\)/) // the sign draws in currentColor
})

test('the content of a page fades in over 160 ms, and the rail, the top bar, the banners and the note do not', () => {
  const rule = /\.gb-main > :not\(([^)]*)\)\s*\{([^}]*)\}/.exec(css)
  expect(rule).not.toBeNull()
  const [, kept, declarations] = rule!
  expect(kept.split(',').map((one) => one.trim()).sort()).toEqual(['.gb-banner', '.gb-hint', '.gb-top', '.note-host'])
  expect(declarations).toMatch(/animation:\s*fade-in 160ms var\(--ease-out\)/)
  // The rail and the top bar have no motion of their own.
  for (const part of ['.gb-rail', '.gb-brand', '.gb-nav', '.gb-top', '.gb-title', '.gb-plate-wrap', '.gb-plate', '.gb-banner', '.gb-hint']) {
    expect(ruleOf(part), part).not.toBe('') // a rule that is renamed must not leave this check empty
    expect(ruleOf(part), part).not.toMatch(/animation/)
  }
})

test('a button darkens on hover and shrinks to 97% on a press, unless it cannot be used', () => {
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({ selector: selector.trim(), body }))
  const hover = rules.filter((one) => one.selector.includes(':hover') && /filter:\s*brightness\(\.\d+\)/.test(one.body))
  const press = rules.filter((one) => one.selector.includes(':active') && /transform:\s*scale\(\.97\)/.test(one.body))
  expect(hover).toHaveLength(1)
  expect(press).toHaveLength(1)
  for (const { selector } of [...hover, ...press]) {
    expect(selector).toContain(":not(:disabled, [aria-disabled='true'])")
    // Edit's Settings and Remove in a widget's heading (board.css) are among them.
    for (const button of ['.gb-btn', '.ach-toggle', '.ach-step', '.w-tool']) expect(selector).toContain(button)
  }
  expect(css).not.toMatch(/\.board-tool\b/) // the old Edit bar's buttons are gone
  // The colour changes over --t-hover and the press over --t-press.
  const transition = /transition:([^;]*);/.exec(rules.find((one) => one.body.includes('transition:'))?.body ?? '')?.[1] ?? ''
  expect(transition).toContain('var(--t-hover)')
  expect(transition).toMatch(/transform var\(--t-press\)/)
  // A card does not lift or scale on hover.
  expect(css).not.toMatch(/\.gb-panel[^{]*:hover/)
})

test('a section heading takes a gold hairline from a small diamond', () => {
  expect(ruleOf('.gb-section-h')).toMatch(/display:\s*flex/)
  expect(ruleOf('.gb-section-h::before')).toMatch(/width:\s*7px/)
  expect(ruleOf('.gb-section-h::before')).toMatch(/transform:\s*rotate\(45deg\)/)
  expect(ruleOf('.gb-section-h::before')).toMatch(/background:\s*var\(--w-icon\)/) // the signs' gold, no colour of its own
  expect(ruleOf('.gb-section-h::after')).toMatch(/height:\s*1px/)
  expect(ruleOf('.gb-section-h::after')).toMatch(/linear-gradient\(to right, var\(--gold\)/)
})

test('the plate keeps every figure that moves in digits of one width, and the sync part one width in every state', () => {
  expect(ruleOf('.gb-plate')).toMatch(/font-variant-numeric:\s*tabular-nums/)
  expect(ruleOf('.gb-plate')).toMatch(/--sync-w:\s*\d+px/)
  // The everyday states fit the least width and never grow it; the amber part takes at least as much room, ten pixels
  // of it in its margins, and grows to its text and its action.
  expect(ruleOf('.gb-plate-sync')).toMatch(/min-width:\s*var\(--sync-w/)
  expect(ruleOf('.gb-plate-sync')).not.toMatch(/(^|[\s;])width:/)
  expect(ruleOf('.gb-plate-sync.gb-need')).toMatch(/min-width:\s*calc\(var\(--sync-w[^)]*\)\s*-\s*10px\)/)
  expect(ruleOf('.gb-plate-sync.gb-need')).toMatch(/margin:\s*5px/)
  // The text keeps to the button, and the spare room is on its far side.
  expect(ruleOf('.gb-plate-sync')).toMatch(/justify-content:\s*flex-end/)
  expect(ruleOf('.gb-sync-text')).toMatch(/text-align:\s*right/)
})

test('the plate takes its colours from the tokens, and the amber look from the new ones', () => {
  expect(ruleOf('.gb-plate-sync.gb-need')).toMatch(/background:\s*var\(--warn-soft\)/)
  expect(ruleOf('.gb-plate-sync.gb-need')).toMatch(/var\(--warn-line\)/)
  expect(ruleOf('.gb-resin-ico')).toMatch(/color:\s*var\(--resin\)/)
  const own = frameCss.slice(frameCss.indexOf('The profile card in the rail')).replace(/\/\*[\s\S]*?\*\//g, '')
  expect(own).not.toMatch(/#[0-9a-f]{3,8}\b/i)
})

test("the card's, the plate's and the panels' classes carry the gb- prefix and no other stylesheet has them", () => {
  const sheets = import.meta.glob<string>('../styles/*.css', { query: '?raw', import: 'default', eager: true })
  const own = frameCss.slice(frameCss.indexOf('The profile card in the rail')).replace(/\/\*[\s\S]*?\*\//g, '')
  // The classes the section's selectors name; `.sign`, `.pic`, `.sbtn`-like names of the shared parts stay out of it.
  const shared = new Set(['sign', 'pic', 'busy', 'grey', 'split', 'spin', 'quiet'])
  const names = new Set([...own.replace(/\{[^}]*\}/g, '{}').matchAll(/\.([a-z][\w-]*)/g)].map((one) => one[1]))
  const mine = [...names].filter((name) => !shared.has(name) && !name.startsWith('gb-btn'))
  expect(mine.length).toBeGreaterThan(20)
  expect(mine.filter((name) => !name.startsWith('gb-'))).toEqual([])
  for (const [file, sheet] of Object.entries(sheets)) {
    if (file.endsWith('/frame.css')) continue
    const plain = sheet.replace(/\/\*[\s\S]*?\*\//g, '')
    for (const name of mine) expect(plain, `${name} in ${file}`).not.toMatch(new RegExp(String.raw`\.${name}(?![\w-])`))
  }
})

// The online plan, Task B7: the pages of Friends have titles of their own ("Your profile", "You and Riley") under the rail's
// one item, which stays the current page.
test('a page can give itself a title, and the rail still marks Friends as the page it is on', () => {
  const named = renderToStaticMarkup(createElement(Frame, { page: 'friends', live, title: 'You and Riley', children: null }))
  expect(named).toContain('>You and Riley</h1>')
  expect(named).toMatch(/class="gb-nav on"[^>]*aria-current="page"[^>]*>[\s\S]*?Friends<\/a>/)
  const plain = renderToStaticMarkup(createElement(Frame, { page: 'friends', live, children: null }))
  expect(plain).toContain('>Friends</h1>')
})
