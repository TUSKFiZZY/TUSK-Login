"""Records the demo core's answers for the site's browser demo.

    uv run python tools/record-site-demo.py [--out web/site-demo/recording] [--demo-now 2026-10-13T08:00:00+00:00]

It starts the demo core on a new, empty data folder in the temp folder (with no way to the internet: a dead proxy, and its own
profile folders), signs in the way the window does (the launch link's cookie), marks the risk statement read, waits for the first
sync, and reads every GET route the interface uses for each page (`PAGES`; the routes that depend on an answer, such as one for each
category, are made from it in `EXPANDERS`). Each answer is written as a JSON file named by its route (`recording_key`, which
web/src/sitedemo.ts makes the same way), with `index.json` listing the routes, and `icons.json` the official address of each picture of the icon cache (`/api/icon/<key>`) by
key. The core is stopped by its process id.

The site's build (`npm run build:site-demo` in web/) serves these files as the recording its transport reads. They hold made-up
data only, and nothing of the machine they were recorded on: the temp data folder's path is replaced.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from guildboard import characters

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "web" / "site-demo" / "recording"
DEMO_NOW = "2026-10-13T08:00:00+00:00"
PICTURES = ROOT / "src" / "guildboard" / "characters" / "demo" / "pictures.json"

Get = Callable[[str], Any]

# What the interface reads on every page (live.ts).
COMMON = ("/api/status", "/api/settings")

# The routes of each page that do not depend on an answer. A page that shows another's models (the Board's widgets) names them too.
PAGES: dict[str, tuple[str, ...]] = {
    "board": (
        "/api/board", "/api/layouts/board", "/api/commissions", "/api/timers", "/api/quick-timers", "/api/checkin",
        "/api/pulls", "/api/achievements?view=all&pinned=true",
    ),
    "achievements": ("/api/timers", "/api/commissions"),
    "backlog": tuple(f"/api/backlog?sort={sort}" for sort in ("least", "closest", "game")),
    "map": ("/api/map",),
    "stats": tuple(f"/api/stats?range={span}" for span in ("7", "30", "90", "all")) + ("/api/stats/endgame", "/api/stats/month", "/api/stats/year"),
    "pulls": ("/api/pulls", "/api/pulls/choices"),
    "ledger": ("/api/ledger",) + tuple(f"/api/ledger/history?kind={kind}&limit=50" for kind in ("primogem", "crystal", "resin", "artifact")),
    "friends": ("/api/friends", "/api/friends?fresh=1", "/api/profile"),
    "characters": ("/api/characters",),
    "week": (),
    "codes": ("/api/codes",),
    "gallery": ("/api/gallery",),
    "tuskfizzy": ("/api/tuskfizzy/month?month=2026-11", "/api/tuskfizzy/tracker?day=2026-11-09"),
    "settings": (
        "/api/synclog?limit=50", "/api/overlay", "/api/ledger", "/api/achievements?unlisted=true", "/api/backups",
        "/api/whats-new", "/api/notices", "/api/gallery",
    ),
    "overlay": ("/api/overlay", "/api/layouts/overlay", "/api/board", "/api/timers", "/api/commissions", "/api/quick-timers"),
}


def _achievements(get: Get) -> list[str]:
    """Each of the three views, whole, as Pinned, and in each category (a category is its position in the list). Pinned keeps the
    category the page is on (modelPath in web/src/pages/achievementsview.ts), and the page is on one once it has opened, so Pinned
    is also recorded with each category."""
    categories = get("/api/achievements?view=left")["categories"]
    routes = []
    for view in ("left", "done", "all"):
        routes.append(f"/api/achievements?view={view}")
        routes.append(f"/api/achievements?view={view}&pinned=true")
        routes.extend(f"/api/achievements?view={view}&category={number}" for number in range(len(categories)))
        routes.extend(f"/api/achievements?view={view}&category={number}&pinned=true" for number in range(len(categories)))
    return routes


def _commissions(get: Get) -> list[str]:
    return [f"/api/commissions?region={region['key']}" for region in get("/api/commissions")["regions"]]


def _widgets(get: Get) -> list[str]:
    """The data a widget keeps (a note's text, a stopwatch's time), for the widgets of both layouts; a widget with none has no route."""
    routes = []
    for place in ("board", "overlay"):
        routes.extend(f"/api/widgets/{widget['id']}/data" for widget in get(f"/api/layouts/{place}")["widgets"])
    return routes


def _characters(get: Get) -> list[str]:
    routes = []
    for entry in get("/api/characters")["entries"]:
        routes.append(f"/api/characters/{entry['key']}")
        routes.append(f"/api/characters/{entry['key']}/plan")
    return routes


def _years(get: Get) -> list[str]:
    """Each year of the Year tab's switch, which the tab asks for by its address when it is chosen."""
    return [f"/api/stats/year?year={year}" for year in get("/api/stats/year")["years"]]


