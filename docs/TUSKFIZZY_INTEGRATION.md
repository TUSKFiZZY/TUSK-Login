# TUSKFiZZY integration plan for Guild Board

## Repository and licence

This repository contains Guild Board by QL1STY and is the working base for the TUSKFiZZY integration. The upstream project is MIT-licensed; retain its `LICENSE`, notices and attribution in all redistributed copies. Attribution: **Based on Guild Board by QL1STY**.

The repository owner has granted permission to reuse the project under its MIT licence. This permission does not cover HoYoverse/Genshin Impact artwork, logos, or other third-party assets. Do not imply affiliation with or endorsement by HoYoverse or COGNOSPHERE.

## Scope

Build an offline-first, Windows-focused TUSKFiZZY companion on Guild Board's Python core, SQLite storage, local API, React interface and Electron shell. Keep this integration on a dedicated branch until it has been built and tested. Do not replace the default branch, published site, or existing installer as part of this work.

## Product identity and interface

- Apply TUSKFiZZY naming and purple visual identity while retaining Guild Board's MIT attribution and third-party notices.
- Rework navigation and dashboard incrementally; preserve accessible controls, stable input focus/caret behaviour, and responsive layout.
- Keep optional cloud, Discord, and Supabase features disabled unless explicitly required.

## Existing TUSKFiZZY capabilities to port

- Multiple Genshin accounts with add, edit, and delete; no pre-created Main/Alternate accounts.
- Daily Login, Commission, and Resin checklist, including the established automatic resin and commission completion rules.
- Monthly history with past-day status, and future dates shown as unavailable.
- HoYoLAB login, daily check-in, and Daily Note (resin, commissions, expeditions).
- Enka character profiles (support 9- and 10-digit UIDs), Battle Chronicle, wish-history import, pity, and achievements.
- Per-account profile pictures and isolated account sessions.
- Reliable navigation, refresh actions, buttons, and text inputs.

## Data migration and account security

- Do not copy, print, commit, or expose HoYoLAB cookies, auth tokens, session data, or credentials.
- Keep local databases, backups, environment files, cookies, tokens, logs containing private data, and user-specific exports out of Git.
- Prefer Guild Board's local encrypted token storage. If secure session migration cannot be verified, ask the user to sign in again; preserve account profile metadata separately.
- Before importing existing data, create a recoverable local backup and verify account separation and imported records.

## Implementation sequence

1. Confirm the Guild Board baseline and its build/test instructions.
2. Apply TUSKFiZZY branding without removing upstream licence or notices.
3. Add a documented data model and migration path for existing TUSKFiZZY account/checklist data.
4. Port one feature at a time, starting with accounts, daily checklist, and Daily Note.
5. Port profile, achievements, wish history/pity, and remaining integrations.
6. Test builds, data migration, account isolation, UI interactions, and Windows packaging before any release.

## Acceptance checklist

- [x] TUSKFiZZY identity and purple theme applied consistently.
- [x] Guild Board MIT licence, attribution, and third-party notices retained.
- [x] No HoYoverse artwork added; profile credentials use per-account encrypted local storage and wish-history keys are memory-only.
- [x] Existing checklist and Genshin profile data can be exported/imported transactionally; credentials are re-entered separately per profile.
- [ ] All listed TUSKFiZZY features are implemented and individually tested (feature code is present; end-to-end verification is pending).
- [ ] UI navigation, typing, focus, refresh, and window resizing work reliably (build/test run pending).
- [ ] Windows build and installer tested before release.
- [ ] Default branch, live site, and existing installer remain untouched until explicit approval.
