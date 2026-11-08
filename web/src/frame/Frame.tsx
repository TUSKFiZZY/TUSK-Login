import { type ReactNode, useState } from 'react'
import { api } from '../api.ts'
import { duration } from '../format.ts'
import { type Live, useTick } from '../live.ts'
import { hashOf, PAGE_TITLES, type Page } from '../route.ts'
import type { Status } from '../types.ts'
import { railPages, weekShown } from '../pages/characters/hidden.ts'
import { NoteHost } from '../ui/note.tsx'
import { Sign, type SignName } from '../ui/Sign.tsx'
import { Plate } from './Plate.tsx'
import { ProfileCard } from './ProfileCard.tsx'
import { ChangesModal } from './ChangesModal.tsx'
import { UpdateNotice, useInstall } from './UpdateNotice.tsx'

/** Each page with its sign (look pass spec 14.1), in the rail's order. */
const NAV: { page: Page; label: string; sign: SignName }[] = [
  { page: 'board', label: 'Board', sign: 'cells' },
  { page: 'tuskfizzy', label: 'Daily Tracker', sign: 'calendar' },
  { page: 'achievements', label: 'Achievements', sign: 'cup' },
  { page: 'characters', label: 'Characters', sign: 'bust' },
  { page: 'week', label: 'Your week', sign: 'book' },
  { page: 'backlog', label: 'Backlog', sign: 'map' },
  { page: 'map', label: 'Map', sign: 'compass' },
  { page: 'stats', label: 'Stats', sign: 'bars' },
  { page: 'pulls', label: 'Pulls', sign: 'star' },
  { page: 'ledger', label: 'Ledger', sign: 'scroll' },
  { page: 'friends', label: 'Friends', sign: 'people' },
  { page: 'gallery', label: 'Gallery', sign: 'oculus' },
  { page: 'settings', label: 'Settings', sign: 'gear' },
]

function gameLine(status: Status, now: Date): ReactNode {
  const game = status.game
  if (game.running && game.session_started_at !== null) {
    const minutes = Math.max(0, (now.getTime() - Date.parse(game.session_started_at)) / 60_000)
    return (
      <>
        Game running
        <br />
        This session {duration(minutes)}
      </>
    )
  }
  return (
    <>
      Game not running
      {game.last_session_minutes !== null && (
        <>
          <br />
          Last session {duration(game.last_session_minutes)}
        </>
      )}
    </>
  )
}

/** Shown until the player has seen it (spec section 10.6). The hint stays when the setting could not be saved. */
function TrayHint({ live }: { live: Live }) {
  return (
    <p className="gb-hint" data-testid="tray-hint">
      Closing this window keeps TUSKFiZZY running in the tray. To end it, choose Quit in the tray icon's menu.
      <button
        type="button"
        className="gb-link"
        onClick={() => void live.saveSettings({ tray_hint_shown: true }).catch(() => undefined)}
      >
        Got it
      </button>
    </p>
  )
}

export function Frame({
  page,
  live,
  title,
  heading,
  beside,
  controls,
  wide = false,
  bare = false,
  children,
}: {
  page: Page
  live: Live
  /** The page's own title where it is not the rail's name for it: "Your profile", or "You and Riley". */
  title?: string
  /** Drawn inside the title instead of its text, where the title carries more than words. */
  heading?: ReactNode
  /** Drawn right after the title and the demo badge: the page's freshness ring. */
  beside?: ReactNode
  controls?: ReactNode
  /** The page takes the window's whole width, as the Board does; the top bar and the banners keep the frame's. */
  wide?: boolean
  /** Only the page: no rail and no title, as the Map's Wide button asks. The banners and the note stay. */
  bare?: boolean
  children: ReactNode
}) {
  useTick(15_000) // "4 min ago" and the session timer move on
  const status = live.status
  const now = live.now()
  // While the core does not answer, the last numbers stay, but nothing is claimed about the sync or the game.
  const connected = live.problem === null
  // The list of changes: after an update it is the core's to show, once; before one it opens from the notice.
  const [listOpen, setListOpen] = useState(false)
  const { install, refused } = useInstall(live)
  const changes = status?.whats_new ?? null
  const update = status?.update ?? null
  const seen = async () => {
    try {
      await api.post('/api/whats-new/seen')
    } catch {
      // The list stays; the player can press Got it again.
    }
    await live.refresh()
  }
  return (
    <div className={bare ? 'gb bare' : 'gb'}>
      {!bare && (
        <nav className="gb-rail" aria-label="Pages">
          <div className="gb-brand">✦ TUSKFiZZY</div>
          {status !== null && <ProfileCard status={status} />}
          {/* The Gallery only while the player has it turned on (the ledger design, section 7). */}
          {NAV.filter((item) => item.page !== 'gallery' || live.settings?.gallery_enabled === true)
            .filter((item) => railPages([item.page], weekShown(status)).length > 0)
            .map((item) => (
            <a
              key={item.label}
              className={item.page === page ? 'gb-nav on' : 'gb-nav'}
              href={hashOf(item.page)}
              aria-current={item.page === page ? 'page' : undefined}
            >
              <Sign name={item.sign} />
              {item.label}
            </a>
          ))}
          <div className="gb-foot" data-testid="game">
            {status !== null && (connected ? gameLine(status, now) : 'Game status unknown')}
          </div>
        </nav>
      )}
      <main className={wide ? 'gb-main wide' : 'gb-main'}>
        <NoteHost />
        {!bare && (
          <header className="gb-top">
            <h1 className="gb-title" tabIndex={-1}>{heading ?? title ?? PAGE_TITLES[page]}</h1>
            {status?.mode === 'demo' && <span className="gb-badge">Demo data</span>}
            {beside}
            <span className="gb-grow" />
            {controls}
            {status !== null && <Plate live={live} status={status} />}
          </header>
        )}
        {live.problem !== null && (
          <div className="gb-banner" role="alert">
            {live.problem}
          </div>
        )}
        {connected && status !== null && status.history_notice !== null && (
          <div className="gb-banner" role="status" data-testid="history-notice">
            <span className="gb-grow">{status.history_notice.text}</span>
            {status.history_notice.page !== page && (
              <a className="gb-btn" href={hashOf(status.history_notice.page)}>
                Open {PAGE_TITLES[status.history_notice.page]}
              </a>
            )}
          </div>
        )}
        {status?.has_tray && live.settings !== null && !live.settings.tray_hint_shown && <TrayHint live={live} />}
        {update !== null && (
          <UpdateNotice update={update} install={install} refused={refused} onNotes={() => setListOpen(true)} />
        )}
        {changes !== null ? (
          <ChangesModal mode="after" version={changes.version} released={null} items={changes.items} onClose={() => void seen()} onInstall={() => undefined} />
        ) : (
          listOpen &&
          update !== null &&
          update.state !== 'none' &&
          update.version !== null && (
            <ChangesModal
              mode="before"
              version={update.version}
              released={update.released}
              items={update.notes}
              onClose={() => setListOpen(false)}
              onInstall={() => {
                setListOpen(false)
                void install()
              }}
            />
          )
        )}
        {children}
      </main>
    </div>
  )
}
