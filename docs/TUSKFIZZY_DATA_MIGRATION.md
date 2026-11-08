# TUSKFiZZY legacy data migration format

This note records the exact browser-storage keys used by the legacy TUSKFiZZY Daily Tracker. It is a design contract for the import/export bridge; it does not claim that the importer UI or database write path is implemented yet.

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

- [ ] Export from the legacy desktop app includes all four keys and excludes secrets.
- [ ] Import preview reports profile and day counts before the user confirms.
- [ ] Re-import is idempotent.
- [ ] Portrait data URLs are size-limited and decoded safely.
- [ ] Account-to-Genshin associations remain isolated.
- [ ] Resin and Commission automatic completion rules match the legacy app.
- [ ] Failed import leaves the destination database unchanged.


## Implementation status on the TUSKFiZZY migration branch

The migration branch now includes SQLite tables for TUSKFiZZY checklist profiles, Genshin profile references, and dated Login/Commission/Resin records. The authenticated local API route `POST /api/tuskfizzy/import` validates the versioned export shape before writing it in one transaction; repeated imports update matching IDs rather than duplicating them. The database migration backs up an existing database before upgrading, following the normal Store migration path.

This is the storage/API foundation only. The old app's export button, the new Settings import screen and preview/confirmation flow, the visible multi-account checklist, and the live HoYoLAB/Enka feature integration are not implemented yet. No cookies or HoYoLAB credentials are accepted by this import route.
