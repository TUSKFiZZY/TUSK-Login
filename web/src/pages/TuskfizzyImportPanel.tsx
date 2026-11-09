import { useRef, useState } from 'react'
import { api, messageOf } from '../api.ts'

type LegacyExport = {
  format: 'tuskfizzy-local-data'
  version: 1
  accounts: Array<{ id: string; name: string; portrait?: string; genshinId?: string | null; createdAt?: string }>
  dailyRecords: Record<string, Record<string, boolean[] | Record<string, number>>>
  genshinAccounts: Array<{ id: string; name: string; uid: string; server?: string; createdAt?: string }>
  selectedGenshinId?: string
}

const MAX_FILE_BYTES = 10_000_000

function parseExport(value: unknown): LegacyExport {
  if (typeof value !== 'object' || value === null) throw new Error('This file is not a TUSKFiZZY export.')
  const data = value as Record<string, unknown>
  if (data.format !== 'tuskfizzy-local-data' || data.version !== 1 ||
      !Array.isArray(data.accounts) || !Array.isArray(data.genshinAccounts) ||
      typeof data.dailyRecords !== 'object' || data.dailyRecords === null || Array.isArray(data.dailyRecords)) {
    throw new Error('This file is not a supported TUSKFiZZY export (format version 1).')
  }
  return data as LegacyExport
}

/** A separate, explicit import for the old TUSKFiZZY daily tracker; it never accepts or sends login credentials. */
export function TuskfizzyImportPanel() {
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<LegacyExport | null>(null)
  const [fileName, setFileName] = useState('')
  const [problem, setProblem] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<string | null>(null)

  const choose = async (picked?: File) => {
    if (!picked) return
    setProblem(null)
    setResult(null)
    setFile(null)
    if (picked.size > MAX_FILE_BYTES) {
      setProblem('The export is larger than 10 MB.')
      return
    }
    try {
      const parsed = parseExport(JSON.parse(await picked.text()) as unknown)
      if (parsed.accounts.length > 100 || parsed.genshinAccounts.length > 100 ||
          Object.keys(parsed.dailyRecords).length > 5000) {
        throw new Error('This export contains more profiles or days than can be imported.')
      }
      setFile(parsed)
      setFileName(picked.name)
    } catch (error) {
      setProblem(messageOf(error))
    }
  }

  const importNow = async () => {
    if (!file || busy) return
    setBusy(true)
    setProblem(null)
    try {
      // Rebuild the payload from the documented, non-secret fields; never forward arbitrary JSON keys.
      const payload = {
        format: file.format,
        version: file.version,
        accounts: file.accounts,
        dailyRecords: file.dailyRecords,
        genshinAccounts: file.genshinAccounts,
        selectedGenshinId: file.selectedGenshinId ?? '',
      }
      const answer = await api.post<{ imported: { accounts: number; genshinAccounts: number; dailyRecords: number } }>(
        '/api/tuskfizzy/import', payload,
      )
      setResult(`Imported ${answer.imported.accounts} checklist profiles, ${answer.imported.genshinAccounts} Genshin profiles and ${answer.imported.dailyRecords} daily records.`)
      setFile(null)
    } catch (error) {
      setProblem(messageOf(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="set-more" data-testid="tuskfizzy-import">
      <p>Bring your profiles and Login / Commission / Resin history from the older TUSKFiZZY app. Choose its JSON export to preview the contents before importing.</p>
      <p className="set-aside">HoYoLAB sign-in sessions and cookies are never imported. You will sign in again. Your existing database is backed up before its schema is upgraded.</p>
      <div className="set-actions">
        <button type="button" className="gb-btn quiet" onClick={() => input.current?.click()} disabled={busy}>Choose TUSKFiZZY export…</button>
        <input ref={input} type="file" accept=".json,application/json" hidden aria-label="TUSKFiZZY JSON export" onChange={event => { void choose(event.target.files?.[0]); event.target.value = '' }} />
      </div>
      {file && (
        <div className="set-more" role="group" aria-label="TUSKFiZZY import preview">
          <b>{fileName}</b>
          <p>{file.accounts.length} checklist profiles · {file.genshinAccounts.length} Genshin profiles · {Object.values(file.dailyRecords).reduce((sum, day) => sum + (day && typeof day === 'object' && !Array.isArray(day) ? Object.keys(day).filter(key => key !== '_resinLast').length : 0), 0)} daily records</p>
          <p>Import these records into this TUSKFiZZY database? Existing records with matching IDs and dates will be updated.</p>
          <div className="set-actions">
            <button type="button" className="gb-btn" onClick={() => void importNow()} disabled={busy}>{busy ? 'Importing…' : 'Import data'}</button>
            <button type="button" className="gb-btn quiet" onClick={() => setFile(null)} disabled={busy}>Cancel</button>
          </div>
        </div>
      )}
      {problem && <p className="ach-problem" role="alert">{problem}</p>}
      {result && <p className="set-notice" role="status">{result}</p>}
    </div>
  )
}
