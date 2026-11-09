import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import '../styles/settings.css'
import { api, messageOf, restoreFile } from '../api.ts'
import { DataTable } from '../charts/ChartCard.tsx'
import { localDay, localTime } from '../format.ts'
import { ChangesModal } from '../frame/ChangesModal.tsx'
import { Frame } from '../frame/Frame.tsx'
import { type Live, useModel } from '../live.ts'
import type { Currency, Ledger } from '../ledgertypes.ts'
import { FAN_MADE, GALLERY_HANDS_OFF, HANDS_OFF, LEDGER_HANDS_OFF, PRIVACY_URL, RISK, SOURCE_URL } from '../texts.ts'
import type {
  Achievements as AchievementsModel, Changes, FishingRule, OverlayDisplay, OverlayStatus, Region, Settings as SettingsModel,
  Status, SyncLogRow,
} from '../types.ts'
import { Bar } from '../ui/marks.tsx'
import { Picture } from '../ui/Picture.tsx'
import type { PictureQuality } from '../ui/pictureQuality.ts'
import { Segmented } from '../ui/Segmented.tsx'
import { Sign, type SignName } from '../ui/Sign.tsx'
import { Switch } from '../ui/Switch.tsx'
import { ImportPanel } from './ImportPanel.tsx'
import { TuskfizzyImportPanel } from './TuskfizzyImportPanel.tsx'
import { ListPanel, useListUpdates } from './ListPanel.tsx'
import { hashOf } from '../route.ts'
import { weekShown } from './characters/hidden.ts'
import {
  accountLine, backupLine, BOTH_HOLD_DATA, cloudSpace, galleryFolderMissing, KEEP_REPLACES, KEEP_THIS_PC, type NoticeKey, type NoticeSwitches,
  noticeSwitches, onlineLine, opensSyncLog, overlayLine, syncDataLine, USE_REPLACES, USE_THE_ACCOUNT,
} from './settingsview.ts'
import { REGION_NAMES } from './timersview.ts'

const TRIGGERS = new Map([
  ['startup', 'Start-up'],
  ['post_session', 'After a session'],
  ['manual', 'Sync now'],
  ['interval', 'While playing'],
  ['login', 'After login'],
])

const OUTCOMES = new Map([
  ['ok', 'OK'],
  ['partial', 'Partly'],
  ['failed', 'Failed'],
  ['skipped', 'Skipped'],
])

const INTERVALS = [
  { value: '0', label: 'Off' },
  { value: '60', label: 'Every 60 min' },
  { value: '30', label: 'Every 30 min' },
] as const

/**
 * How large the overlay draws (Task 14), in percent: the Board's size, 100, unless the player chooses larger. Guild
 * Board's window zooms the overlay's view by it.
 */
const SIZES = [
  { value: '100', label: '100%' },
  { value: '125', label: '125%' },
  { value: '150', label: '150%' },
  { value: '175', label: '175%' },
  { value: '200', label: '200%' },
] as const

type Size = (typeof SIZES)[number]['value']

/** When fishing spots come back: guides disagree (spec 9.3), so the player picks what they see in the game. */
const FISHING_RULES: readonly { value: FishingRule; label: string }[] = [
  { value: '3_days', label: 'After 3 days' },
  { value: 'next_reset', label: 'At the next daily reset' },
]

const TEST_SENT = 'Sent. It shows in the corner of the screen, or in the notification centre while Do not disturb is on.'
const NO_TRAY = "Notifications come from Guild Board's tray icon, which is not running here."

/**
 * Runs a button's request and keeps what it answered, or why it failed, to show under the button until the next run or
 * `clear`.
 */
function useAction(live: Live) {
  const [notice, setNotice] = useState<string | null>(null)
  const run = async (action: () => Promise<string | null>) => {
    try {
      setNotice(await action())
    } catch (error) {
      setNotice(messageOf(error))
    }
    await live.refresh()
  }
  return { notice, run, clear: () => setNotice(null) }
}

/**
 * A control that saves one setting, through `run` so that a failure shows under it. The control follows the player's
 * latest choice at once. The saved value takes over again only when the latest save has settled, so that an earlier
 * save's answer does not undo a later choice. The saves go out one after another: a save that is slow to land is never
 * overtaken by a later one, and the core ends on the latest choice. (run never fails, as it keeps a failure to show, so
 * a later save still goes.)
 */
function useSetting<S extends SettingsModel, K extends keyof S>(
  live: Live,
  settings: S,
  name: K,
  run: (action: () => Promise<string | null>) => Promise<void>,
): [S[K], (value: S[K]) => void] {
  const [wanted, setWanted] = useState<S[K] | null>(null)
  const latest = useRef(0)
  const saving = useRef<Promise<void>>(Promise.resolve())
  const choose = (value: S[K]) => {
    const mine = (latest.current += 1)
    setWanted(value)
    const changes: Partial<S> = {}
    changes[name] = value
    saving.current = saving.current
      .then(() =>
        run(async () => {
          await live.saveSettings(changes as Partial<SettingsModel>)
          return null
        }),
      )
      .finally(() => {
        if (mine === latest.current) setWanted(null)
      })
  }
  return [wanted ?? settings[name], choose]
}

/** One group of settings: a panel that opens with a small round sign and the group's name. */
function Group({ name, sign, children }: { name: string; sign: SignName; children: ReactNode }) {
  const id = useId()
  return (
    <section className="gb-panel set-group" aria-labelledby={id}>
      <h2 className="set-head" id={id}>
        <span className="set-icon">
          <Sign name={sign} />
        </span>
        {name}
      </h2>
      {children}
    </section>
  )
}

/** One option: its name in bold, one plain sentence under it, and the control on the right. */
function Row({ name, sentence, children }: { name: string; sentence?: ReactNode; children?: ReactNode }) {
  return (
    <div className="set-row">
      <div className="set-tx">
        <b>{name}</b>
        {sentence !== undefined && <span>{sentence}</span>}
      </div>
      {children !== undefined && <div className="set-ctl">{children}</div>}
    </div>
  )
}

function Notice({ text }: { text: string | null }) {
  if (text === null) return null
  return (
    <p className="set-notice" role="status">
      {text}
    </p>
  )
}

