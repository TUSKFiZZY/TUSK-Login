import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, test } from 'vitest'
import fixture from '../../fixtures/characters/week.json'
import type { Week as WeekModel } from '../../types.ts'
import { contrast, tokenValues } from '../../styles/contrast.ts'
import tokens from '../../styles/tokens.css?raw'
import { PickList, WeekView } from './Week.tsx'
import css from './week.css?raw'

const week = fixture as unknown as WeekModel
const none: WeekModel = { ...week, focus: [], characters: week.characters.map((one) => ({ ...one, focus: null })), days: week.days.map((day) => ({ ...day, tasks: [], spare: null })), ready: [], resin_by: [] }
const plain = css.replace(/\/\*[\s\S]*?\*\//g, '')

/** The declarations of a rule of week.css with exactly this selector. */
function ruleOf(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\>]/g, '\\$&')
  return new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(plain)?.[1] ?? ''
}

const draw = (model = week, hot: string | null = null) =>
  renderToStaticMarkup(
    createElement(WeekView, {
      week: model, hot, selected: 0, refusal: null, onHot: () => undefined, onToggle: () => undefined, onMove: () => undefined,
      onTick: () => undefined, onOpenDay: () => undefined, onAgendaScroll: () => undefined,
    }),
  )

test('the top has the four focus places and one Add a character button, not a chip for every character', () => {
  const markup = draw()
  expect(markup).not.toContain('wk-rc')
  expect(markup).not.toContain('wk-n"')
  expect(markup.match(/>Add a character</g)).toHaveLength(1)
  expect(markup).toMatch(/<button[^>]*class="gb-btn wk-add-btn"[^>]*aria-haspopup="dialog"[^>]*aria-expanded="false"/)
  expect(markup).not.toContain('wk-pick')
  expect(markup.match(/class="wk-fc[ "]/g)).toHaveLength(4)
  expect(markup.match(/class="wk-out"/g)).toHaveLength(4)
  expect(markup).toContain('aria-label="Take Sandrone out of the focus"')
})

const rows = [
  { key: 'nefer', name: 'Nefer', icon: null, owned: true, focus: null },
  { key: 'sandrone', name: 'Sandrone', icon: null, owned: true, focus: 1 },
  { key: 'amber', name: 'Amber', icon: null, owned: false, focus: null },
  { key: 'albedo', name: 'Albedo', icon: null, owned: false, focus: 2 },
]
const pickList = (list = rows, query = '') =>
  renderToStaticMarkup(createElement(PickList, { rows: list, query, onQuery: () => undefined, onPick: () => undefined }))

