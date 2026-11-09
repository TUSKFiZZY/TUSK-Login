/** Which addresses the shell opens, shows or lets through: the Qt shell's rules (dashboard.py, netpolicy.py, login.py). */

// A link opens in the player's own browser only when its address starts with one of these. Each ends in "/",
// so a look-alike host cannot pass.
export const EXTERNAL_LINKS: readonly string[] = [
  'https://genshin-impact.fandom.com/',
  'https://act.hoyolab.com/ys/app/interactive-map/',
  // The profile card's links (the gift codes design, section 1).
  'https://akasha.cv/profile/',
  'https://enka.network/u/',
  'https://act.hoyolab.com/app/community-game-records-sea/',
  // HoYoLAB's own check-in page, where the player claims when it asks for a check (the daily check-in design).
  'https://act.hoyolab.com/ys/event/signin-sea-v3/',
  // Settings, About: TUSKFiZZY's upstream source and privacy notice. The player's browser opens them; TUSKFiZZY asks for nothing.
  'https://github.com/QL1STY/guild-board/',
]

/** The core's allow-list (`ALLOWED_DOMAINS` in netpolicy.py). */
export const ALLOWED_DOMAINS: readonly string[] = ['hoyolab.com', 'hoyoverse.com', 'mihoyo.com']

// The sign-in pages of the services HoYoLAB's login offers beside its own (Google, Facebook, Apple, X and Discord), by
// their exact hosts: a player who signs in that way can finish in the window, and the verification window too. A
// service this list misses is signed in with elsewhere, and the login pasted.
export const SIGN_IN_HOSTS: readonly string[] = [
  'accounts.google.com', 'www.facebook.com', 'm.facebook.com', 'appleid.apple.com', 'x.com', 'twitter.com',
  'api.twitter.com', 'discord.com',
]

function parse(url: string): URL | null {
  try {
    return new URL(url)
  } catch {
    return null
  }
}

/** The host in lower case with no trailing dot, as netpolicy's `_host` gives it. */
export function hostOf(parts: URL): string {
  return parts.hostname.toLowerCase().replace(/\.+$/, '')
}

/** Whether a link goes to the player's own browser: its address starts with one of EXTERNAL_LINKS. */
export function opensOutside(url: string): boolean {
  return EXTERNAL_LINKS.some((prefix) => url.startsWith(prefix))
}

/** Whether `host` is one of HoYoverse's domains or a subdomain of one. */
export function inHoyoverseDomains(host: string): boolean {
  return ALLOWED_DOMAINS.some((domain) => host === domain || host.endsWith(`.${domain}`))
}

/** `netpolicy.is_hoyoverse`: https only, and the host is one of the domains or ends with "." and one of them. */
export function isHoyoverse(url: string): boolean {
  const parts = parse(url)
  return parts !== null && parts.protocol === 'https:' && inHoyoverseDomains(hostOf(parts))
}

/**
 * `login.sign_in_site`: whether the login and verification windows may show `url` in a frame: one of HoYoverse's sites,
 * or a page on one of SIGN_IN_HOSTS over https, with no other port and no user name.
 */
export function signInSite(url: string): boolean {
  if (isHoyoverse(url)) return true
  const parts = parse(url)
  return (
    parts !== null && parts.protocol === 'https:' && parts.port === '' && parts.username === '' && parts.password === '' &&
    SIGN_IN_HOSTS.includes(parts.hostname)
  )
}

/** Whether `url` is on `origin` (scheme, host and port). An opaque origin (`data:`, `about:`) is never the same. */
export function sameOrigin(url: string, origin: string): boolean {
  const a = parse(url)
  const b = parse(origin)
  if (a === null || b === null) return false
  return a.origin !== 'null' && a.origin === b.origin
}