def _stats(get: Get) -> list[str]:
    return _months(get) + _years(get)


def _friends(get: Get) -> list[str]:
    model = get("/api/friends")
    routes = []
    for friend in model["friends"]:
        routes.append(f"/api/friends/{friend['user_id']}")
        if friend.get("compare"):
            routes.append(f"/api/friends/{friend['user_id']}/compare")
    return routes


def _map(get: Get) -> list[str]:
    return [f"/api/map/tabs/{tab['id']}" for tab in get("/api/map")["tabs"]]


def _months(get: Get) -> list[str]:
    """Each month of the Months tab's strip, which the tab asks for by its address when it is chosen."""
    return [f"/api/stats/month?ym={month['ym']}" for month in get("/api/stats/month")["months"]]


# The routes of Your week and the Endgame tab, which the core answers (and the interface asks for) only while the switch that shows
# them is on (`characters.WEEK_AND_ENDGAME`, passed on to the core below).
WEEK_AND_ENDGAME = ("/api/week", "/api/endgame?after=0", "/api/endgame?after=1")
OPTIONAL = {"board": ("/api/week",), "week": ("/api/week",), "characters": WEEK_AND_ENDGAME[1:]}


# The routes that depend on an answer, made from it.
EXPANDERS: dict[str, Callable[[Get], list[str]]] = {
    "achievements": lambda get: _achievements(get) + _commissions(get),
    "board": lambda get: _commissions(get) + _widgets(get),
    "characters": _characters,
    "friends": _friends,
    "map": _map,
    "overlay": _widgets,
    "stats": _stats,
}

# What the interface asks of the core that is not a model to record, by the address it starts with, and why.
NOT_RECORDED = {
    "/api/events": "the stream of the core's events, left out of the browser demo",
    "/api/tuskfizzy/enka/": "a live public Enka profile is specific to a UID and is not part of the made-up browser demo",
    "/api/icon/": "a picture, read by the browser from an address that icons.json holds for its key (web/src/sitedemo.ts)",
    "/api/art/": "a picture, read by the browser from HoYoverse's address (web/src/sitedemo.ts)",
    "/api/gallery/": "the gallery is off, and its pictures are the player's own",
    "/api/friends/": "a friend's picture, read from an address",
    "/api/pulls/history": "the next page of the Pulls history: the first hundred rows are in /api/pulls",
    "/api/commissions": "another day's or region's model that the page has no list of: those of each region are recorded",
}


# What the interface only writes to (POST, PUT, DELETE), by the address it starts with. The recording holds no answer for these, and the
# browser demo's transport refuses what a page sends (web/src/sitedemo.ts). A test reads the interface's source for any other path.
WRITE_ONLY = (
    "/api/achievements/counter", "/api/achievements/group", "/api/achievements/stage", "/api/auth/", "/api/codes/redeem",
    "/api/demo/open", "/api/tuskfizzy/import", "/api/online/", "/api/profile/pins", "/api/profile/sharing", "/api/sync/pause", "/api/sync/resume",
)


