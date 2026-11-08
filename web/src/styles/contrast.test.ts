import { expect, test } from 'vitest'
import base from './base.css?raw'
import board from './board.css?raw'
import { contrast, over, tokenValues } from './contrast.ts'
import elements from '../pages/characters/elements.css?raw'
import frame from './frame.css?raw'
import months from './months.css?raw'
import overlay from './overlay.css?raw'
import stats from './stats.css?raw'
import timers from './timers.css?raw'
import css from './tokens.css?raw'
import widgets from './widgets.css?raw'

// Stylesheets are read as text with Vite's ?raw (vite.config.ts lets those through). A test under src cannot import
// node:fs: the app's tsconfig has no Node types, and `npm run build` type-checks the tests.
const t = tokenValues(css)

/** The declarations of the rule of a stylesheet with this selector, between its braces; '' where there is none. */
function ruleOf(sheet: string, selector: string): string {
  const plain = sheet.replace(/\/\*[\s\S]*?\*\//g, '')
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(plain)?.[1] ?? ''
}

// Spec section 15's table.
test('small text on a card and on the header tint reaches 4.5:1', () => {
  expect(contrast(t['--ink'], t['--surface'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--muted'], t['--surface'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--muted'], t['--tint'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--good'], over(t['--done'], 0.18, t['--surface']))).toBeGreaterThanOrEqual(4.5)
})

test("small text on the page's own background is in --ink, which reaches 4.5:1 where the page is darkest", () => {
  // --page-dark is the page everywhere but near its top right. --ink2 and --muted are tuned for the cards and fall short.
  expect(contrast(t['--ink'], t['--page-dark'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--ink2'], t['--page-dark'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--muted'], t['--page-dark'])).toBeGreaterThanOrEqual(4.5)
  const onThePage: [string, string][] = [
    [frame, '.gb-status'], // the top bar's status line
    [board, '.board-help'], // Edit's line of help
    [board, '.board-narrow'], // Edit's notice of a narrower window
    [timers, '.tim-group-head'], // the Timers category's groups
    [stats, '.st-note'], // the note under the Stats tiles
    [months, '.mo-yr'], // the years of the Months strip
    [months, '.mo-chip.thin'], // a month with little data, which has no card behind it
    [months, '.mo-since .gb-note'], // the notes under the month's parts
  ]
  for (const [sheet, selector] of onThePage) expect(ruleOf(sheet, selector), selector).toMatch(/color:\s*var\(--ink\)/)
})

test("Playtime's columns are the progress gold, and today's a mark of its own that reaches 3:1", () => {
  expect(ruleOf(widgets, '.pw-bar')).toMatch(/background:\s*var\(--w-fill\)/)
  // Today's: the gold mixed toward the ink, as color-mix(in srgb, var(--w-fill) 55%, var(--w-ink)) mixes them.
  expect(ruleOf(widgets, '.pw-col.today .pw-bar')).toMatch(/background:\s*color-mix\(in srgb, var\(--w-fill\) 55%, var\(--w-ink\)\)/)
  expect(contrast(t['--w-fill'], t['--w-bg'])).toBeGreaterThanOrEqual(3)
  expect(contrast(over(t['--w-fill'], 0.55, t['--w-ink']), t['--w-bg'])).toBeGreaterThanOrEqual(3)
  // On the overlay's dark panel, over a white and a black game.
  for (const game of ['#FFFFFF', '#000000']) {
    expect(contrast(over('#E2C98F', 0.55, '#F1EADB'), over('#2B3142', 0.93, game))).toBeGreaterThanOrEqual(3)
  }
})

test('marks and the outlines of controls reach 3:1', () => {
  expect(contrast(t['--fill'], t['--surface'])).toBeGreaterThanOrEqual(3)
  expect(contrast(t['--outline'], t['--surface'])).toBeGreaterThanOrEqual(3)
  expect(contrast(t['--outline'], t['--tint'])).toBeGreaterThanOrEqual(3)
  expect(contrast(t['--gold-ink'], t['--page-dark'])).toBeGreaterThanOrEqual(3)
  expect(contrast(t['--button-text'], t['--button'])).toBeGreaterThanOrEqual(4.5)
})

test("the overlay's text holds over a white and a black game", () => {
  for (const game of ['#FFFFFF', '#000000']) {
    const panel = over('#2B3142', 0.93, game)
    expect(contrast('#F1EADB', panel)).toBeGreaterThanOrEqual(4.5)
    expect(contrast('#A9B0C0', panel)).toBeGreaterThanOrEqual(4.5)
    expect(contrast('#E2C98F', panel)).toBeGreaterThanOrEqual(3)
  }
})

test('the rest of the table holds: buttons, the focus ring and the overlay marks', () => {
  expect(contrast(t['--button-text'], t['--button'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--focus'], t['--surface'])).toBeGreaterThanOrEqual(3)
  expect(contrast(t['--focus'], t['--page-dark'])).toBeGreaterThanOrEqual(3)
  expect(contrast('#F1EADB', '#2B3142')).toBeGreaterThanOrEqual(3) // the overlay's focus ring
  expect(contrast('#2B3142', '#E2C98F')).toBeGreaterThanOrEqual(4.5) // the overlay's solid button
  for (const game of ['#FFFFFF', '#000000']) {
    expect(contrast('#9DC48C', over('#2B3142', 0.93, game))).toBeGreaterThanOrEqual(3)
  }
})

test('contrast gives the ratios of the spec, whichever colour comes first', () => {
  expect(contrast('#000000', '#FFFFFF')).toBeCloseTo(21, 5)
  expect(contrast('#FFFFFF', '#000000')).toBeCloseTo(21, 5)
  expect(contrast('#3B4255', '#3B4255')).toBe(1)
  expect(contrast('#3B4255', '#F7F2E7')).toBeCloseTo(8.97, 2)
  expect(contrast('#6A6659', '#F7F2E7')).toBeCloseTo(5.14, 2)
  expect(contrast('#8A806B', '#F1EAD9')).toBeCloseTo(3.25, 2)
  expect(contrast('#E6DDCA', '#8E661B')).toBeCloseTo(3.83, 2)
  expect(contrast('#2E6B1F', '#DCE1CC')).toBeCloseTo(4.84, 2)
})

test('a colour that is not #RRGGBB is refused by name', () => {
  expect(() => contrast('#FFF', '#000000')).toThrow('Not a #RRGGBB colour: #FFF')
  expect(() => contrast('#000000', undefined as unknown as string)).toThrow('Not a #RRGGBB colour: undefined')
})

test('over mixes a colour at an opacity into the one below it', () => {
  expect(over('#69E39A', 0.18, '#F7F2E7')).toBe('#DCE1CC') // the ready seal's tint
  expect(over('#2B3142', 0.93, '#FFFFFF')).toBe('#3A3F4F')
  expect(over('#2B3142', 0.93, '#000000')).toBe('#282E3D')
  expect(over('#2B3142', 1, '#FFFFFF')).toBe('#2B3142')
  expect(over('#2b3142', 0, '#ffffff')).toBe('#FFFFFF')
})

test('tokenValues reads the colours of :root and nothing else', () => {
  const sheet = `
    /* --ghost: #123456; */
    :root { --a: #AABBCC; --gradient: radial-gradient(#111111 0%, #222222 70%) fixed #333333; --b: #00ff00 }
    :root { --c: #010203; --alpha: rgba(1, 2, 3, .5); }
    .dark { --a: #000000; --d: #FFFFFF; }
  `
  expect(tokenValues(sheet)).toEqual({ '--a': '#AABBCC', '--b': '#00ff00', '--c': '#010203' })
})

test('tokenValues finds the real palette', () => {
  expect(t['--surface']).toBe('#1B1826')
  expect(t['--page-dark']).toBe('#100E18')
  expect(t['--page']).toBeUndefined() // a gradient, not a colour
})

// Spec 6.1: the three changed colours and the two new ones.
test('the changed and new colours have the values of spec 6.1', () => {
  expect(t['--muted']).toBe('#AAA2BD')
  expect(t['--fill']).toBe('#B58CFF')
  expect(t['--r1']).toBe('#8D61E8') // the playtime calendar's first level keeps the lighter gold
  expect(t['--outline']).toBe('#8D82A5')
  expect(t['--gold-ink']).toBe('#D1B8FF')
  expect(t['--good']).toBe('#8FE7A5')
  expect(t['--done']).toBe('#69E39A') // stays for marks
  expect(t['--focus']).toBe('#C7A8FF')
})

// The Pulls history's monthly graph (the second pass): the plan's palette in its order. The dataviz validator passes every
// check for it on the card's surface, which this test cannot run (a script outside the interface); it holds the colours
// to the plan's values and each mark to the 3:1 of a mark on the card.
test("the monthly graph's wishes have the plan's colours, in its order, and each reaches 3:1 on the card", () => {
  const wishes = ['--wish-character', '--wish-weapon', '--wish-standard', '--wish-chronicled']
  expect(wishes.map((name) => t[name])).toEqual(['#B58CFF', '#83A9F0', '#E58BA2', '#8D75D6'])
  for (const name of wishes) expect(contrast(t[name], t['--surface']), name).toBeGreaterThanOrEqual(3)
})

/** The declarations of the first rule written as `selector {` in a stylesheet, tokens.css unless said, as written. */
function declarations(selector: string, sheet = css): Record<string, string> {
  const plain = sheet.replace(/\/\*[\s\S]*?\*\//g, '')
  const start = plain.indexOf(`${selector} {`)
  if (start < 0) throw new Error(`No rule for ${selector}`)
  const body = plain.slice(start, plain.indexOf('}', start))
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]))
}

// Spec 6.2: the role, its parchment value, its dark value and the token the parchment value stands for.
const roles: [role: string, parchment: string, dark: string, stands: string | null][] = [
  ['--w-bg', '#1B1826', '#1B1826', '--surface'],
  ['--w-gap', '#1B1826', '#1B1826', '--surface'],
  ['--w-plate', '#211D30', '#211D30', '--tint'],
  ['--w-border', '#39334C', '#39334C', '--border'],
  ['--w-line', '#302A40', '#302A40', '--hairline'],
  ['--w-ink', '#F5F1FF', '#F5F1FF', '--ink'],
  ['--w-ink2', '#D4CBE5', '#D4CBE5', '--ink2'],
  ['--w-muted', '#AAA2BD', '#AAA2BD', '--muted'],
  ['--w-fill', '#B58CFF', '#B58CFF', '--fill'],
  ['--w-track', '#39334B', '#39334B', '--track'],
  ['--w-icon', '#C4A6FF', '#C4A6FF', null],
  ['--w-done', '#69E39A', '#69E39A', '--done'],
  ['--w-good', '#8FE7A5', '#8FE7A5', '--good'],
  ['--w-outline', '#8D82A5', '#8D82A5', '--outline'],
  ['--w-btn', '#7752C8', '#7752C8', '--button'],
  ['--w-btn-text', '#FFFFFF', '#FFFFFF', '--button-text'],
  ['--w-quiet', '#39334C', '#39334C', '--border'],
  ['--w-focus', '#C7A8FF', '#C7A8FF', '--focus'],
  // The Pulls plan, Task 11: the mark of soft pity on a pity ring, dark as --gold-ink is on parchment and light as the
  // overlay's --gold-ink is over the game.
  ['--w-gold-ink', '#D1B8FF', '#D1B8FF', '--gold-ink'],
  // The Pulls plan, Task 12: the purple frame of a 4★'s face, as the gold one (--w-fill) is a 5★'s. The pages have no
  // purple of their own to stand for.
  ['--w-four', '#8A6BBE', '#B9A2E0', null],
  // The Pulls review's M6: the washes of a 5★'s and a 4★'s row in the history, in the gold and the purple; and the
  // shade under words laid over a picture, as a banner's name over its art, light on parchment and dark on the panel.
  ['--w-five-wash', 'rgba(181, 140, 255, .24)', 'rgba(226, 201, 143, .2)', null],
  ['--w-four-wash', 'rgba(141, 97, 232, .20)', 'rgba(185, 162, 224, .18)', null],
  ['--w-shade', 'rgba(16, 14, 24, .88)', 'rgba(18, 22, 40, .85)', null],
]

test("the soft pity's mark reaches 3:1 on the ring's track, on parchment and over a white and a black game", () => {
  expect(contrast(t['--w-gold-ink'], t['--w-track'])).toBeGreaterThanOrEqual(3)
  for (const game of ['#FFFFFF', '#000000']) {
    const panel = over('#2B3142', 0.93, game)
    expect(contrast('#E2C98F', over('#F1EADB', 0.17, panel))).toBeGreaterThanOrEqual(3)
  }
})

test("a 4★'s purple frame reaches 3:1 on the card, on parchment and over a white and a black game", () => {
  expect(contrast(t['--w-four'], t['--w-bg'])).toBeGreaterThanOrEqual(3)
  for (const game of ['#FFFFFF', '#000000']) {
    expect(contrast('#B9A2E0', over('#2B3142', 0.93, game))).toBeGreaterThanOrEqual(3)
  }
})

test.each(roles)('%s holds the spec values on parchment and in the dark panel', (role, parchment, dark) => {
  expect(declarations(':root')[role]).toBe(parchment)
  expect(declarations('.on-dark')[role]).toBe(dark)
})

test('on parchment a widget role is the colour of the token it stands for, so there is one gold and one muted', () => {
  const root = declarations(':root')
  for (const [role, , , stands] of roles) {
    if (stands !== null) expect([role, root[role]]).toEqual([role, t[stands]])
  }
})

// Look pass spec 20.3, item 16: a ring's gaps and a bar's notches are drawn in the background's colour, which the dark
// panel makes see-through; theirs is the same colour, opaque, so that the mark under them never shows through.
test("the gap colour is the background's, opaque on parchment and in the dark panel", () => {
  const root = declarations(':root')
  const dark = declarations('.on-dark')
  for (const gap of [root['--w-gap'], dark['--w-gap']]) expect(gap).toMatch(/^#[0-9A-F]{6}$/)
  expect(root['--w-gap']).toBe(root['--w-bg'])
  expect(dark['--w-gap']).toBe(dark['--w-bg'])
})

// The overlay's page (plan 2A-4): its bar, Edit and the sheets draw with the page's own tokens, which .ov sets dark
// again over the game, as .on-dark sets the widgets' roles. Text reaches 4.5:1 on the panel's colour, and marks 3:1.
test("the overlay sets the page's tokens dark, its text reaching 4.5:1 on the panel's #2B3142", () => {
  const ov = declarations('.ov', overlay)
  const panel = '#2B3142'
  const tokens = ['--surface', '--tint', '--ink', '--ink2', '--muted', '--border', '--hairline', '--gold-ink']
  for (const token of [...tokens, '--button', '--button-text', '--grid']) expect(ov[token], token).toBeDefined()
  expect(ov['--surface']).toBe(panel) // the bar and the sheets
  for (const text of ['--ink', '--ink2', '--muted']) {
    expect(contrast(ov[text], panel), text).toBeGreaterThanOrEqual(4.5)
    expect(contrast(ov[text], ov['--tint']), `${text} on the tint`).toBeGreaterThanOrEqual(4.5)
  }
  expect(contrast(ov['--button-text'], ov['--button'])).toBeGreaterThanOrEqual(4.5) // a solid button
  expect(contrast(ov['--gold-ink'], panel)).toBeGreaterThanOrEqual(3) // Edit's landing outline and grips
  // The size tag's plate is --gold-ink, light over the game: its text, white in board.css, is the panel's colour.
  expect(ruleOf(overlay, '.ov .size-tag')).toMatch(/color:\s*var\(--surface\)/)
  expect(contrast(ov['--surface'], ov['--gold-ink'])).toBeGreaterThanOrEqual(4.5)
})

test('the dark panel sets every widget role again, and the focus ring', () => {
  const widgetRoles = Object.keys(declarations(':root')).filter((name) => name.startsWith('--w-'))
  expect(widgetRoles.sort()).toEqual(roles.map(([role]) => role).sort())
  expect(Object.keys(declarations('.on-dark')).sort()).toEqual([...widgetRoles, '--focus'].sort())
  expect(declarations('.on-dark')['--focus']).toBe('#C7A8FF')
})

// Spec 6.3.
test.each([
  ['--t-press', '90ms'],
  ['--t-hover', '140ms'],
  ['--t-state', '200ms'],
  ['--t-open', '260ms'],
  ['--t-move', '280ms'],
  ['--t-enter', '700ms'],
  ['--t-count', '800ms'],
  ['--t-glint', '900ms'],
  ['--stagger', '30ms'],
  ['--ease-out', 'cubic-bezier(.22, .8, .24, 1)'],
  ['--ease-move', 'cubic-bezier(.45, 0, .2, 1)'],
  ['--ease-stamp', 'linear(0, 1.14 42%, .97 70%, 1)'],
])('the motion token %s is %s', (name, value) => {
  expect(declarations(':root')[name]).toBe(value)
})

test('the focus ring is drawn with the focus token, which the dark panel sets again', () => {
  expect(base).toMatch(/:focus-visible\s*\{[^}]*outline:\s*2px solid var\(--focus\)/)
})

test("the Ledger's earned and spent amounts reach 4.5:1 on the card", () => {
  expect(contrast(t['--good'], t['--surface'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--spent'], t['--surface'])).toBeGreaterThanOrEqual(4.5)
})

// The Characters plan, Task U1: the seven element palettes of profile.html, which the profile and the planner draw on a
// dark card, and the colours the roster draws on parchment. elements.css has them as `.el-cryo` and so on.
const ELEMENT_NAMES = ['pyro', 'hydro', 'anemo', 'electro', 'dendro', 'cryo', 'geo']
const dark = tokenValues(elements)

/** The custom properties of one element's class in elements.css, as written. */
function palette(name: string): Record<string, string> {
  const plain = elements.replace(/\/\*[\s\S]*?\*\//g, '')
  const body = new RegExp(`\\.el-${name}\\s*\\{([^}]*)\\}`).exec(plain)?.[1] ?? ''
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);?/g)].map((found) => [found[1], found[2].trim()]))
}

test("the dark card has the profile mockup's own colours for its text, in elements.css's :root", () => {
  expect(dark['--c-star']).toBe('#F1EADB')
  expect(dark['--c-star2']).toBe('#C9CFDD')
  expect(dark['--c-dim']).toBe('#8D94A8')
  expect(dark['--c-gold']).toBe('#E2C98F')
  expect(dark['--c-good']).toBe('#A9D49A')
  expect(dark['--c-bad']).toBe('#F2A08F')
})

test.each(ELEMENT_NAMES)('%s: every colour pair of the dark card reaches its ratio on its own ground', (name) => {
  const p = palette(name)
  for (const key of ['--card-bg', '--el', '--el-ink']) expect(p[key], `${name} ${key}`).toMatch(/^#[0-9A-Fa-f]{6}$/)
  const ground = p['--card-bg']
  // The glass panel is a faint veil of the star colour over the card's ground.
  const panel = over(dark['--c-star'], 0.06, ground)
  for (const text of ['--c-star', '--c-star2', '--c-dim', '--c-gold', '--c-good', '--c-bad']) {
    expect(contrast(dark[text], ground), `${name} ${text} on the card`).toBeGreaterThanOrEqual(4.5)
    expect(contrast(dark[text], panel), `${name} ${text} on the panel`).toBeGreaterThanOrEqual(4.5)
  }
  // Her colour as text (her title, a goal reached, a chosen option) and as a mark (a bar, a lit star, a roll that helps).
  expect(contrast(p['--el'], ground), `${name} el on the card`).toBeGreaterThanOrEqual(4.5)
  expect(contrast(p['--el'], panel), `${name} el on the panel`).toBeGreaterThanOrEqual(4.5)
  // Text on her colour: a chosen option and the planner's chosen chip, in the card's ground.
  expect(contrast(ground, p['--el']), `${name} ground on el`).toBeGreaterThanOrEqual(4.5)
  // The three verdicts' chips, small text on their own tint over the strip under the card.
  const strip = over(dark['--c-star'], 0.05, ground)
  const chips: [string, string, number][] = [
    [dark['--c-good'], '#9DC48C', 0.18],
    [dark['--c-bad'], '#E6826E', 0.2],
    [dark['--c-gold'], '#E2C98F', 0.18],
  ]
  for (const [text, tint, alpha] of chips) {
    expect(contrast(text, over(tint, alpha, strip)), `${name} chip ${text}`).toBeGreaterThanOrEqual(4.5)
  }
})

test.each(ELEMENT_NAMES)('%s: her colour on parchment reaches 3:1 on a card and on a sheet', (name) => {
  const ink = palette(name)['--el-ink']
  expect(contrast(ink, t['--surface']), name).toBeGreaterThanOrEqual(3)
  expect(contrast(ink, t['--ledger-light']), name).toBeGreaterThanOrEqual(3)
})

test('no two elements share a dark ground and a colour, though colour alone never tells two characters apart', () => {
  const pairs = ELEMENT_NAMES.map((name) => `${palette(name)['--card-bg']} ${palette(name)['--el']}`)
  expect(new Set(pairs).size).toBe(ELEMENT_NAMES.length)
})
