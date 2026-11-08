# TUSKFiZZY legacy data migration format

This note records the legacy browser-storage keys and the portable export/import bridge. The old app's export button is on branch `migration/legacy-export` of `TUSK_Check-in`; the new app's importer is on branch `migration/tuskfizzy-integration` of `TUSK-Login`. Neither change is on either repository's `main` branch.

## Legacy localStorage keys

| Key | Contents |
| --- | --- |
| `tuskfizzy_accounts_v1` | User-created checklist profiles (name, portrait data URL, optional UID/server and linked Genshin account fields) |
| `tuskfizzy_daily_multi_v1` | Daily records keyed by local calendar date, then profile ID; each profile value is `[login, commission, resin]` booleans. May also contain `_resinLast` transition data. |
| `tuskfizzy_genshin_accounts_v1` | Genshin account profiles (id, name, UID, server, cached Daily Note, Enka profile and Battle Chronicle data) |
| `tuskfizzy_selected_genshin_v1` | Selected Genshin profile ID |
| `tuskfizzy_daily_v1` | Legacy single-profile record key; old built-in Main/Alternate profiles are removed by newer app versions. |

## Import rules

1. Parse and validate the export before writing anything. Reject malformed JSON, duplicate profile IDs, invalid date keys, invalid task arrays, and UIDs that are not 9 or 10 digits.
2. Keep user-created profile names, IDs, portraits, Genshin UID/server associations, selected Genshin profile, and every dated checklist record.
3. Preserve the three task meanings in order: Login, Commission, Resin.
4. Preserve the resin transition marker only as history metadata. Do not auto-complete Resin merely because the first imported Daily Note already reports fewer than 50 resin; the old app only auto-completed on a same-day observed transition from 50 or more to below 50.
5. Preserve Commission auto-completion only when HoYoLAB reports the extra commission reward as claimed.
6. Treat cached Daily Note, Enka, and Battle Chronicle responses as stale display cache; refresh them after the user signs into the matching account.
7. Never import or export HoYoLAB cookies, ltoken/ltuid values, browser session partitions, wish-history URLs, or any other authentication material. The user signs in again in the new app, per account.
8. Keep the source data unchanged until the new database confirms a successful import. Import must be repeatable without duplicating profiles or day records.

## Proposed portable export object

The export should contain only the values of the four keys above, wrapped in a versioned object:

```json
{
  "format": "tuskfizzy-local-data",
  "version": 1,
  "exportedAt": "ISO-8601 timestamp",
  "accounts": [],
  "dailyRecords": {},
  "genshinAccounts": [],
  "selectedGenshinId": ""
}
```

The export must be generated locally by the old app. Do not ask the user to paste browser cookies or authentication values into a file or chat.

## Acceptance checks

- [x] Export from the legacy desktop app includes the documented profiles, selected Genshin account and daily records, and excludes secrets.
- [x] Import preview reports profile and day counts before the user confirms.
- [x] Re-import is idempotent.
- [ ] Portrait data URLs are size-limited and decoded safely.
- [x] Account-to-Genshin associations are preserved as profile references.
- [ ] Resin and Commission automatic completion rules match the legacy app.
- [x] Failed import leaves the destination database unchanged (validated before the transaction; transactional write).


## Implementation status on the TUSKFiZZY migration branch

The migration branch now includes SQLite tables for TUSKFiZZY checklist profiles, Genshin profile references, and dated Login/Commission/Resin records. The authenticated local API route `POST /api/tuskfizzy/import` validates the versioned export shape before writing it in one transaction; repeated imports update matching IDs rather than duplicating them. The database migration backs up an existing database before upgrading, following the normal Store migration path.

Implemented on the migration branches: a legacy-app JSON export button; a Settings importer with file selection, local preview, and explicit confirmation; a validated local API route; SQLite profile/history tables; idempotent transactional import; and store/API tests. The imported records are stored in dedicated TUSKFiZZY tables and are now wired into a Daily Tracker page with add/edit/remove profiles, picture upload, Login/Commission/Resin checkboxes, date navigation and a monthly overview. Past month days are selectable; today and future days are neutral and not selectable in the overview. The current Guild Board connection supplies fresh Daily Note data for the single active HoYoLAB login and applies it only to the matching linked TUSKFiZZY profile. Independent per-Genshin-account HoYoLAB sessions, Enka profile and Battle Chronicle integration are still outstanding. No cookies or HoYoLAB credentials are exported or accepted.


## Still to port

- [x] Resin auto-completion on a same-day observed transition from at least 50 to below 50; the first observation already below 50 does not auto-complete, and a completed check remains checked after refill. This runs only for fresh notes matching the linked active HoYoLAB UID.
- [x] Commission auto-completion only after fresh HoYoLAB Daily Note data reports the extra reward claimed; stale notes cannot mark today's checklist.
- Live HoYoLAB Daily Note, Enka profile and Battle Chronicle connections for each selected Genshin profile.
- Achievements and wish-history/pity feature integration.
- Windows packaging and end-to-end verification on a real Windows machine.