def recording_key(route: str) -> str:
    """The name of the file a route is recorded in: its words and a 32-bit FNV-1a hash of the whole route
    (`recordingKey` in web/src/sitedemo.ts makes the same name)."""
    value = 0x811C9DC5
    for byte in route.encode("utf-8"):
        value = ((value ^ byte) * 0x01000193) & 0xFFFFFFFF
    words = re.sub(r"[^A-Za-z0-9]+", "-", re.sub(r"^/api/", "", route)).strip("-")[:60]
    return f"{words}-{value:08x}"


def routes_of(page: str, get: Get, week_and_endgame: bool = False) -> list[str]:
    """Every route of a page: the common ones, its own, and those its answers lead to, without repeats, in order. The routes of Your
    week and the Endgame are there only while the core shows them."""
    routes = list(COMMON) + list(PAGES[page]) + (list(OPTIONAL.get(page, ())) if week_and_endgame else [])
    if page in EXPANDERS:
        routes.extend(EXPANDERS[page](get))
    return list(dict.fromkeys(routes))


class Reader:
    """GET requests to the core with the session the launch link gives."""

    def __init__(self, launch: str) -> None:
        self.origin = launch.split("/launch", 1)[0]
        jar = http.cookiejar.CookieJar()

        class Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *arguments: Any, **keywords: Any) -> None:  # the launch link answers with the cookie and a redirect
                return None

        # No proxy: the core is on this machine.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar), Stay)
        try:
            self.opener.open(launch, timeout=30).close()
        except urllib.error.HTTPError as redirect:
            if redirect.code not in (301, 302, 303, 307, 308):
                raise

    def send(self, method: str, route: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(self.origin + route, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data else {})
        with self.opener.open(request, timeout=60) as answer:
            return json.loads(answer.read().decode("utf-8"))

    def __call__(self, route: str) -> Any:
        return self.send("GET", route)


def _environment(data_dir: Path) -> dict[str, str]:
    """The core's environment: its own profile folders, so that it never finds the player's files, and a proxy where nothing listens,
    so that nothing it attempts reaches the internet (as web/e2e/fixtures.ts does)."""
    own = {
        "APPDATA": str(data_dir / "appdata"), "LOCALAPPDATA": str(data_dir / "localappdata"), "USERPROFILE": str(data_dir / "userprofile"),
        "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
    }
    replaced = {name.upper() for name in own} | {"NO_PROXY"}
    return {**{name: value for name, value in os.environ.items() if name.upper() not in replaced}, **own}


def start_core(data_dir: Path, demo_now: str, week_and_endgame: bool = False) -> tuple[subprocess.Popen[str], str]:
    """The demo core of tools/site_demo_core.py (the demo, whose pictures are named by their official addresses), and its launch link
    (the first line it prints)."""
    program = [sys.executable, str(ROOT / "tools" / "site_demo_core.py"), "--demo-now", demo_now, "--data-dir", str(data_dir)]
    if week_and_endgame:
        program.append("--week-and-endgame")
    core = subprocess.Popen(program, cwd=ROOT, env=_environment(data_dir), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert core.stdout is not None
    deadline = time.monotonic() + 60
    line = ""
    while time.monotonic() < deadline:
        line = core.stdout.readline().strip()
        if line.startswith("http://127.0.0.1:") or core.poll() is not None:
            break
    if not line.startswith("http://127.0.0.1:"):
        stop_core(core)
        raise SystemExit("The demo core did not print its launch link.")
    return core, line


def stop_core(core: subprocess.Popen[str]) -> None:
    """Ends the core by its process id (never `guildboard --quit`, which would ask the player's own Guild Board to quit), and the
    processes it started."""
    if core.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(core.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        core.terminate()
    try:
        core.wait(timeout=30)
    except subprocess.TimeoutExpired:
        core.kill()


def settled(get: Get) -> None:
    for _ in range(400):
        if get("/api/status")["sync"]["state"] != "syncing":
            return
        time.sleep(0.05)
    raise SystemExit("The demo's first sync did not end.")


def record(get: Reader, out: Path, scrub: dict[str, str] | None = None, week_and_endgame: bool = False) -> dict[str, list[str]]:
    """Reads the routes of every page and writes them into `out`. Answers the routes of each page, by page. A route the core does not
    answer is left out and said; one of a page's own fixed routes that fails ends the run."""
    out.mkdir(parents=True, exist_ok=True)
    recorded: dict[str, str] = {}
    by_page: dict[str, list[str]] = {}
    for page in PAGES:
        by_page[page] = []
        for route in routes_of(page, get, week_and_endgame):
            if route not in recorded:
                try:
                    text = json.dumps(get(route), separators=(",", ":"), ensure_ascii=False)
                except urllib.error.HTTPError as refused:
                    if route in COMMON or route in PAGES[page] or (week_and_endgame and route in OPTIONAL.get(page, ())):
                        raise SystemExit(f"{route} answered {refused.code}.") from refused
                    print(f"Left out, the core answered {refused.code}: {route}", file=sys.stderr)
                    continue
                for before, after in (scrub or {}).items():
                    text = text.replace(json.dumps(before)[1:-1], after)
                (out / f"{recording_key(route)}.json").write_text(text, encoding="utf-8", newline="\n")
                recorded[route] = recording_key(route)
            by_page[page].append(route)
    index = {"week_and_endgame": week_and_endgame, "pages": by_page, "routes": recorded}
    (out / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return by_page


def icon_addresses(db: Path) -> dict[str, dict[str, str]]:
    """The pictures of the icon cache (`/api/icon/<key>`) with the official address each is read from, by key, as the demo core's
    labels hold them (STATIC_ICONS, the timer pre-sets' pictures, the banners'): web/src/sitedemo.ts reads them from `icons.json`
    where the core is not there to serve them, and the screenshots' test does the same. Only labels that have an address."""
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = connection.execute("SELECT key, icon_url FROM labels WHERE icon_url <> '' ORDER BY key").fetchall()
    finally:
        connection.close()
    return {key: {"url": url} for key, url in rows}


def missing_pictures(out: Path) -> list[str]:
    """The keys of pictures the recorded answers name that the demo's list holds no address for: those show the drawn sign."""
    wanted: set[str] = set()
    for path in out.glob("*.json"):
        wanted.update(re.findall(r"/api/art/([A-Za-z0-9_-]+)", path.read_text(encoding="utf-8")))
    known = set(json.loads(PICTURES.read_text(encoding="utf-8")))
    return sorted(wanted - known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record the demo core's answers for the site's browser demo.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--demo-now", default=DEMO_NOW)
    args = parser.parse_args(argv)
    if args.out.exists():
        for old in args.out.glob("*.json"):
            old.unlink()
    with tempfile.TemporaryDirectory(prefix="guildboard-site-demo-") as folder:
        data_dir = Path(folder) / "data"
        shown = characters.WEEK_AND_ENDGAME  # the app as it is built: Your week and the Endgame are in the demo while they are in the app
        core, launch = start_core(data_dir, args.demo_now, shown)
        try:
            get = Reader(launch)
            get.send("PUT", "/api/settings", {"risk_acknowledged": True})
            settled(get)
            by_page = record(get, args.out, scrub={str(data_dir): "Guild Board's data folder"}, week_and_endgame=shown)
            icons = icon_addresses(data_dir / "guildboard.db")
        finally:
            stop_core(core)
    (args.out / "icons.json").write_text(json.dumps(icons, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    count = len({route for routes in by_page.values() for route in routes})
    print(f"Recorded {count} answers of {len(by_page)} pages into {args.out}.")
    absent = missing_pictures(args.out)
    if absent:
        print(f"{len(absent)} pictures have no address in pictures.json and will show their drawn sign.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