test('the list has a search box, faces and names, the ones in the focus marked, and the rest greyed as pre-farm', () => {
  const markup = pickList()
  expect(markup).toMatch(/<input[^>]*type="search"[^>]*aria-label="Search characters"/)
  expect(markup.match(/class="wk-opt[ "]/g)).toHaveLength(4)
  expect(markup.match(/class="pic cx-ava wk-ava/g)).toHaveLength(4)
  expect(markup.match(/In focus/g)).toHaveLength(2)
  expect(markup.match(/Pre-farm/g)).toHaveLength(2)
  expect(markup).toMatch(/class="wk-opt pre"[^>]*>[\s\S]*?Amber/)
  expect(markup.match(/aria-disabled="true"/g)).toHaveLength(2)
  expect(markup).toMatch(/aria-disabled="true"[^>]*>[\s\S]*?Sandrone[\s\S]*?In focus/)
})

test('a search with no match says so in one line', () => {
  expect(pickList([], 'zz')).toContain('No character matches')
})

test('with four in the focus the list says so at its top, and no row can be picked', () => {
  const full = renderToStaticMarkup(createElement(PickList, { rows, query: '', full: true, onQuery: () => undefined, onPick: () => undefined }))
  expect(full).toMatch(/^<input[^>]*><p class="wk-full"[^>]*>Four at most: take one out first\.<\/p><ul/)
  expect(full.match(/aria-disabled="true"/g)).toHaveLength(4)
  expect(pickList()).not.toContain('wk-full')
})

test("the cross sits in the card's corner and takes no column of its own, so the text keeps its width", () => {
  expect(ruleOf('.wk-fc')).toMatch(/grid-template-columns:\s*56px 1fr auto;/)
  expect(ruleOf('.wk-fc')).toMatch(/position:\s*relative/)
  expect(ruleOf('.wk-out')).toMatch(/position:\s*absolute/)
  // The arrows stand below the cross, clear of it however short the card.
  expect(ruleOf('.wk-ord')).toMatch(/padding-top:\s*2\dpx/)
})

test('the focus cards have the goal, the ready date and arrows that rest at the ends', () => {
  const markup = draw()
  expect(markup).toContain('1. Sandrone')
  expect(markup).toContain('Talents today; the goblet is luck')
  expect(markup).toMatch(/aria-label="Plan Sandrone earlier" disabled=""/)
  expect(markup).not.toMatch(/aria-label="Plan Odette earlier" disabled=""/)
  expect(markup).toMatch(/aria-label="Plan Jahoda later" disabled=""/)
  expect(markup.match(/class="wk-fc empty"/g)).toBeNull()
  const three = draw({ ...week, focus: week.focus.slice(0, 3) })
  expect(three.match(/class="wk-fc empty"/g)).toHaveLength(1)
})

test('the agenda has a section for each day, and the alerts say what the core says, the Sunday one loudest', () => {
  const markup = draw()
  expect(markup.match(/class="wk-sec( on)?"/g)).toHaveLength(7)
  expect(markup).toContain('<em>Today, </em>Sunday 25 October')
  expect(markup).toMatch(/class="wk-alert"><svg[\s\S]*?<\/svg>Weekly bosses reset tomorrow at 04:00: do them today\./)
  expect(markup).toContain('class="wk-alert calm"')
  expect(markup).toContain('On the way, no resin')
})

test('a tick is on the steps of today only, and a ticked step is pressed', () => {
  const markup = draw()
  expect(markup.match(/class="wk-tick"/g)).toHaveLength(6)
  expect(markup.match(/class="wk-step[^"]* plain"/g)).toHaveLength(week.days.slice(1).reduce((all, day) => all + day.tasks.length, 0))
  const ticked = { ...week, days: week.days.map((day, index) => (index === 0 ? { ...day, tasks: day.tasks.map((task, at) => (at === 0 ? { ...task, done: true } : task)) } : day)) }
  expect(draw(ticked)).toMatch(/class="wk-step done"[\s\S]*?aria-pressed="true"/)
})

test('pointing at a character lights only their steps and dims the rest', () => {
  const markup = draw(week, 'odette')
  const dims = markup.match(/class="wk-step[^"]* dim[^"]*"/g) ?? []
  const steps = week.days.reduce((all, day) => all + day.tasks.length, 0)
  const odettes = week.days.reduce((all, day) => all + day.tasks.filter((task) => task.who.includes('odette')).length, 0)
  expect(dims).toHaveLength(steps - odettes)
  expect(markup).toContain('class="wk-fc hot"')
})

test('the week grid has a row for each character in the focus and a column for each day, with a frame and the reset line', () => {
  const markup = draw()
  expect(markup.match(/class="wk-dh"/g)).toHaveLength(7)
  expect(markup).toContain('data-reset=""')
  expect(markup.match(/class="wk-rowh/g)).toHaveLength(4)
  expect(markup.match(/class="wk-cell"/g)).toHaveLength(28)
  expect(markup).toContain('class="wk-sel"')
  expect(markup).toContain('class="wk-reset"')
  expect(markup).toContain('Weekly reset')
})

test('the side has three blocks: when each is ready, where the resin goes, and how it plans', () => {
  const markup = draw()
  expect(markup).toContain('When each is ready')
  expect(markup).toContain('goblet: half a chance by Wed 11 Nov')
  expect(markup).toContain('1,270 of 1,280')
  expect(markup.match(/wk-blk wk-how[\s\S]*?<\/aside>/)?.[0].match(/<b>/g)).toHaveLength(4)
})

test('an empty week shows one line, not empty boxes', () => {
  const markup = draw(none)
  expect(markup).toContain('Choose up to four characters to plan for.')
  expect(markup).not.toContain('wk-sec')
  expect(markup).not.toContain('wk-step')
  expect(markup).not.toContain('wk-dh')
  expect(markup).not.toContain('wk-grow')
  // One line where the focus goes, one in the agenda, and no empty box: no empty slots, no empty chart, no empty grid.
  expect(markup.match(/Choose up to four characters to plan for\./g)).toHaveLength(1)
  expect(markup.match(/Nothing to show until you choose a character\./g)).toHaveLength(1)
  expect(markup).not.toContain('Room for one more')
  expect(markup.match(/>Add a character</g)).toHaveLength(1)
  expect(markup).not.toContain('wk-fc')
  expect(markup).not.toContain('When each is ready')
  expect(markup).not.toContain('wk-week')
  expect(markup).toContain('How it plans')
  // With some in the focus, the slots left are plain lines still.
  expect(draw({ ...week, focus: week.focus.slice(0, 3) }).match(/class="wk-fc empty"/g)).toHaveLength(1)
})

test('the agenda is of a fixed height and scrolls, and the page keeps its 1,440 pixels with no zoom of its own', () => {
  expect(ruleOf('.wk-main')).toMatch(/min-height:\s*7\d\dpx/)
  expect(ruleOf('.wk-agenda')).toMatch(/position:\s*absolute/)
  expect(ruleOf('.wk-agenda')).toMatch(/inset:\s*0/)
  expect(ruleOf('.wk-agenda')).toMatch(/overflow-y:\s*auto/)
  expect(plain).not.toMatch(/zoom:/)
  expect(ruleOf('.wk-side')).toMatch(/justify-content|align-content:\s*space-between/)
})

test('the page moves nothing with a CSS transform that a script also animates', () => {
  // The frame in the grid moves by left and width; the only transforms in the sheet are fixed ones.
  expect(ruleOf('.wk-sel')).not.toMatch(/transform/)
  expect(css).not.toMatch(/transition:[^;]*transform/)
})

test('the text of week.css reaches 4.5:1 on its grounds', () => {
  const t = tokenValues(tokens)
  const soft = '#F1E3C2'
  expect(contrast('#7A5418', soft)).toBeGreaterThanOrEqual(4.5) // the alert's text and the step numbers
  expect(contrast(t['--gold-ink'], t['--ledger-light'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--gold-ink'], t['--surface'])).toBeGreaterThanOrEqual(4.5) // the ready date on a focus card, today's head
  expect(contrast('#2E6B1F', '#DDEBD3')).toBeGreaterThanOrEqual(4.5) // the ready chip
  expect(contrast(t['--muted'], t['--ledger-light'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--button-text'], t['--button'])).toBeGreaterThanOrEqual(4.5) // the numbers on the resin bar and the focus badge
  expect(contrast('#FFFFFF', '#4F7C42')).toBeGreaterThanOrEqual(4.5) // a ticked segment
})
