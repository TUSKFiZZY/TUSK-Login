import { useState } from 'react'
import '../styles/tuskfizzy.css'
import { api, messageOf } from '../api.ts'
import { Frame } from '../frame/Frame.tsx'
import { type Live, useModel } from '../live.ts'

type Account = { id: string; name: string; portrait: string; genshinId: string | null; createdAt: string }
type GenshinAccount = { id: string; name: string; uid: string; server: string; createdAt?: string }
type DailyNote = { profileName: string; resin: number | null; resinMax: number | null; resinReadAt: string | null; commissionDone: number | null; commissionTotal: number | null; bonusClaimed: boolean; expeditionsDone: number | null; expeditionsTotal: number | null; readAt: string }
type Tracker = { accounts: Account[]; genshinAccounts: GenshinAccount[]; daily: Record<string, boolean[]>; dailyNote?: DailyNote | null }
type MonthTracker = { records: Record<string, Record<string, boolean[]>> }
const TASKS = [{ key: 'login', label: 'Login' }, { key: 'commission', label: 'Commission' }, { key: 'resin', label: 'Resin' }] as const

function todayKey(): string {
  const now = new Date()
  return [now.getFullYear(), String(now.getMonth() + 1).padStart(2, '0'), String(now.getDate()).padStart(2, '0')].join('-')
}

async function pictureData(file: File): Promise<string> {
  if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type) || file.size > 700_000) {
    throw new Error('Choose a PNG, JPEG, WebP or GIF picture smaller than 700 KB.')
  }
  return await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('The picture could not be read.'))
    reader.onerror = () => reject(new Error('The picture could not be read.'))
    reader.readAsDataURL(file)
  })
}

