<div align="center">

# ✦ TUSKFiZZY

**Your Genshin day on one board.**

*TUSKFiZZY is an independent integration based on Guild Board by QL1STY.*

Resin, commissions, achievements, wishes, primogems and your characters' plans, in one Windows app that keeps it all on your PC and never touches the game.

[![Latest release](https://img.shields.io/github/v/release/QL1STY/guild-board?style=for-the-badge&color=a6751f&labelColor=3b4255&label=latest)](https://github.com/QL1STY/guild-board/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/QL1STY/guild-board/total?style=for-the-badge&color=a6751f&labelColor=3b4255)](https://github.com/QL1STY/guild-board/releases)
[![MIT licence](https://img.shields.io/badge/licence-MIT-a6751f?style=for-the-badge&labelColor=3b4255)](LICENSE)
![Windows 10 and 11](https://img.shields.io/badge/Windows-10%20%7C%2011-a6751f?style=for-the-badge&labelColor=3b4255)

### [⬇ Download for Windows](https://github.com/QL1STY/guild-board/releases/latest) · [▶ Try it in your browser](https://ql1sty.github.io/guild-board/demo/) · [Website](https://ql1sty.github.io/guild-board/)

<sub>Free and open source · no ads, no account needed · about 130 MB · no administrator rights · updates itself</sub>

<br>

<img src="https://ql1sty.github.io/guild-board/shots/board.png" alt="The Board on a made-up account: timers, commissions, resin, resets, expeditions, pinned achievements, a clock and notes" width="900">

<sub>The Board, on a made-up account. Every card is a widget you can move, resize, add or remove.</sub>

Unofficial and fan-made. Not affiliated with or endorsed by HoYoverse or COGNOSPHERE.

</div>

<br>

## What it does

TUSKFiZZY is being built on Guild Board's local-first foundation. The upstream Guild Board project is by QL1STY and is used under its MIT licence. This integration is a work in progress; the existing Guild Board feature set is not yet fully adapted to TUSKFiZZY. Guild Board reads your HoYoLAB account, with the login you give it, and keeps what it finds on your own PC. Unless you sign in to its optional online features, nothing about you reaches its developer.

### 🧭 The Board: your day at a glance

Widgets for **timers**, **today's commissions**, **Resin** and when it is full, **daily and weekly resets**, **expeditions**, **pinned achievements**, **notes**, a **clock** on server time, a **quick timer**, the **daily check-in** and six for your pulls. Press **Edit** to drag, resize and add. When a timer is ready you get a Windows notification, and while you play, a short line over the game.

### ✦ Pulls: pity, savings and a plan

Your wish history, the **pity** of every banner, your **luck** against a typical player, a **savings ledger** of the pulls you hold and the ones the next version gives, and a **planner** that says how likely you are to get the characters you want. **Import** reads the history with your HoYoLAB login, so you don't have to open the game first.

<p align="center"><img src="https://ql1sty.github.io/guild-board/shots/pulls.png" alt="The Pulls page: the banners running now, the pity of each banner, the savings ledger and the planner" width="860"></p>

### 🏆 Achievements

The full list with progress counters, stages and **pins** for the ones you are working on. Tick them off by hand, or check Paimon.moe's public list for new ones when you ask. The primogems still to earn are counted for you.

<p align="center"><img src="https://ql1sty.github.io/guild-board/shots/achievements.png" alt="The Achievements page: categories on the left, achievement cards with their progress on the right" width="860"></p>

### 📊 Stats and the Ledger

Chests, achievements, play time, days played and primogems, each against the period before: a **play-time calendar**, **when you play** by the hour, and **primogems by source** each day and month. The Ledger keeps the game's own history of primogems, crystals, resin and artifacts. Every chart can be shown as a table.

<p align="center"><img src="https://ql1sty.github.io/guild-board/shots/stats.png" alt="The Stats page: summary tiles, a play-time calendar and charts of primogems and play time" width="860"></p>

The **Year** tab shows your year as Wrapped, which you can save as a picture to share.

<p align="center"><img src="https://ql1sty.github.io/guild-board/shots/year.png" alt="The Year page: your year as Wrapped, with the character of the year and cards of your numbers" width="860"></p>

### 🗡 Characters

A page for every character you own, built from your account: stats, weapon, artifacts with what to keep or replace, talents, constellations, and a **planner** that works out what each one needs for the levels you choose.

### 🖥 The overlay

Press **Ctrl+Alt+G** and a panel of your widgets appears over the game: timers, commissions, a quick timer, whatever you pinned. Press it again and it is gone. It is a window of Guild Board's own, laid over the screen, not a hook into the game.

<p align="center"><img src="https://ql1sty.github.io/guild-board/shots/overlay.png" alt="The overlay open over the game's screen, with a quick timer, timers, commissions and a clock" width="860"></p>

### 🔧 And the rest

- **Backlog**: every region and area with what is left to explore, the oculi still missing, and open Archon Quests.
- **The interactive map**: HoYoLAB's official Teyvat map inside Guild Board, in tabs you can name and keep.
- **Gift codes**: paste one or several and redeem them, with a note when a new livestream code appears.
- **Friends**: optional, with a Discord sign-in. Compare stats and see who is in game. Friends never see that Guild Board is open or when your PC is on.
- **Screenshot gallery**, **backups** you can move to another PC, **start with Windows**, and **updates** that install with one button and keep your data.

## Download

Windows 10 or 11. Get the installer from the [latest release](https://github.com/QL1STY/guild-board/releases/latest). It needs no administrator rights, and an update or an uninstall leaves your data where it is. An installed copy checks the same page for a newer version and offers it.

An installed copy updates only to an installer that carries a signature by the release key pinned in `src/guildboard/selfupdate.py`.

To check an installer before you run it, see [`SECURITY.md`](SECURITY.md): `gh attestation verify` shows which commit of this repository GitHub built it from.

Want to look around first? [Try it in your browser](https://ql1sty.github.io/guild-board/demo/) on a made-up account, or start the installed app's demo mode, which contacts nothing.

## What it reads

From HoYoLAB, with your own login:

- Your account summary and counters: achievements, days active, chests, oculi and waypoints.
- How far each region and area is explored, with its offerings and reputation.
- Resin and the other Real-Time Notes, and which Archon Quest chapters are still open.
- Your characters, your Spiral Abyss, Imaginarium Theater and Stygian Onslaught results, and your primogem income.
- The current banners and events.
- Your daily check-in: this month's rewards and how many you claimed.
- Your characters' builds, and how many materials you hold, from HoYoLAB; character, weapon and artifact pages and pictures from the HoYoLAB wiki.

Everything stays in one folder on your PC, `%LOCALAPPDATA%\GuildBoard`. Nothing is uploaded unless you sign in to an online account. What an online account sends, and what it does not, is in the [privacy notice](PRIVACY.md).

## What it never does

- It never changes, adds or deletes anything in the game's folder or in the game's Windows settings. The only things it reads there are the game's log and the history links in its web cache, read-only, and only when you press Import on the Pulls or Ledger page and your HoYoLAB login cannot be used for it.
- It never opens the game's process, reads or writes its memory, or hooks into it. All it learns about the game is whether it is running.
- It never sends keys or clicks to the game and never captures its screen.
- Your HoYoLAB login is kept only on this PC, encrypted for your Windows user. It is never written to a log and never sent anywhere but to HoYoverse. A wish-history link's key is kept in memory only, never on disk: for the import, and for up to a day after it to fetch new wishes.
- It talks to HoYoverse's own sites: hoyolab.com, hoyoverse.com and mihoyo.com. Beyond them, it downloads Paimon.moe's public achievement list from GitHub only when you ask to check for new achievements, checks GitHub for a new version of Guild Board, and talks to Guild Board's own server only after you sign in to an online account. It may also download public pictures from other sites, such as the Endgame headers, and sends nothing of yours with them.
- It only reads your account, except to redeem a gift code that you typed, pasted or clicked to redeem, and to claim the daily check-in reward when you press Claim. It never posts, claims or changes anything else for you.
- It never changes anything else on your HoYoLAB account, not even the calculator's sync setting.
- It reads your wish history and your game's own history pages (primogems, crystals, resin, artifacts) with a read-only key HoYoverse gives your HoYoLAB login, kept in memory only (or, without a login, the link the game writes when you open Feedback), and only reads.
- With the gallery on, it looks at the pictures in the game's ScreenShot folder and never moves, changes or deletes one.

`tests/test_handsoff.py` checks these rules against the code. It is only ever tightened, except when the maintainer changes a rule in `CLAUDE.md`, as on 2026-11-06 to allow public pictures from outside HoYoverse.

## The risk

Guild Board is a third-party tool. Please read this before you use it.

Reading your account through HoYoLAB with your own login is low-risk, but not zero-risk.

The developer of the most widely used tool of this kind says that no user has ever reported a ban. Any third-party access is still technically against the Terms of Service, and if action were ever taken, it would plausibly be against the HoYoLAB account rather than the game account.

HoYoverse's terms of service restrict its services to individual, non-commercial enjoyment and bar software that gives an unfair advantage or disrupts the service, and HoYoverse tells players not to use third-party plug-ins. Guild Board gives no advantage in play and does not take part in the game, but it is not made or approved by HoYoverse, and you use it at your own risk.

The saved login cannot log in to the game, change your password or show your email address. It can act on HoYoLAB as you, which is why Guild Board keeps it encrypted and uses it only to read, apart from the two things you ask for: redeeming a gift code and claiming the daily check-in reward.

If HoYoverse asks for something here to change or stop, it changes or stops.

## Build it from source

You need Windows, [uv](https://docs.astral.sh/uv/) with Python 3.12, and Node.js 22.

```powershell
git clone https://github.com/QL1STY/guild-board.git
cd guild-board
powershell -ExecutionPolicy Bypass -File setup.ps1
```

`setup.ps1` installs the Python packages and the packages of the interface and of the shell, builds both and puts a shortcut on your desktop. The window is an Electron shell in `shell/`, written in TypeScript; it starts the Python core and shows the interface. Then:

- `npm start --prefix shell -- --demo` runs Guild Board with its window on made-up data. The shell starts the core from `.venv`. `uv run tuskfizzy --demo --no-shell` runs the core alone and prints a link for your browser.
- `uv run pytest -n auto` runs the Python tests on every core. In `web/`, `npm test` runs the interface's unit tests and `npm run e2e` its browser tests. In `shell/`, `npm test` runs the shell's unit tests and `npm run e2e` its browser tests.
- `powershell -ExecutionPolicy Bypass -File build-installer.ps1` makes the installer. It refuses to build until a release key is pinned in `src/guildboard/selfupdate.py`; `-WithoutReleaseKey` builds an installer that never updates.

[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) says how the parts fit together, and [`CONTRIBUTING.md`](CONTRIBUTING.md) how to help.

## Licence

Guild Board is free software under the [MIT licence](LICENSE). The software inside it has licences of its own, listed in [`THIRD-PARTY-NOTICES.txt`](THIRD-PARTY-NOTICES.txt) and in Settings, under About, as Open-source licences. The achievement list comes from [Paimon.moe](https://github.com/MadeBaruna/paimon-moe)'s public data, also MIT licensed.

This repository holds no game art. Guild Board reads HoYoverse's official pictures and wiki entries while it runs, into a cache on your PC; the game's text in the achievement list comes from Paimon.moe's data.

Unofficial and fan-made. Not affiliated with or endorsed by HoYoverse or COGNOSPHERE. Genshin Impact is a trademark of its owner.