function Account({ live, status }: { live: Live; status: Status }) {
  const { notice, run } = useAction(live)
  const [confirming, setConfirming] = useState(false)
  const account = status.account
  const loggedIn = status.mode === 'live' && account !== null && account.uid !== null
  const logout = () =>
    run(async () => {
      await api.post('/api/auth/logout')
      setConfirming(false)
      return null
    })
  const login = () =>
    run(async () => {
      await api.post('/api/auth/login')
      return null
    })
  const known = status.mode === 'demo' || loggedIn
  return (
    <section className="gb-panel set-group" aria-label="Account">
      <div className="set-acct">
        <span className="set-ava">
          <Picture src={account?.picture_url ?? null} sign="person" />
        </span>
        <div className="set-tx">
          {known && account !== null ? (
            <>
              <b className="set-nick">{account.nickname || '–'}</b>
              <span>{accountLine(account, status.mode)}</span>
            </>
          ) : (
            <>
              <b className="set-nick">Not logged in</b>
              <span>Guild Board reads your account once you log in to HoYoLAB.</span>
            </>
          )}
        </div>
        {status.mode === 'live' && (
          <div className="set-ctl">
            {loggedIn ? (
              !confirming && (
                <button type="button" className="gb-btn quiet" onClick={() => setConfirming(true)}>
                  Log out
                </button>
              )
            ) : (
              <button type="button" className="gb-btn" onClick={() => void login()}>
                Log in to HoYoLAB
              </button>
            )}
          </div>
        )}
      </div>
      {status.mode === 'demo' && (
        <p className="set-aside">This is the demo account. Its numbers are made up, and nothing is read from HoYoLAB.</p>
      )}
      {loggedIn && confirming && (
        <div className="set-more">
          <p>Logging out deletes the saved login from this PC. Your history stays.</p>
          <div className="set-actions">
            <button type="button" className="gb-btn" onClick={() => void logout()}>
              Log out
            </button>
            <button type="button" className="gb-btn quiet" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      <Notice text={notice} />
    </section>
  )
}

/**
 * The online account (the online design, section 5): what going online does and signing in, which happens in the system
 * browser, and once signed in signing out and Delete my online data, which asks first. A request can take a while when the
 * server is away, so the buttons wait for it: aria-disabled, which keeps the focus where disabled would drop it to the page.
 */
/**
 * The room the copy of the player's data takes on the account, under the Sync row while sync is on: one line, the meter, and the
 * items beneath it, a sentence when it is nearly full, or only that no place is free. Its place is kept while the core has not read
 * it yet, so that nothing below it moves when it arrives.
 */
function CloudRoom({ usage }: { usage: Status['online']['usage'] }) {
  const room = usage === null ? null : cloudSpace(usage)
  return (
    <div className="set-space" data-testid="cloud-space">
      {room?.kind === 'space' && (
        <>
          <span className="set-space-line">{room.line}</span>
          <Bar value={room.value} max={room.max} />
          <span className="set-space-items">{room.items}</span>
          {room.note !== null && <span className="set-space-note">{room.note}</span>}
        </>
      )}
      {room?.kind === 'full' && <span className="set-space-note">{room.sentence}</span>}
    </div>
  )
}

function Online({ live, status, settings }: { live: Live; status: Status; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [syncing, chooseSyncing] = useSetting(live, settings, 'sync_my_data', run)
  const [asking, setAsking] = useState(false)
  const [busy, setBusy] = useState(false)
  const view = status.online
  const waiting = busy || undefined
  const send = (path: string, body?: unknown) => {
    if (busy) return
    setAsking(false)
    setBusy(true)
    void run(async () => {
      try {
        await api.post(path, body)
        return null
      } finally {
        setBusy(false)
      }
    })
  }
  const signIn = () => send('/api/online/sign-in')
  return (
    <Group name="Online" sign="person">
      <Row name="Your online account" sentence={onlineLine(view)}>
        {view.state === 'signed_out' && (
          <button type="button" className="gb-btn" aria-disabled={waiting} onClick={signIn}>
            Sign in with Discord
          </button>
        )}
        {view.state === 'waiting' && (
          <button type="button" className="gb-btn quiet" aria-disabled={waiting} onClick={signIn}>
            Start again
          </button>
        )}
        {view.state === 'sign_in_again' && (
          <button type="button" className="gb-btn" aria-disabled={waiting} onClick={signIn}>
            Sign in again
          </button>
        )}
        {view.state === 'signed_in' && (
          <>
            <button type="button" className="gb-btn quiet" aria-disabled={waiting} onClick={() => send('/api/online/sign-out')}>
              Sign out
            </button>
            {!asking && (
              <button type="button" className="gb-btn quiet" aria-disabled={waiting} onClick={() => setAsking(!busy)}>
                Delete my online data
              </button>
            )}
          </>
        )}
      </Row>
      {view.state === 'signed_in' && view.sync !== null && (
        <Row name="Sync my data" sentence={syncDataLine({ ...view.sync, on: syncing }, localTime)}>
          <Switch on={syncing} label="Sync my data" className="set-switch" onPress={() => chooseSyncing(!syncing)} />
          {syncing && !view.sync.needs_choice && (
            <button type="button" className="gb-btn quiet" aria-disabled={waiting} onClick={() => send('/api/online/sync')}>
              Sync now
            </button>
          )}
        </Row>
      )}
      {view.state === 'signed_in' && view.sync !== null && syncing && !view.sync.needs_choice && <CloudRoom usage={view.usage} />}
      {view.state === 'signed_in' && view.sync?.needs_choice === true && syncing && (
        <div className="set-more" role="group" aria-label="Which data to keep">
          <p>{BOTH_HOLD_DATA}</p>
          <div className="set-choice">
            <button type="button" className="gb-btn" aria-disabled={waiting} onClick={() => send('/api/online/first-sync', { choice: 'keep' })}>
              {KEEP_THIS_PC}
            </button>
            <span>{KEEP_REPLACES}</span>
          </div>
          <div className="set-choice">
            <button type="button" className="gb-btn" aria-disabled={waiting} onClick={() => send('/api/online/first-sync', { choice: 'use' })}>
              {USE_THE_ACCOUNT}
            </button>
            <span>{USE_REPLACES}</span>
          </div>
        </div>
      )}
      {view.state !== 'signed_in' && (
        <p className="set-aside">
          Signing in puts your profile and the stats and data you switch on on Guild Board's online server; the{' '}
          <a className="set-link" href={PRIVACY_URL} target="_blank" rel="noopener noreferrer">
            privacy notice
          </a>{' '}
          says exactly what.
        </p>
      )}
      {view.state === 'signed_in' && view.offline && <p className="set-aside">The online server cannot be reached right now.</p>}
      {status.mode === 'demo' && <p className="set-aside">This account is made up, and nothing goes online in the demo.</p>}
      {asking && view.state === 'signed_in' && (
        <div className="set-more" role="alertdialog" aria-label="Confirm deleting your online data">
          <p>
            Delete your online account and everything stored with it? Friends will no longer find you, and this cannot be
            undone. What is on this PC stays.
          </p>
          <div className="set-actions">
            <button type="button" className="gb-btn" onClick={() => send('/api/online/delete', { confirm: true })}>
              Delete my online data
            </button>
            <button type="button" className="gb-btn quiet" onClick={() => setAsking(false)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      <Notice text={notice ?? view.message} />
    </Group>
  )
}

function Sync({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [minutes, choose] = useSetting(live, settings, 'in_session_interval_min', run)
  // The plate's See the log comes here as "#/settings/log": the log opens and is brought into view. The address goes
  // back to "#/settings", so that a later See the log changes it again, also while Settings is open.
  const [log, setLog] = useState(() => opensSyncLog(window.location.hash))
  const [reveal, setReveal] = useState(log)
  const box = useRef<HTMLDivElement>(null)
  const id = useId()
  useEffect(() => {
    const follow = () => {
      if (!opensSyncLog(window.location.hash)) return
      setLog(true)
      setReveal(true)
      window.history.replaceState(window.history.state, '', hashOf('settings'))
    }
    follow()
    window.addEventListener('hashchange', follow)
    return () => window.removeEventListener('hashchange', follow)
  }, [])
  useEffect(() => {
    if (!reveal || !log) return
    box.current?.scrollIntoView({ block: 'start' })
    setReveal(false)
  }, [reveal, log])
  const pause = () =>
    run(async () => {
      await api.post(settings.sync_paused ? '/api/sync/resume' : '/api/sync/pause')
      return null
    })
  return (
    <Group name="Sync" sign="sync">
      <Row name="Sync while you play" sentence="Keeps resin and dailies fresh during a session">
        <select
          className="set-select"
          aria-label="Sync while you play"
          value={String(minutes)}
          onChange={(event) => choose(Number(event.target.value) as SettingsModel['in_session_interval_min'])}
        >
          {INTERVALS.map((each) => (
            <option key={each.value} value={each.value}>
              {each.label}
            </option>
          ))}
        </select>
      </Row>
      <Row name="Pause syncing" sentence="Guild Board stops asking HoYoLAB until you turn this off">
        <Switch on={settings.sync_paused} label="Pause syncing" className="set-switch" onPress={() => void pause()} />
      </Row>
      <Notice text={notice} />
      <p className="set-centred">
        <button type="button" className="set-link" aria-expanded={log} aria-controls={id} onClick={() => setLog(!log)}>
          {log ? 'Hide the sync log' : 'Show the sync log'}
        </button>
      </p>
      {log && (
        <div id={id} ref={box} className="set-more">
          <p>
            Guild Board syncs when it starts and the last sync is more than 3 hours old, 3 minutes after you close the
            game, and when you press Sync now. It never syncs more than once in 5 minutes or more than 24 times a day.
          </p>
          <SyncLog live={live} />
        </div>
      )}
    </Group>
  )
}

function SyncLog({ live }: { live: Live }) {
  const { data, error } = useModel<SyncLogRow[]>('/api/synclog?limit=50', live.version)
  return (
    <section className="set-log" aria-label="Sync log">
      {data === null ? (
        <p>{error ?? 'Reading the sync log…'}</p>
      ) : data.length === 0 ? (
        <p>No sync has run yet.</p>
      ) : (
        <div className="set-log-table" tabIndex={0}>
          <DataTable
            table={{
              head: ['When', 'Trigger', 'Outcome', 'Message'],
              rows: data.map((row) => [
                localTime(row.started_at),
                TRIGGERS.get(row.trigger) ?? row.trigger,
                OUTCOMES.get(row.outcome) ?? row.outcome,
                row.message ?? '',
              ]),
            }}
          />
        </div>
      )}
    </section>
  )
}

function StartWithWindows({ live, status, settings }: { live: Live; status: Status; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [on, choose] = useSetting(live, settings, 'start_with_windows', run)
  return (
    <>
      <Row
        name="Start with Windows"
        sentence={
          <>
            Starts hidden in the tray, so every session's playtime is counted
            {!status.has_shell && <i>This takes effect when Guild Board runs in its own window.</i>}
          </>
        }
      >
        <Switch on={on} label="Start with Windows" className="set-switch" onPress={() => choose(!on)} />
      </Row>
      <Notice text={notice} />
    </>
  )
}

/** One of the Characters notices (the design's section 6.6): a switch for its kind, on until the player switches it off. */
function NoticeSwitchRow({ live, settings, name, sentence, notice: key }: { live: Live; settings: SettingsModel; name: string; sentence: string; notice: NoticeKey }) {
  const { notice, run } = useAction(live)
  const [value, choose] = useSetting(live, settings as SettingsModel & NoticeSwitches, key, run)
  const on = value !== false
  return (
    <>
      <Row name={name} sentence={sentence}>
        <Switch on={on} label={name} className="set-switch" onPress={() => choose(!on)} />
      </Row>
      <Notice text={notice} />
    </>
  )
}

/** Spec 10: Windows notifications when a timer is ready, and a test notification from the tray icon. */
function Notifications({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [on, choose] = useSetting(live, settings, 'notifications', run)
  const test = () =>
    run(async () => {
      const answer = await api.post<{ sent: boolean }>('/api/notifications/test')
      return answer.sent ? TEST_SENT : NO_TRAY
    })
  return (
    <>
      <Row name="Notifications" sentence="A Windows notification when a timer is ready">
        <button type="button" className="gb-btn quiet" onClick={() => void test()}>
          Send a test notification
        </button>
        <Switch on={on} label="Notifications" className="set-switch" onPress={() => choose(!on)} />
      </Row>
      <Notice text={notice} />
      {(weekShown(live.status) ? noticeSwitches(settings as SettingsModel & NoticeSwitches) : []).map((row) => (
        <NoticeSwitchRow key={row.key} live={live} settings={settings} name={row.name} sentence={row.sentence} notice={row.key} />
      ))}
    </>
  )
}

/** The keys a hotkey can end on, by their place on the keyboard (KeyboardEvent.code): A to Z, 0 to 9, F1 to F12. */
const HOTKEY_KEYS = /^(?:Key([A-Z])|Digit([0-9])|(F[1-9]|F1[0-2]))$/

/**
 * The hotkey a key press makes, as the core writes it (hotkeys.py): the modifiers in the order Ctrl, Alt, Shift, Win,
 * then the key. Null for a modifier alone and for a key a hotkey cannot end on. The key is read from its place on the
 * keyboard, not from what it types: on the player's Polish layout Windows reports AltGr as Ctrl+Alt, and AltGr+E types
 * "ę". It shows as Ctrl+Alt+E, which the core refuses with the reason.
 */
function hotkeyOf(press: Pick<KeyboardEvent, 'code' | 'ctrlKey' | 'altKey' | 'shiftKey' | 'metaKey'>): string | null {
  const found = HOTKEY_KEYS.exec(press.code)
  if (found === null) return null
  const modifiers = [press.ctrlKey && 'Ctrl', press.altKey && 'Alt', press.shiftKey && 'Shift', press.metaKey && 'Win']
  return [...modifiers.filter((name) => name !== false), found[1] ?? found[2] ?? found[3]].join('+')
}

/**
 * The overlay's hotkey. The field records the keys pressed in it, and Save hotkey saves them; the core refuses a hotkey
 * with a problem, and why shows under the field. Under that, whether Guild Board's window holds the hotkey.
 */
function OverlayHotkey({
  live,
  settings,
  overlay,
}: {
  live: Live
  settings: SettingsModel
  overlay: OverlayStatus | null
}) {
  const { notice, run, clear } = useAction(live)
  const [keys, setKeys] = useState<string | null>(null) // recorded, and not saved yet
  const id = useId()
  const save = () => {
    const chosen = keys ?? settings.overlay_hotkey
    return run(async () => {
      await live.saveSettings({ overlay_hotkey: chosen })
      setKeys((current) => (current === chosen ? null : current)) // unless other keys were pressed meanwhile
      return null
    })
  }
  return (
    <div className="set-part">
      <div className="set-line">
        <label htmlFor={id}>Hotkey</label>
        <span className="set-field">
          <input
            id={id}
            type="text"
            className="set-keys"
            readOnly
            value={keys ?? settings.overlay_hotkey}
            onKeyDown={(event) => {
              const pressed = hotkeyOf(event)
              if (pressed === null) return // Tab still moves the focus
              event.preventDefault() // and the browser does not act on the keys, as it would on F5
              setKeys(pressed)
              clear() // a refusal under the field was about the keys before
            }}
          />
          <button type="button" className="gb-btn quiet" onClick={() => void save()}>
            Save hotkey
          </button>
        </span>
      </div>
      <Notice text={notice} />
      {overlay?.hotkey_state === 'held' && <p className="set-aside">{overlay.hotkey} opens and closes the overlay.</p>}
      {overlay?.hotkey_state === 'taken' && (
        <p className="set-aside">Another program uses {overlay.hotkey}. Choose another.</p>
      )}
    </div>
  )
}

/**
 * The pinned widgets' opacity, from 50 to 100%. The value beside the slider follows it, and `choose` hears of it when
 * the slider is let go: on the input's own change event, which comes once at the end of a drag, and at each step a key
 * takes. React's onChange comes at every step of a drag. A drag that ends where it began sends no change event, so the
 * slider follows `value` again once the focus leaves it.
 */
function Opacity({ value, choose }: { value: number; choose: (value: number) => void }) {
  const [moving, setMoving] = useState<number | null>(null)
  const slider = useRef<HTMLInputElement>(null)
  const id = useId()
  useEffect(() => {
    // After every render, so that the change event reaches the latest choose.
    const input = slider.current
    if (input === null) return
    const letGo = () => {
      setMoving(null)
      choose(Number(input.value))
    }
    input.addEventListener('change', letGo)
    return () => input.removeEventListener('change', letGo)
  })
  const shown = moving ?? value
  return (
    <div className="set-part">
      <div className="set-line">
        <label htmlFor={id}>Pinned widgets' opacity</label>
        <span className="set-range">
          <input
            ref={slider}
            id={id}
            type="range"
            min={50}
            max={100}
            step={5}
            value={shown}
            aria-valuetext={`${shown}%`}
            onChange={(event) => setMoving(Number(event.target.value))}
            onBlur={() => setMoving(null)}
          />
          <span aria-hidden="true">{shown}%</span>
        </span>
      </div>
      <p className="set-aside">Lower lets more of the game show through them.</p>
    </div>
  )
}

/** While the shell lists no display, as without Guild Board's window: "" is the main display, whichever it is. */
const MAIN_DISPLAY: OverlayDisplay = { name: '', label: 'The main display', primary: true }

/**
 * Spec 12.5 and 14.1: the overlay's hotkey, the display it covers, its size (Task 14), the pinned widgets over the game
 * and the lines over the game. Guild Board's window reports the displays and whether it holds the hotkey. Without the
 * window there is no overlay, and the settings still save. They open under the Overlay row, from its Change.
 */
function OverlaySettings({ live, settings, id }: { live: Live; settings: SettingsModel; id: string }) {
  const { data: overlay } = useModel<OverlayStatus>('/api/overlay', live.version)
  const { notice, run } = useAction(live)
  const [display, chooseDisplay] = useSetting(live, settings, 'overlay_display', run)
  const [size, chooseSize] = useSetting(live, settings, 'overlay_size', run)
  const [opacity, chooseOpacity] = useSetting(live, settings, 'overlay_opacity', run)
  const [pinned, choosePinned] = useSetting(live, settings, 'overlay_pinned', run)
  const [lines, chooseLines] = useSetting(live, settings, 'overlay_lines', run)
  const [fullScreen, chooseFullScreen] = useSetting(live, settings, 'overlay_full_screen', run)
  const radios = useId() // the display's radio buttons, one group
  const listed = overlay?.displays ?? []
  const displays = listed.length === 0 ? [MAIN_DISPLAY] : listed
  // The display the overlay covers: the one chosen, or the main display while none is chosen or the one chosen is gone,
  // or else the first listed.
  const covered =
    displays.find((each) => each.name === display) ?? displays.find((each) => each.primary) ?? displays[0]
  return (
    <div id={id} className="set-more" role="group" aria-label="Overlay">
      {overlay?.available === false && (
        <p className="set-aside set-part">The overlay shows over the game when Guild Board runs with its window.</p>
      )}
      <OverlayHotkey live={live} settings={settings} overlay={overlay} />
      {/* Once the shell's list is read, so that the main display does not show alone before it. */}
      {overlay !== null && (
        <fieldset className="set-part set-choices">
          <legend>Display</legend>
          {displays.map((each) => (
            <label key={each.name} className="set-check">
              <input type="radio" name={radios} checked={each === covered} onChange={() => chooseDisplay(each.name)} />
              {each.label}
            </label>
          ))}
        </fieldset>
      )}
      <div className="set-part set-line">
        <span>Size</span>
        <Segmented
          label="Size"
          options={SIZES}
          value={String(size) as Size}
          onChange={(value) => chooseSize(Number(value))}
        />
      </div>
      <Opacity value={opacity} choose={chooseOpacity} />
      <div className="set-part set-line">
        <span>Show pinned widgets over the game</span>
        <Switch
          on={pinned}
          label="Show pinned widgets over the game"
          className="set-switch"
          onPress={() => choosePinned(!pinned)}
        />
      </div>
      <div className="set-part">
        <div className="set-line">
          <span>Lines over the game</span>
          <Switch on={lines} label="Lines over the game" className="set-switch" onPress={() => chooseLines(!lines)} />
        </div>
        <p className="set-aside">While the game runs, a line at the top of the overlay says when a timer is ready.</p>
      </div>
      <div className="set-part">
        <div className="set-line">
          <span>The game runs in full screen</span>
          <Switch
            on={fullScreen}
            label="The game runs in full screen"
            className="set-switch"
            onPress={() => chooseFullScreen(!fullScreen)}
          />
        </div>
        <p className="set-aside">
          Turn on if the game's Display Mode is Fullscreen, which minimizes the game when the open overlay takes the
          keyboard. The game then keeps the keyboard: hold Alt in the game to show the pointer and click, and close the
          overlay with its hotkey or Close. Notes and timers are typed on the Board.
        </p>
      </div>
      <Notice text={notice} />
    </div>
  )
}

/**
 * How large Guild Board's window draws its page, in percent of the size it grows to with the window (PAGE_SIZES in
 * src/guildboard/config.py and shell/src/dashboard.ts): one size while the window is not maximized, and one while it is,
 * so that a full screen can draw smaller than the usual window. The window steps the size of its state with Ctrl and + or
 * -, and with Ctrl and the mouse wheel, and saves the step, which shows here too.
 */
const PAGE_SIZES = ['70', '80', '90', '100', '110', '120', '130'].map((value) => ({ value, label: `${value}%` }))

function PageSizeChoice({ label, value, choose }: { label: string; value: number; choose: (value: number) => void }) {
  // A recording made before the settings has none: the window then draws at 100%.
  const shown = PAGE_SIZES.some((each) => each.value === String(value)) ? String(value) : '100'
  return (
    <span className="set-equal">
      <Segmented label={label} options={PAGE_SIZES} value={shown} onChange={(chosen) => choose(Number(chosen))} />
    </span>
  )
}

function PageSizes({ live, status, settings }: { live: Live; status: Status; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [inWindow, chooseInWindow] = useSetting(live, settings, 'page_size', run)
  const [maximized, chooseMaximized] = useSetting(live, settings, 'page_size_maximized', run)
  return (
    <>
      <Row name="Page size in a window" sentence="How large everything is drawn while the window is not maximized.">
        <PageSizeChoice label="Page size in a window" value={inWindow} choose={chooseInWindow} />
      </Row>
      <Row name="Page size when maximized" sentence="While the window fills the screen.">
        <PageSizeChoice label="Page size when maximized" value={maximized} choose={chooseMaximized} />
      </Row>
      <p className="set-aside">
        Ctrl with + or −, or with the mouse wheel, changes the size of the window as it is, and Ctrl with 0 sets it back to
        100%.{!status.has_shell && " These take effect in Guild Board's own window."}
      </p>
      <Notice text={notice} />
    </>
  )
}

/**
 * How large the Characters pages' big pictures are fetched (the Characters design, 11.7): a plain choice of three. The faces
 * and the small icons are the same in all of them; the picture of a character's card is the one that changes.
 */
const QUALITIES: readonly { value: PictureQuality; label: string }[] = [
  { value: 'high', label: 'High' },
  { value: 'medium', label: 'Medium' },
  { value: 'low', label: 'Low' },
]

function PictureQualityRow({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [quality, choose] = useSetting(live, settings, 'picture_quality', run)
  return (
    <>
      <Row
        name="Picture quality"
        sentence="High keeps the big pictures as they are. Medium and Low make them smaller, which saves memory."
      >
        <span className="set-equal">
          <Segmented label="Picture quality" options={QUALITIES} value={quality} onChange={choose} />
        </span>
      </Row>
      <Notice text={notice} />
    </>
  )
}

function Overlay({ live, settings }: { live: Live; settings: SettingsModel }) {
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <>
      <Row name="Overlay" sentence={overlayLine(settings.overlay_hotkey)}>
        <button type="button" className="set-link" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
          Change
        </button>
      </Row>
      {open && <OverlaySettings live={live} settings={settings} id={id} />}
    </>
  )
}

/**
 * The screenshot gallery's switch (the ledger, gallery and settings design, section 7), off until the player turns it on.
 * While it is on, the gallery is asked whether it found the game's screenshot folder; when it says it did not, a field
 * takes the folder's path. Nothing of the gallery is asked while it is off.
 */
function Gallery({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [on, choose] = useSetting(live, settings, 'gallery_enabled', run)
  return (
    <>
      <Row
        name="Screenshot gallery"
        sentence="Shows the photos you take in the game. Guild Board only looks at them, never moves or deletes one."
      >
        <Switch on={on} label="Screenshot gallery" className="set-switch" onPress={() => choose(!on)} />
      </Row>
      <Notice text={notice} />
      {settings.gallery_enabled && <GalleryFolder live={live} settings={settings} />}
    </>
  )
}

function GalleryFolder({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  // The gallery is asked when the switch turns it on or the page opens with it on, and again after each save; not at
  // each sync, which would list the folder again for nothing. An answer that fails, as before the gallery is there,
  // asks for nothing.
  const [asks, setAsks] = useState(0)
  const { data } = useModel<unknown>('/api/gallery', asks)
  const [path, setPath] = useState(settings.gallery_folder)
  const id = useId()
  if (!galleryFolderMissing(data)) return <Notice text={notice} />
  const save = () =>
    run(async () => {
      await live.saveSettings({ gallery_folder: path.trim() })
      setAsks((count) => count + 1)
      return null
    })
  return (
    <form
      className="set-more"
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      <label htmlFor={id}>Guild Board could not find the game's screenshot folder. Paste its path:</label>
      <span className="set-field set-path">
        <input id={id} type="text" spellCheck={false} value={path} onChange={(event) => setPath(event.target.value)} />
        <button type="submit" className="gb-btn quiet">
          Save
        </button>
      </span>
      <Notice text={notice} />
    </form>
  )
}

const CURRENCIES: readonly { value: Currency; label: string }[] = [
  { value: 'USD', label: 'US dollar' },
  { value: 'EUR', label: 'Euro' },
  { value: 'PLN', label: 'Polish złoty' },
]

/** A corrected price, at most (`config.py` MAX_PRICE). */
const MAX_PRICE = 100_000

/**
 * The currency of the Ledger's "Spent on Genshin" (the ledger design, sections 6 and 8), and "Edit prices", which opens
 * the price list in place: each pack with its official price and the player's own, empty for the official one.
 */
function Spending({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [currency, choose] = useSetting(live, settings, 'spending_currency', run)
  const [open, setOpen] = useState(false)
  const id = useId()
  return (
    <>
      <Row name="Spending in" sentence="The currency for “Spent on Genshin” on the Ledger.">
        <span className="set-field">
          <select
            className="set-select"
            aria-label="Spending in"
            value={currency}
            onChange={(event) => choose(event.target.value as Currency)}
          >
            {CURRENCIES.map((each) => (
              <option key={each.value} value={each.value}>
                {each.label}
              </option>
            ))}
          </select>
          <button type="button" className="set-link" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
            Edit prices
          </button>
        </span>
      </Row>
      <Notice text={notice} />
      {open && <Prices key={currency} live={live} settings={settings} currency={currency} id={id} />}
    </>
  )
}

/**
 * The price list in one currency. A price saves when its field is left or Enter is pressed; an empty field goes back to
 * the official price. The saves go out one after another, each from the corrections the last one left.
 */
function Prices({ live, settings, currency, id }: { live: Live; settings: SettingsModel; currency: Currency; id: string }) {
  const { notice, run } = useAction(live)
  const { data, error } = useModel<Ledger>('/api/ledger', 0)
  const corrections = useRef(settings.spending_prices)
  const saving = useRef<Promise<void>>(Promise.resolve())
  const name = CURRENCIES.find((each) => each.value === currency)?.label ?? currency
  if (data === null || data.prices === undefined) {
    return (
      <div className="set-more" id={id}>
        {error ?? 'Reading the price list…'}
      </div>
    )
  }
  const save = (pack: string, text: string): boolean => {
    const typed = text.trim().replace(',', '.')
    const value = typed === '' ? null : Number(typed)
    if (value !== null && !(Number.isFinite(value) && value >= 0 && value <= MAX_PRICE)) {
      void run(async () => `A price is a number from 0 to ${MAX_PRICE.toLocaleString('en-US')}.`)
      return false
    }
    const mine = { ...(corrections.current[currency] ?? {}) }
    if ((mine[pack] ?? null) === value) return true
    if (value === null) delete mine[pack]
    else mine[pack] = value
    const next = { ...corrections.current, [currency]: mine }
    corrections.current = next
    saving.current = saving.current.then(() =>
      run(async () => {
        await live.saveSettings({ spending_prices: next })
        return null
      }),
    )
    return true
  }
  return (
    <div className="set-more" id={id} role="group" aria-label="Prices">
      <p>Each pack's price in {name}. Leave a price empty to use the official one.</p>
      <table className="set-prices">
        <thead>
          <tr>
            <th scope="col">Pack</th>
            <th scope="col">Official</th>
            <th scope="col">Yours</th>
          </tr>
        </thead>
        <tbody>
          {data.prices.packs.map((pack) => (
            <PriceRow
              key={pack.id}
              label={pack.label}
              official={data.prices.defaults[currency][pack.id] ?? 0}
              saved={settings.spending_prices[currency]?.[pack.id] ?? null}
              save={(text) => save(pack.id, text)}
            />
          ))}
        </tbody>
      </table>
      <Notice text={notice} />
    </div>
  )
}

function PriceRow({
  label, official, saved, save,
}: { label: string; official: number; saved: number | null; save: (text: string) => boolean }) {
  const [text, setText] = useState(saved === null ? '' : String(saved))
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{official.toFixed(2)}</td>
      <td>
        <input
          className="set-price"
          type="number"
          inputMode="decimal"
          min={0}
          max={MAX_PRICE}
          step={0.01}
          aria-label={label}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onBlur={() => save(text)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') event.currentTarget.blur()
          }}
        />
      </td>
    </tr>
  )
}

/**
 * The page model without its cards: `unlisted` asks for the rows the list does not hold instead, which are few.
 * Settings needs only the installed list, which the model carries.
 */
const LIST_ONLY = '/api/achievements?unlisted=true'

/**
 * Spec 11.1, 9.3 and 12.5: the region Daily commissions opens on and when fishing spots come back, each saved at once,
 * and the installed achievement list and its updates.
 */
function Achievements({ live, settings }: { live: Live; settings: SettingsModel }) {
  const { notice, run } = useAction(live)
  const [region, chooseRegion] = useSetting(live, settings, 'commission_region', run)
  const [fishing, chooseFishing] = useSetting(live, settings, 'fishing_rule', run)
  const { data, error, read } = useModel<AchievementsModel>(LIST_ONLY, live.version)
  const updates = useListUpdates()
  return (
    <Group name="Achievements and commissions" sign="cup">
      <Row
        name="Commission region"
        sentence="Daily commissions opens on this region. HoYoLAB does not say which region you chose in the game."
      >
        <select
          className="set-select"
          aria-label="Commission region"
          value={region}
          onChange={(event) => chooseRegion(event.target.value as Region)}
        >
          {Object.entries(REGION_NAMES).map(([key, name]) => (
            <option key={key} value={key}>
              {name}
            </option>
          ))}
        </select>
      </Row>
      <Row name="Fishing spots come back" sentence="Guides disagree. Pick what you see in the game; every fishing spot follows it.">
        <Segmented label="Fishing spots come back" options={FISHING_RULES} value={fishing} onChange={chooseFishing} />
      </Row>
      <Notice text={notice} />
      <div className="set-row set-held">
        <b>Achievement list</b>
        {data === null ? (
          <p className="set-aside">{error ?? 'Reading the achievement list…'}</p>
        ) : (
          <ListPanel list={data.list} read={read} updates={updates} withCheck />
        )}
      </div>
    </Group>
  )
}

type BackupCopy = { name: string; made_at: string; kind: 'daily' | 'manual' | 'auto' | 'before' }
type RestoreSource = { label: string; replaces: string; send: () => Promise<{ restarting: boolean }> }
const SHOWN_BACKUPS = 20
const WITH_SETTINGS = 'achievements, notes, timers, wishes, codes and settings'
const WITHOUT_SETTINGS = 'achievements, notes, timers, wishes and codes'

const BACKUP_KINDS = { daily: 'Daily', manual: 'Made by you', auto: 'Before a change', before: 'Before a restore' }
/**
 * The largest file taken as a backup, the core's own limit (backups.py, MAX_BACKUP_BYTES). A real one is far smaller: a
 * player's database of about 18 MB, which a backup deflates (audit L27). The file goes as it is and is never held whole.
 */
const BACKUP_FILE_LIMIT = 200 * 1024 * 1024

/**
 * Your data: the backups (the copy in the backups folder, a file to keep elsewhere, and a restore from either), the
 * folder that holds everything, and importing progress from a Paimon.moe export. The folder's path is not printed.
 */
function YourData({ live, status }: { live: Live; status: Status }) {
  const { notice, run } = useAction(live)
  const [made, setMade] = useState(0) // the copies are read again after each backup
  const { data: copies } = useModel<BackupCopy[]>('/api/backups', live.version + made)
  const [listing, setListing] = useState(false)
  const [asking, setAsking] = useState<RestoreSource | null>(null)
  const [busy, setBusy] = useState(false)
  const chooser = useRef<HTMLInputElement>(null)
  const id = useId()
  const backup = () =>
    run(async () => {
      const answer = await api.post<{ file: string }>('/api/backup')
      setMade((count) => count + 1)
      return `Saved ${answer.file} in the backups folder.`
    })
  const open = () =>
    run(async () => {
      await api.post('/api/data/open')
      return null
    })
  const exportFile = () =>
    run(async () => {
      await api.post('/api/backup/export')
      return 'Choose where to save the backup in the Guild Board window.'
    })
  const pickFile = (file: File | undefined) => {
    if (chooser.current) chooser.current.value = ''
    if (file === undefined) return
    if (file.size > BACKUP_FILE_LIMIT) {
      void run(async () => 'This file is too large to be a Guild Board backup.')
      return
    }
    setAsking({
      label: localDay(new Date(file.lastModified).toISOString()),
      replaces: WITH_SETTINGS,
      send: () => restoreFile(file),
    })
  }
  const restore = () => {
    const source = asking
    if (source === null || busy) return
    setBusy(true)
    setAsking(null)
    void run(async () => {
      try {
        const answer = await source.send()
        return answer.restarting ? 'Guild Board is restarting.' : 'Restart Guild Board to finish the restore.'
      } finally {
        setBusy(false)
      }
    })
  }
  return (
    <Group name="Your data" sign="chest">
      <Row name="Backups" sentence={backupLine(copies, localTime)}>
        <button type="button" className="gb-btn quiet" onClick={() => void backup()}>
          Back up now
        </button>
        <button type="button" className="gb-btn quiet" onClick={() => void open()}>
          Open folder
        </button>
      </Row>
      <p className="set-links">
        <button type="button" className="set-link" onClick={() => void exportFile()}>
          Export backup…
        </button>
        <span aria-hidden="true">·</span>
        <button
          type="button"
          className="set-link"
          aria-expanded={listing}
          aria-controls={id}
          onClick={() => setListing(!listing)}
        >
          Restore a backup
        </button>
      </p>
      {/* A file passed on shows all of it to whoever opens it (audit DB-10); tests/test_backups.py keeps the login out. */}
      <p className="set-aside" data-testid="backup-holds">
        A backup file holds all your data, including your ledger with what you spent on top-ups, your wishes, play sessions and
        settings, what HoYoLAB sent, and your online account's ID, but never your HoYoLAB login or your online session.
      </p>
      {listing && (
        <div id={id} className="set-more">
          <ul className="set-backups" aria-label="Backups you can restore">
            {(copies ?? []).slice(0, SHOWN_BACKUPS).map((copy) => (
              <li key={copy.name} className="set-line">
                <span>
                  {localTime(copy.made_at)}, {BACKUP_KINDS[copy.kind]}
                </span>
                <button
                  type="button"
                  className="gb-btn quiet"
                  onClick={() =>
                    setAsking({
                      label: localDay(copy.made_at),
                      replaces: WITHOUT_SETTINGS,
                      send: () => api.post<{ restarting: boolean }>('/api/backup/restore', { name: copy.name }),
                    })
                  }
                >
                  Restore
                </button>
              </li>
            ))}
          </ul>
          <button type="button" className="gb-btn quiet" onClick={() => chooser.current?.click()}>
            Choose a file…
          </button>
          <input
            ref={chooser}
            type="file"
            accept=".gbbackup"
            hidden
            aria-label="Backup file"
            onChange={(event) => pickFile(event.target.files?.[0])}
          />
        </div>
      )}
      {asking !== null && (
        <div className="set-more" role="alertdialog" aria-label="Confirm the restore">
          <p>
            Restore the backup of {asking.label}? Your {asking.replaces} are replaced by the backup's. What you have now is saved as a backup first. Guild Board
            restarts. If this is a new PC, log in to HoYoLAB again afterwards.
          </p>
          <div className="set-actions">
            <button type="button" className="gb-btn" onClick={restore}>
              Restore
            </button>
            <button type="button" className="gb-btn quiet" onClick={() => setAsking(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
      {status.restore_unfinished && (
        <p className="set-notice" role="status">
          The restore could not finish. It will be tried again at the next start.
        </p>
      )}
      <Notice text={notice} />
      <div className="set-row set-held">
        <b>Import progress</b>
        <ImportPanel />
      </div>
      <div className="set-row set-held">
        <b>Old TUSKFiZZY data</b>
        <TuskfizzyImportPanel />
      </div>
    </Group>
  )
}

/**
 * A dialog that the footer or the About group opens. Close, Escape and a click on the dim around it all close the dialog
 * itself; its close event then takes it away, and the focus goes back to where it was.
 */
function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const id = useId()
  useEffect(() => {
    const element = dialog.current
    const before = document.activeElement
    element?.showModal()
    return () => {
      element?.close()
      if (before instanceof HTMLElement && before.isConnected) before.focus({ preventScroll: true })
    }
  }, [])
  const close = () => dialog.current?.close()
  return (
    <dialog
      ref={dialog}
      className="set-dialog"
      aria-labelledby={id}
      onClose={onClose}
      onClick={(event) => {
        if (event.target === dialog.current) close() // a click on the dim around it
      }}
    >
      <div className="set-dialog-body">
        <h2 id={id}>{title}</h2>
        {children}
        <div className="set-actions">
          <button type="button" className="gb-btn" onClick={close}>
            Close
          </button>
        </div>
      </div>
    </dialog>
  )
}

/** What Guild Board never does and the risk, in a dialog that the footer opens. */
function RulesDialog({ onClose }: { onClose: () => void }) {
  return (
    <Dialog title="What Guild Board never does" onClose={onClose}>
      <ul className="set-rules">
        {[...HANDS_OFF, LEDGER_HANDS_OFF, GALLERY_HANDS_OFF].map((rule) => (
          <li key={rule}>{rule}</li>
        ))}
      </ul>
      <h3>The risk</h3>
      {RISK.map((paragraph) => (
        <p key={paragraph}>{paragraph}</p>
      ))}
    </Dialog>
  )
}

/** The licences of the software inside Guild Board, as the core reads them from THIRD-PARTY-NOTICES.txt. */
function NoticesDialog({ onClose }: { onClose: () => void }) {
  const [shown, setShown] = useState<{ text: string } | { error: string } | null>(null)
  useEffect(() => {
    let current = true
    api
      .get<{ text: string }>('/api/notices')
      .then((answer) => current && setShown({ text: answer.text }))
      .catch((error: unknown) => current && setShown({ error: messageOf(error) }))
    return () => {
      current = false
    }
  }, [])
  return (
    <Dialog title="Open-source licences" onClose={onClose}>
      <p>Guild Board is MIT licensed. These are the licences of the software inside it.</p>
      {shown === null && <p className="set-aside">Reading the licences…</p>}
      {shown !== null && 'error' in shown && <p className="set-notice" role="alert">{shown.error}</p>}
      {shown !== null && 'text' in shown && (
        <pre className="set-notices" tabIndex={0} aria-label="Licences of the software inside Guild Board">
          {shown.text}
        </pre>
      )}
    </Dialog>
  )
}

/** The version, what Guild Board is not, and where its licence, source and privacy notice are. */
function About({ status }: { status: Status }) {
  const [open, setOpen] = useState(false)
  return (
    <Group name="About" sign="book">
      <Row name="Version" sentence={status.version} />
      <Row name="MIT licence" sentence="Guild Board is free software; the software inside it has licences of its own.">
        <button type="button" className="gb-btn quiet" onClick={() => setOpen(true)}>
          Open-source licences
        </button>
      </Row>
      <Row name="Source code" sentence="The code is public, so you can read what Guild Board does.">
        <a className="set-link" href={SOURCE_URL} target="_blank" rel="noopener noreferrer">
          Open on GitHub
        </a>
      </Row>
      <Row name="Privacy notice" sentence="What Guild Board reads, keeps and sends.">
        <a className="set-link" href={PRIVACY_URL} target="_blank" rel="noopener noreferrer">
          Read the notice
        </a>
      </Row>
      <p className="set-aside set-fan">{FAN_MADE}</p>
      {open && <NoticesDialog onClose={() => setOpen(false)} />}
    </Group>
  )
}

/** The notes of this version, in the list of changes that the update notice opens too. */
function WhatsNew({ live, status, onClose }: { live: Live; status: Status; onClose: () => void }) {
  const { data } = useModel<Changes[]>('/api/whats-new', live.version)
  if (data === null) return null
  const entry = data.find((each) => each.version === status.version) ?? data[0] ?? { version: status.version, items: [] }
  return <ChangesModal mode="after" version={entry.version} released={null} items={entry.items} onClose={onClose} onInstall={() => undefined} />
}

function Footer({ live, status }: { live: Live; status: Status }) {
  const [open, setOpen] = useState<'new' | 'rules' | null>(null)
  return (
    <footer className="set-foot" data-testid="settings-foot">
      Guild Board {status.version}
      <span aria-hidden="true">·</span>
      <button type="button" className="set-link" onClick={() => setOpen('new')}>
        What's new
      </button>
      <span aria-hidden="true">·</span>
      <button type="button" className="set-link" onClick={() => setOpen('rules')}>
        What Guild Board never does
      </button>
      {open === 'new' && <WhatsNew live={live} status={status} onClose={() => setOpen(null)} />}
      {open === 'rules' && <RulesDialog onClose={() => setOpen(null)} />}
    </footer>
  )
}

export function Settings({ live }: { live: Live }) {
  const { status, settings } = live
  return (
    <Frame page="settings" live={live}>
      {status === null || settings === null ? (
        <div className="gb-panel gb-empty">Reading the settings…</div>
      ) : (
        <div className="set">
          <Account live={live} status={status} />
          <Online live={live} status={status} settings={settings} />
          <Sync live={live} settings={settings} />
          <Group name="Guild Board" sign="star">
            <StartWithWindows live={live} status={status} settings={settings} />
            <Notifications live={live} settings={settings} />
            <PageSizes live={live} status={status} settings={settings} />
            <PictureQualityRow live={live} settings={settings} />
            <Overlay live={live} settings={settings} />
          </Group>
          <Group name="Extras" sign="cells">
            <Gallery live={live} settings={settings} />
            <Spending live={live} settings={settings} />
          </Group>
          <Achievements live={live} settings={settings} />
          <YourData live={live} status={status} />
          <About status={status} />
          <Footer live={live} status={status} />
        </div>
      )}
    </Frame>
  )
}