export function Tuskfizzy({ live }: { live: Live }) {
  const [day, setDay] = useState(todayKey)
  const [revision, setRevision] = useState(0)
  const [showMonth, setShowMonth] = useState(false)
  const [monthOffset, setMonthOffset] = useState(0)
  const { data, error } = useModel<Tracker>(`/api/tuskfizzy/tracker?day=${day}`, live.version + revision)
  const monthDate = new Date(`${day}T12:00:00`)
  monthDate.setDate(1)
  monthDate.setMonth(monthDate.getMonth() + monthOffset)
  const monthKey = `${monthDate.getFullYear()}-${String(monthDate.getMonth() + 1).padStart(2, '0')}`
  const { data: monthData } = useModel<MonthTracker>(`/api/tuskfizzy/month?month=${monthKey}`, live.version + revision)
  const [name, setName] = useState('')
  const [portrait, setPortrait] = useState('')
  const [genshinId, setGenshinId] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [saving, setSaving] = useState<string | null>(null)
  const [problem, setProblem] = useState<string | null>(null)
  const [genshinName, setGenshinName] = useState('')
  const [genshinUid, setGenshinUid] = useState('')
  const [genshinServer, setGenshinServer] = useState('os_asia')
  const [editingGenshin, setEditingGenshin] = useState<string | null>(null)

  const edit = (account: Account) => {
    setEditing(account.id)
    setName(account.name)
    setPortrait(account.portrait)
    setGenshinId(account.genshinId ?? '')
    setProblem(null)
  }
  const clearForm = () => {
    setEditing(null)
    setName('')
    setPortrait('')
    setGenshinId('')
    setProblem(null)
  }
  const save = async () => {
    if (!name.trim() || saving !== null) return
    setSaving('profile')
    setProblem(null)
    try {
      const body = { name: name.trim(), portrait, genshinId: genshinId || null }
      if (editing) await api.put(`/api/tuskfizzy/accounts/${encodeURIComponent(editing)}`, body)
      else await api.post('/api/tuskfizzy/accounts', body)
      clearForm()
      setRevision(value => value + 1)
    } catch (failure) {
      setProblem(messageOf(failure))
    } finally {
      setSaving(null)
    }
  }
  const remove = async (account: Account) => {
    if (saving !== null || !window.confirm(`Remove ${account.name} and its saved checklist history?`)) return
    setSaving(account.id)
    setProblem(null)
    try {
      await api.delete(`/api/tuskfizzy/accounts/${encodeURIComponent(account.id)}`)
      if (editing === account.id) clearForm()
      setRevision(value => value + 1)
    } catch (failure) {
      setProblem(messageOf(failure))
    } finally {
      setSaving(null)
    }
  }
  const editGenshin = (account: GenshinAccount) => {
    setEditingGenshin(account.id)
    setGenshinName(account.name)
    setGenshinUid(account.uid)
    setGenshinServer(account.server || 'os_asia')
    setProblem(null)
  }
  const clearGenshin = () => {
    setEditingGenshin(null)
    setGenshinName('')
    setGenshinUid('')
    setGenshinServer('os_asia')
  }
  const saveGenshin = async () => {
    if (!genshinName.trim() || !/^[0-9]{9,10}$/.test(genshinUid) || saving !== null) return
    setSaving('genshin')
    setProblem(null)
    try {
      const body = { name: genshinName.trim(), uid: genshinUid, server: genshinServer }
      if (editingGenshin) await api.put(`/api/tuskfizzy/genshin-accounts/${encodeURIComponent(editingGenshin)}`, body)
      else await api.post('/api/tuskfizzy/genshin-accounts', body)
      clearGenshin()
      setRevision(value => value + 1)
    } catch (failure) {
      setProblem(messageOf(failure))
    } finally {
      setSaving(null)
    }
  }
  const removeGenshin = async (account: GenshinAccount) => {
    if (saving !== null || !window.confirm(`Remove Genshin profile ${account.name}? Checklist profiles linked to it will be unlinked.`)) return
    setSaving(account.id)
    setProblem(null)
    try {
      await api.delete(`/api/tuskfizzy/genshin-accounts/${encodeURIComponent(account.id)}`)
      if (editingGenshin === account.id) clearGenshin()
      setRevision(value => value + 1)
    } catch (failure) {
      setProblem(messageOf(failure))
    } finally {
      setSaving(null)
    }
  }
  const check = async (account: Account, task: typeof TASKS[number]['key'], checked: boolean) => {
    const key = `${account.id}:${task}`
    if (saving !== null) return
    setSaving(key)
    setProblem(null)
    try {
      await api.put('/api/tuskfizzy/daily', { day, accountId: account.id, task, checked })
      setRevision(value => value + 1)
    } catch (failure) {
      setProblem(messageOf(failure))
    } finally {
      setSaving(null)
    }
  }

  return (
    <Frame page="tuskfizzy" live={live} wide>
      <section className="tf-tracker">
        <div className="tf-date">
          <button className="gb-btn quiet" type="button" onClick={() => {
            const next = new Date(`${day}T12:00:00`); next.setDate(next.getDate() - 1)
            setDay([next.getFullYear(), String(next.getMonth()+1).padStart(2,'0'), String(next.getDate()).padStart(2,'0')].join('-'))
          }}>Previous day</button>
          <label>
            <span className="sr-only">Selected date</span>
            <input type="date" value={day} max={todayKey()} onChange={event => { if (event.target.value) { setDay(event.target.value); setMonthOffset(0) } }} />
          </label>
          <button className="gb-btn quiet" type="button" disabled={day >= todayKey()} onClick={() => {
            const next = new Date(`${day}T12:00:00`); next.setDate(next.getDate() + 1)
            setDay([next.getFullYear(), String(next.getMonth()+1).padStart(2,'0'), String(next.getDate()).padStart(2,'0')].join('-'))
          }}>Next day</button>
          <button className="gb-btn quiet" type="button" onClick={() => setDay(todayKey())}>Today</button>
        </div>
        <p className="tf-caption">Daily checklist · {day}</p>
        {data?.dailyNote && (
          <section className="gb-panel tf-daily-note">
            <div className="tf-note-heading"><div><h2>Daily Note</h2><span>{data.dailyNote.profileName} · HoYoLAB</span></div><span className="tf-note-updated">Updated {new Date(data.dailyNote.readAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span></div>
            <div className="tf-note-grid">
              <div><span>Original Resin</span><b>{data.dailyNote.resin ?? '—'} / {data.dailyNote.resinMax ?? '—'}</b></div>
              <div><span>Daily Commissions</span><b>{data.dailyNote.commissionDone ?? '—'} / {data.dailyNote.commissionTotal ?? '—'}</b></div>
              <div><span>Expeditions</span><b>{data.dailyNote.expeditionsDone ?? '—'} / {data.dailyNote.expeditionsTotal ?? '—'}</b></div>
              <div><span>Bonus Reward</span><b>{data.dailyNote.bonusClaimed ? 'Claimed' : 'Not claimed'}</b></div>
            </div>
          </section>
        )}
        <div className="tf-month-actions">
          <button className="gb-btn quiet" type="button" onClick={() => setShowMonth(value => !value)}>{showMonth ? 'Day view' : 'Month overview'}</button>
        </div>
        {showMonth && (
          <section className="gb-panel tf-month">
            <div className="tf-month-head">
              <button className="gb-btn quiet" type="button" aria-label="Previous month" onClick={() => setMonthOffset(value => value - 1)}>←</button>
              <h2>{monthDate.toLocaleDateString(undefined, { month: 'long', year: 'numeric' })}</h2>
              <button className="gb-btn quiet" type="button" aria-label="Next month" disabled={monthKey >= todayKey().slice(0, 7)} onClick={() => setMonthOffset(value => value + 1)}>→</button>
            </div>
            <div className="tf-month-grid">
              {['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(label => <span className="tf-weekday" key={label}>{label}</span>)}
              {Array.from({ length: new Date(monthDate.getFullYear(), monthDate.getMonth(), 1).getDay() }, (_, index) => <span className="tf-day blank" key={`blank-${index}`} />)}
              {Array.from({ length: new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0).getDate() }, (_, index) => {
                const number = index + 1
                const dateKey = `${monthKey}-${String(number).padStart(2, '0')}`
                const past = dateKey < todayKey()
                const saved = monthData?.records[dateKey] ?? {}
                const complete = Boolean(data?.accounts.length) && data!.accounts.every(account => saved[account.id]?.length === 3 && saved[account.id].every(Boolean))
                return <button type="button" key={dateKey} disabled={!past} className={`tf-day ${past ? complete ? 'complete' : 'incomplete' : 'future'}`} onClick={() => { setDay(dateKey); setMonthOffset(0); setShowMonth(false) }}>
                  <b>{number}</b>{past && <span aria-label={complete ? 'Completed' : 'Incomplete'}>{complete ? '✓' : '×'}</span>}
                </button>
              })}
            </div>
            <p className="tf-month-legend">Past days: green for all tasks completed, red for incomplete. Today and future days are not selectable here.</p>
          </section>
        )}
        {error && <p className="gb-banner" role="alert">{error}</p>}
        {problem && <p className="gb-banner" role="alert">{problem}</p>}
        {data === null ? <div className="gb-panel gb-empty">Loading your TUSKFiZZY profiles…</div> : (
          <>
            {data.accounts.length === 0 && <div className="gb-panel gb-empty">No checklist profiles yet. Add your first profile below.</div>}
            <div className="tf-account-grid">
              {data.accounts.map(account => {
                const flags = data.daily[account.id] ?? [false, false, false]
                const done = flags.filter(Boolean).length
                return (
                  <article className="gb-panel tf-account" key={account.id}>
                    <div className="tf-account-head">
                      {account.portrait ? <img className="tf-avatar" src={account.portrait} alt="" /> : <div className="tf-avatar tf-avatar-fallback">{account.name.slice(0,1).toUpperCase()}</div>}
                      <div className="tf-account-name"><h2>{account.name}</h2><span>{done}/3 completed</span></div>
                      <div className="tf-account-actions">
                        <button type="button" className="gb-link" onClick={() => edit(account)}>Edit</button>
                        <button type="button" className="gb-link" onClick={() => void remove(account)}>Remove</button>
                      </div>
                    </div>
                    <div className="tf-tasks">
                      {TASKS.map((task,index) => (
                        <label className={flags[index] ? 'tf-task checked' : 'tf-task'} key={task.key}>
                          <input type="checkbox" checked={Boolean(flags[index])} disabled={saving !== null || day > todayKey()} onChange={event => void check(account,task.key,event.target.checked)} />
                          <span>{task.label}</span>
                        </label>
                      ))}
                    </div>
                  </article>
                )
              })}
            </div>
            <section className="gb-panel tf-genshin">
              <h2>Genshin accounts</h2>
              {data.genshinAccounts.length === 0 ? <p className="tf-muted">No Genshin profiles added yet.</p> : (
                <div className="tf-genshin-list">
                  {data.genshinAccounts.map(profile => (
                    <div className="tf-genshin-row" key={profile.id}>
                      <div><b>{profile.name}</b><span>UID {profile.uid} · {profile.server || 'Server not set'}</span></div>
                      <button className="gb-link" type="button" onClick={() => editGenshin(profile)}>Edit</button>
                      <button className="gb-link" type="button" onClick={() => void removeGenshin(profile)}>Remove</button>
                    </div>
                  ))}
                </div>
              )}
              <h3>{editingGenshin ? 'Edit Genshin profile' : 'Add Genshin profile'}</h3>
              <label>Profile name<input value={genshinName} maxLength={80} onChange={event => setGenshinName(event.target.value)} placeholder="e.g. Asia account" /></label>
              <label>UID<input value={genshinUid} inputMode="numeric" maxLength={10} onChange={event => setGenshinUid(event.target.value.replace(/[^0-9]/g, '').slice(0,10))} placeholder="9 or 10 digit UID" /></label>
              <label>Server<select value={genshinServer} onChange={event => setGenshinServer(event.target.value)}>
                <option value="os_asia">Asia</option><option value="os_usa">America</option><option value="os_euro">Europe</option><option value="os_cht">TW, HK, MO</option>
              </select></label>
              <div className="tf-editor-actions">
                <button className="gb-btn" type="button" disabled={!genshinName.trim() || !/^[0-9]{9,10}$/.test(genshinUid) || saving !== null} onClick={() => void saveGenshin()}>{saving === 'genshin' ? 'Saving…' : editingGenshin ? 'Save Genshin profile' : 'Add Genshin profile'}</button>
                {editingGenshin && <button className="gb-btn quiet" type="button" disabled={saving !== null} onClick={clearGenshin}>Cancel</button>}
              </div>
            </section>
            <section className="gb-panel tf-editor">
              <h2>{editing ? 'Edit profile' : 'Add checklist profile'}</h2>
              <label>Profile name<input value={name} maxLength={80} onChange={event => setName(event.target.value)} placeholder="Enter a profile name" /></label>
              <label>Profile picture<input type="file" accept="image/png,image/jpeg,image/webp" onChange={event => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (file) void pictureData(file).then(setPortrait).catch(failure => setProblem(messageOf(failure)))
              }} /></label>
              {portrait && <div className="tf-picture-preview"><img src={portrait} alt="Profile picture preview" /><button className="gb-link" type="button" onClick={() => setPortrait('')}>Remove picture</button></div>}
              <label>Linked Genshin profile
                <select value={genshinId} onChange={event => setGenshinId(event.target.value)}>
                  <option value="">Not linked</option>
                  {data.genshinAccounts.map(profile => <option key={profile.id} value={profile.id}>{profile.name} · {profile.uid}</option>)}
                </select>
              </label>
              <div className="tf-editor-actions">
                <button className="gb-btn" type="button" disabled={!name.trim() || saving !== null} onClick={() => void save()}>{saving === 'profile' ? 'Saving…' : editing ? 'Save changes' : 'Add profile'}</button>
                {editing && <button className="gb-btn quiet" type="button" disabled={saving !== null} onClick={clearForm}>Cancel</button>}
              </div>
            </section>
          </>
        )}
      </section>
    </Frame>
  )
}
