export type Page =
  | 'board' | 'achievements' | 'backlog' | 'map' | 'stats' | 'pulls'
  | 'ledger' | 'friends'
  | 'characters' | 'week'
  | 'codes' | 'gallery' | 'settings' | 'tuskfizzy'

const PAGES: readonly string[] = [
  'board', 'achievements', 'backlog', 'map', 'stats', 'pulls',
  'ledger', 'friends',
  'characters', 'week',
  'codes', 'gallery', 'settings', 'tuskfizzy',
]

export const PAGE_TITLES: Record<Page, string> = {
  board: 'Board',
  achievements: 'Achievements',
  backlog: 'Backlog',
  map: 'Map',
  stats: 'Stats',
  pulls: 'Pulls',
  ledger: 'Ledger',
  friends: 'Friends',
  characters: 'Characters',
  week: 'Your week',
  codes: 'Gift codes',
  gallery: 'Gallery',
  settings: 'Settings',
  tuskfizzy: 'Daily Tracker',
}

/** The special categories of the Achievements page that have an address of their own (spec 12.3). */
export type SpecialPlace = 'timers' | 'commissions'

const SPECIAL_PLACES: readonly string[] = ['timers', 'commissions']

/** The parts of a location hash's path: "#/achievements/timers" -> ["achievements", "timers"]. */
function pathOf(hash: string): string[] {
  return hash.replace(/^#\/?/, '').split('?')[0].split('/')
}

/** The page a location hash names. Anything unknown is the Board, the page Guild Board opens on (spec 12.2). */
export function pageOf(hash: string): Page {
  const name = pathOf(hash)[0]
  return PAGES.includes(name) ? (name as Page) : 'board'
}

/**
 * Where a location hash goes, as the Characters plan has it: the roster at `#/characters`, the Endgame tab at
 * `#/characters/endgame`, a character's profile at `#/characters/<key>` and her planner at `#/characters/<key>/plan`, and
 * Your week at `#/week`. A key is a slug (`sandrone`, `lan-yan`). Every other page is its page alone, and anything that
 * is not one of the five addresses is the roster, never a page that is not there.
 */
export type Route =
  | { page: Exclude<Page, 'characters' | 'friends' | 'stats'> }
  | { page: 'stats'; view: 'overview' }
  | { page: 'stats'; view: 'months'; ym: string | null }
  | { page: 'stats'; view: 'year'; year: number | null }
  | { page: 'characters'; view: 'roster' | 'endgame' }
  | { page: 'characters'; view: 'profile' | 'plan'; key: string }
  | { page: 'friends'; view: 'list' | 'me' }
  | { page: 'friends'; view: 'friend' | 'compare'; user: string }

/** A character's key: the slug of her name, as the core makes it (metrics.slug). */
const SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

/** A player's id, as the core takes it (api/app.py, PLAYER_ID): a lower case UUID. Anything else never reaches a request. */
const PLAYER = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/

/**
 * Friends, as the online plan has it: the list at `#/friends`, Your profile at `#/friends/me`, a friend's profile at
 * `#/friends/<id>` and Compare at `#/friends/<id>/compare`. Anything else under `#/friends` is the list.
 */
function friendsRoute(hash: string): Route {
  const [, second, third, ...rest] = pathOf(hash)
  const list: Route = { page: 'friends', view: 'list' }
  if (second === undefined || second === '' || rest.length > 0) return list
  if (second === 'me') return third === undefined ? { page: 'friends', view: 'me' } : list
  if (!PLAYER.test(second)) return list
  if (third === undefined) return { page: 'friends', view: 'friend', user: second }
  return third === 'compare' ? { page: 'friends', view: 'compare', user: second } : list
}

/** A month of the Months tab: `2026-10`. */
const MONTH = /^\d{4}-(?:0[1-9]|1[0-2])$/

/** A year of the Year tab: `2026`. */
const YEAR = /^\d{4}$/

/**
 * Stats: Overview at `#/stats`, the Months tab at `#/stats/months` and a month at `#/stats/months/2026-10`, the Year tab
 * at `#/stats/year` and a year at `#/stats/year/2026`. A month or year that is not one is null, which the tab turns into
 * the newest one that has data; anything else under `#/stats` is Overview.
 */
function statsRoute(hash: string): Route {
  const [, second, third, ...rest] = pathOf(hash)
  const overview: Route = { page: 'stats', view: 'overview' }
  if (rest.length > 0) return overview
  if (second === 'months') {
    return { page: 'stats', view: 'months', ym: third !== undefined && MONTH.test(third) ? third : null }
  }
  if (second === 'year') {
    return { page: 'stats', view: 'year', year: third !== undefined && YEAR.test(third) ? Number(third) : null }
  }
  return overview
}

/** The route a location hash names. `pageOf` still gives the page alone, for the callers that want no more. */
export function routeOf(hash: string): Route {
  const page = pageOf(hash)
  if (page === 'friends') return friendsRoute(hash)
  if (page === 'stats') return statsRoute(hash)
  if (page !== 'characters') return { page }
  const [, second, third, ...rest] = pathOf(hash)
  const roster: Route = { page: 'characters', view: 'roster' }
  if (second === undefined || second === '') return roster
  if (rest.length > 0) return roster
  if (second === 'endgame') return third === undefined ? { page: 'characters', view: 'endgame' } : roster
  if (!SLUG.test(second)) return roster
  if (third === undefined) return { page: 'characters', view: 'profile', key: second }
  return third === 'plan' ? { page: 'characters', view: 'plan', key: second } : roster
}

/** The address of a page, or of a route. */
export function hashOf(target: Page | Route): string {
  if (typeof target === 'string') return `#/${target}`
  if (target.page === 'friends') {
    switch (target.view) {
      case 'list':
        return '#/friends'
      case 'me':
        return '#/friends/me'
      case 'friend':
        return `#/friends/${target.user}`
      case 'compare':
        return `#/friends/${target.user}/compare`
    }
  }
  if (target.page === 'stats') {
    switch (target.view) {
      case 'overview':
        return '#/stats'
      case 'months':
        return target.ym === null ? '#/stats/months' : `#/stats/months/${target.ym}`
      case 'year':
        return target.year === null ? '#/stats/year' : `#/stats/year/${target.year}`
    }
  }
  if (target.page !== 'characters') return `#/${target.page}`
  switch (target.view) {
    case 'roster':
      return '#/characters'
    case 'endgame':
      return '#/characters/endgame'
    case 'profile':
      return `#/characters/${target.key}`
    case 'plan':
      return `#/characters/${target.key}/plan`
  }
}

/**
 * The special category a location hash names on the Achievements page: "#/achievements/timers", as a click on a
 * reminder opens it (spec 10), or "#/achievements/commissions". Null for anything else.
 */
export function placeOfHash(hash: string): SpecialPlace | null {
  const path = pathOf(hash)
  return path.length === 2 && path[0] === 'achievements' && SPECIAL_PLACES.includes(path[1])
    ? (path[1] as SpecialPlace)
    : null
}

/** The address of a special category, or of the Achievements page itself for every other place on it. */
export function hashOfPlace(special: SpecialPlace | null): string {
  return special === null ? hashOf('achievements') : `${hashOf('achievements')}/${special}`
}
