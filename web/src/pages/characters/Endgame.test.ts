import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, test } from 'vitest'
import fixture from '../../fixtures/characters/endgame.json'
import type { Element, Endgame as EndgameModel } from '../../types.ts'
import { contrast, tokenValues } from '../../styles/contrast.ts'
import tokens from '../../styles/tokens.css?raw'
import { EndgameView } from './Endgame.tsx'
import css from './endgame.css?raw'

const endgame = fixture as unknown as EndgameModel
const plain = css.replace(/\/\*[\s\S]*?\*\//g, '')

/** The declarations of a rule of endgame.css with exactly this selector. */
function ruleOf(selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\>]/g, '\\$&')
  return new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(plain)?.[1] ?? ''
}

const draw = (model: EndgameModel = endgame, picked: readonly Element[] = model.theater.elements, refusal: string | null = null) =>
  renderToStaticMarkup(createElement(EndgameView, { endgame: model, picked, refusal, onPick: () => undefined, onBack: () => undefined, onFocus: () => undefined }))

test('the three modes each have a ring with its percent and the core\'s words', () => {
  const markup = draw()
  expect(markup.match(/class="gb-panel eg-mode"/g)).toHaveLength(3)
  expect(markup).toContain('aria-label="77% ready"')
  expect(markup).toContain('aria-label="75% ready"')
  expect(markup).toContain('aria-label="79% ready"')
  expect(markup).toContain('33 of 36 stars')
  expect(markup).toContain('Cryo, Electro, Anemo')
})

test('the Abyss shows its two teams with the core\'s sentence about the weakest member and the lost star', () => {
  const markup = draw()
  expect(markup).toContain('Spiral Abyss, floor 12')
  expect(markup).toContain('First half')
  expect(markup).toContain('Its weakest member is Dahlia, at 58%.')
  expect(markup).toContain('You lost a star in 12-3, second half with this team.')
  expect(markup.match(/class="eg-team"/g)).toHaveLength(2 + 1) // two for the Abyss and one for the Theater
})

test('Stygian bosses show what does well and badly, and their own team or a plain line', () => {
  const markup = draw()
  expect(markup).toContain('Pyro does well')
  expect(markup).toContain('Cryo does badly')
  expect(markup.match(/class="eg-boss"/g)).toHaveLength(3)
  const none = draw({ ...endgame, stygian: endgame.stygian.map((boss) => ({ ...boss, team: null })) })
  expect(none.match(/No team fits yet/g)).toHaveLength(3)
})

test('the element chips press for the elements picked, and mark the calendar\'s as this month\'s', () => {
  const markup = draw()
  expect(markup.match(/aria-pressed="true"/g)).toHaveLength(3)
  expect(markup.match(/class="month"/g)).toHaveLength(3)
  // A pick of the player's: the chips pressed are the pick, and "this month" stays on the calendar's own.
  const own = draw({ ...endgame, theater: { ...endgame.theater, from_calendar: false, elements: ['Geo', 'Pyro', 'Hydro'] } })
  expect(own.match(/aria-pressed="true"/g)).toHaveLength(3)
  expect(own.match(/class="month"/g)).toHaveLength(3)
  expect(own).toMatch(/aria-pressed="true"[^>]*><i class="el-geo"/)
})

test("a kept pick has a button back to this month's elements, and the calendar's own show no such button", () => {
  expect(draw()).not.toContain("Back to this month's elements")
  const own = draw({ ...endgame, theater: { ...endgame.theater, from_calendar: false, elements: ['Geo', 'Pyro', 'Hydro'] } })
  expect(own).toContain('Back to this month&#x27;s elements')
})

test('the cast has a row for each element with its count, and the thinnest is marked', () => {
  const markup = draw()
  expect(markup).toContain('Your cast for these elements')
  expect(markup.match(/class="eg-crow/g)).toHaveLength(3)
  expect(markup.match(/class="eg-crow thin"/g)).toHaveLength(1)
  expect(markup).toContain('title="Your thinnest element this month"')
  expect(markup).toMatch(/<div class="cnt">3<small>ready<\/small>/)
})

test('"Close to counting" is shown with who is near, and hidden when nobody is', () => {
  expect(draw()).toContain('Anemo, 40% ready, 10 to go')
  const markup = draw({ ...endgame, theater: { ...endgame.theater, close: [] } })
  expect(markup).not.toContain('Close to counting')
  expect(markup).not.toContain('class="eg-cl"')
})

test('a month the calendar had no elements for says so and offers the picking', () => {
  const markup = draw({ ...endgame, theater: { ...endgame.theater, from_calendar: false, elements: [], cast: [], teams: [], close: [] } }, [])
  expect(markup).toContain("This month&#x27;s elements weren&#x27;t in HoYoLAB&#x27;s calendar. Pick them:")
  expect(markup).toContain('No full team of four in these elements yet.')
  expect(markup.match(/aria-pressed="false"/g)).toHaveLength(7)
})

test('each action to clear everything has its number, its words and one button of its kind, all the same size', () => {
  const markup = draw()
  expect(markup).toContain('Finish Sandrone&#x27;s plan')
  expect(markup).toContain('href="#/characters/sandrone/plan"')
  expect(markup).toMatch(/<button type="button" class="eg-go">Add to your focus<\/button>/)
  expect(markup.match(/class="eg-act"/g)).toHaveLength(3)
  expect(ruleOf('.eg-go')).toMatch(/min-width:\s*170px/)
  expect(ruleOf('.eg-go')).toMatch(/height:\s*38px/)
  const empty = draw({ ...endgame, clear: [] })
  expect(empty).toContain('Nothing stands in your way this month.')
  expect(empty).not.toContain('class="eg-act"')
})

test('a refusal, such as a fifth character for the focus, is said under the heading of the actions', () => {
  expect(draw(endgame, endgame.theater.elements, 'Four at most: take one out first.')).toContain('Four at most: take one out first.')
})

test('the columns end level: the left one shares its height between its sheets, and the Theater spreads its sections', () => {
  expect(ruleOf('.eg-cols')).toMatch(/align-items:\s*stretch/)
  expect(ruleOf('.eg-left')).toMatch(/grid-auto-rows:\s*1fr/)
  expect(ruleOf('.eg-sheet')).toMatch(/align-content:\s*space-between/)
  expect(ruleOf('.eg-left .eg-sheet')).toMatch(/grid-template-rows:\s*auto 1fr/)
  expect(ruleOf('.eg-left .eg-list')).toMatch(/grid-auto-rows:\s*1fr/)
  expect(plain).not.toMatch(/zoom:/)
})

test('the text of endgame.css reaches 4.5:1 on its grounds', () => {
  const t = tokenValues(tokens)
  expect(contrast('#7A5418', '#F1E3C2')).toBeGreaterThanOrEqual(4.5) // the step numbers, the buttons and "this month"
  expect(contrast(t['--gold-ink'], t['--ledger-light'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast('#2E6B1F', '#DDEBD3')).toBeGreaterThanOrEqual(4.5) // "does well"
  expect(contrast('#8A2E21', '#F3D9D5')).toBeGreaterThanOrEqual(4.5) // "does badly"
  expect(contrast(t['--muted'], t['--ledger-light'])).toBeGreaterThanOrEqual(4.5)
  expect(contrast(t['--gold-ink'], t['--surface'])).toBeGreaterThanOrEqual(4.5) // the sheets' small headings
})
