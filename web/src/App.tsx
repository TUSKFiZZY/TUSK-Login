import { type ReactNode, useEffect, useLayoutEffect, useState } from 'react'
import { type Live, useLive } from './live.ts'
import { Achievements } from './pages/Achievements.tsx'
import { Backlog } from './pages/Backlog.tsx'
import { Codes } from './pages/Codes.tsx'
import { Board } from './pages/Board.tsx'
import { Characters } from './pages/characters/Characters.tsx'
import { Week } from './pages/characters/Week.tsx'
import { Compare } from './pages/Compare.tsx'
import { FirstRun } from './pages/FirstRun.tsx'
import { FriendProfile } from './pages/FriendProfile.tsx'
import { Friends } from './pages/Friends.tsx'
import { Gallery } from './pages/Gallery.tsx'
import { Overlay } from './pages/Overlay.tsx'
import { MapPage } from './pages/Map.tsx'
import { Pulls } from './pages/Pulls.tsx'
import { Ledger } from './pages/Ledger.tsx'
import { Settings } from './pages/Settings.tsx'
import { Stats } from './pages/Stats.tsx'
import { YourProfile } from './pages/YourProfile.tsx'
import { hashOf, type Page, pageOf, routeOf } from './route.ts'
import { placeWhileHidden, weekShown } from './pages/characters/hidden.ts'
import { closeNote } from './ui/note.tsx'
import { PageBoundary } from './ui/PageBoundary.tsx'

function useHash(): string {
  const [hash, setHash] = useState(() => window.location.hash)
  useEffect(() => {
    const onChange = () => setHash(window.location.hash)
    window.addEventListener('hashchange', onChange)
    return () => window.removeEventListener('hashchange', onChange)
  }, [])
  return hash
}

export default function App() {
  const hash = useHash()
  const page = pageOf(hash)
  const live = useLive()
  // A page that opens shows no note (undo design, section 6): the one a page left goes with it.
  useEffect(() => closeNote(), [page])
  // The Gallery is there only while the player has it turned on: its address otherwise opens Settings, where it is.
  const galleryOff = page === 'gallery' && live.settings !== null && !live.settings.gallery_enabled
  useEffect(() => {
    if (galleryOff) window.location.replace(hashOf('settings'))
  }, [galleryOff])
  // Your week and the Endgame tab are hidden while the core says so: their addresses open the roster.
  const hiddenPlace = live.status !== null && !weekShown(live.status) ? placeWhileHidden(hash) : null
  useEffect(() => {
    if (hiddenPlace !== null) window.location.replace(hiddenPlace)
  }, [hiddenPlace])
  // The overlay's page (plan 2A-4), which the overlay's window shows over the game: closed at #/overlay, open at
  // #/overlay/open. Its document is see-through: main.tsx makes it so before React draws, and this keeps it in step
  // when the address changes later.
  const overlay = hash.startsWith('#/overlay')
  useLayoutEffect(() => {
    document.documentElement.classList.toggle('ov-doc', overlay)
  }, [overlay])
  if (overlay) {
    // Over the game nothing shows until the core has answered, and nothing before the risk statement is acknowledged.
    if (live.status === null || live.settings === null || !live.settings.risk_acknowledged) return null
    return <Overlay live={live} open={hash === '#/overlay/open'} />
  }
  if (live.status === null || live.settings === null) {
    // Nothing to show yet: either the first answer is on its way, or the core is not there.
    return (
      <div className="gb-wait" role="status">
        {live.problem ?? 'Opening TUSKFiZZY…'}
      </div>
    )
  }
  if (!live.settings.risk_acknowledged) {
    return <FirstRun live={live} status={live.status} settings={live.settings} />
  }
  // A page that fails as it is drawn leaves the rail and says so, and is drawn again at another address or the next event.
  return (
    <PageBoundary page={galleryOff ? 'settings' : page} live={live} again={[hash, live.version]}>
      {pageShown(page, hash, live, galleryOff, hiddenPlace)}
    </PageBoundary>
  )
}

/** The page the address names, in the frame of the rail. */
function pageShown(page: Page, hash: string, live: Live, galleryOff: boolean, hiddenPlace: string | null): ReactNode {
  if (page === 'achievements') return <Achievements live={live} />
  if (page === 'stats') {
    const stats = routeOf(hash)
    return stats.page === 'stats' ? <Stats live={live} route={stats} /> : null
  }
  if (page === 'settings' || galleryOff) return <Settings live={live} />
  if (page === 'gallery') return <Gallery live={live} />
  if (page === 'backlog') return <Backlog live={live} />
  if (page === 'pulls') return <Pulls live={live} />
  if (page === 'ledger') return <Ledger live={live} />
  const route = hiddenPlace !== null ? routeOf('#/characters') : routeOf(hash)
  if (route.page === 'friends') {
    if (route.view === 'me') return <YourProfile live={live} />
    if (route.view === 'friend') return <FriendProfile key={route.user} live={live} user={route.user} />
    if (route.view === 'compare') return <Compare key={route.user} live={live} user={route.user} />
    return <Friends live={live} />
  }
  if (route.page === 'characters') return <Characters live={live} route={route} />
  if (page === 'week') return <Week live={live} />
  if (page === 'codes') return <Codes live={live} />
  if (page === 'map') return <MapPage live={live} />
  return <Board live={live} />
}
