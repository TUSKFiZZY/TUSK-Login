"""The local HTTP API. It is bound to 127.0.0.1 only, and every /api request needs the session cookie."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
import mimetypes
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from fastapi import Body, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from guildboard.achievements.commissions import AlreadyToday, NotACommission
from guildboard.achievements.importers import (
    MAX_FILE_BYTES, TOO_LARGE as IMPORT_TOO_LARGE, ImportBackupFailed, ImportChanged, ImportFileError, ImportSaveFailed,
)
from guildboard.achievements.progress import NOT_A_COUNT, BadChange, Progress, UnknownAchievement
from guildboard.achievements.updates import UpdateError
from guildboard.backups import DEMO as BACKUP_DEMO, MAX_BACKUP_BYTES, TOO_LARGE as BACKUP_TOO_LARGE, BackupRefused
from guildboard.board.layouts import LayoutChanged, LayoutError, UnknownPlace, UnknownWidget
from guildboard import characters, notices
from guildboard import enka
from guildboard.cloud import SHARE_KEYS
from guildboard.cloud.demo import STATES as DEMO_ONLINE_STATES
from guildboard.cloud.models import NOT_A_FRIEND, OnlineRefused
from guildboard.characters.art import DISPLAY_SIZES
from guildboard.characters.service import NO_SUCH_CHARACTER, CharactersRefused, UnknownCharacter
from guildboard.config import REGIONS
from guildboard.checkin import CheckInRefused
from guildboard.codes import MAX_PRIMOGEMS
from guildboard.core import CodesRefused, Core, SettingRefused
from guildboard.events import EventBus
from guildboard.gallery import PictureMissing
from guildboard.places import launch_target
from guildboard.ledger import KINDS as LEDGER_KINDS
from guildboard.pulls import POOLS
from guildboard.pulls.importer import ImportBusy, ImportNotReady
from guildboard.pulls.planner import (
    ENTRY_FIELDS, HISTORY_ROUTE_ROWS, MAX_HISTORY_ROWS, MAX_NAME, NO_ENTRY, SETTINGS, PullsConflict, PullsRefused,
    UnknownEntry, as_day, switches_fit,
)
from guildboard.pulls.rules import PATHS
from guildboard.records import DataSwitchOff, LoginNeeded, Offline, SourceError, Unexpected, VerificationNeeded
from guildboard.store import iso, valid_text
from guildboard.sync.policy import DAILY_CAP
from guildboard.maptabs import MapError, UnknownTab
from guildboard.timers.quick import QuickTimerError, UnknownQuickTimer
from guildboard.timers.reminders import TEST_TEXT
from guildboard.timers.rules import RuleError, parse_rule
from guildboard.timers.timers import NO_TIMER, TimerConflict, TimerError, UnknownTimer
from guildboard.views.achievements import VIEWS, Query as AchievementQuery
from guildboard.views.backlog import SORTS
from guildboard.views.stats import RANGE_KEYS

SESSION_COOKIE = "gb_session"
# What the interface's files are, whatever this PC's registry says: on Windows the registry's types override Python's own, and
# a PC that maps .js to text/plain gets a module script the browser refuses, and a blank window.
STATIC_TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".webp": "image/webp"}


def _same(presented: str, secret: str) -> bool:
    """Whether a secret was presented. Compared as bytes: `compare_digest` of two texts raises for one that is not ASCII, and a
    request's key or cookie may be anything."""
    return secrets.compare_digest(presented.encode("utf-8", "replace"), secret.encode("utf-8"))
KEEP_ALIVE_SECONDS = 15.0
MAX_LOG_ROWS = 500
MAX_CLOCK_STEP_SECONDS = 400 * 86_400
SLOW_DEMO_SECONDS = 0.5
# The page may load and contact only TUSKFiZZY itself; the browser enforces it.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)

PLACEHOLDER_PAGE = (
    "<!doctype html><meta charset='utf-8'><title>TUSKFiZZY</title>"
    "<p>TUSKFiZZY's core is running. The interface has not been built yet.</p>"
)
SIGNED_IN_LINE = "Signed in. You can close this tab."
NOT_FINISHED_LINE = "Signing in did not finish. You can close this tab and try again in TUSKFiZZY."
# What a sign-in's code can be: URL-safe characters, 512 at most. Supabase's are far shorter.
CALLBACK_CODE = re.compile(r"[A-Za-z0-9._~-]{1,512}")

ONE_CHANGE = "Change one thing at a time: status, pinned, note or part."
UNEXPECTED_REQUEST = "This request does not have the expected shape."
MAX_ONLINE_NAME = 40  # past this no name can fit; a shorter one is the database's to judge
MAX_FRIEND_QUERY = 64  # a tag or a UID, with room for spaces around it
NO_PICTURE_OF_A_FRIEND = "There is no picture for this."
MAX_LIVE_CODES = 50  # the livestream codes a page names for Redeem all: a stream gives three or four
MAX_CODE_CHARS = 40  # a gift code is about a dozen letters
# A player's id as the server gives it, and nothing else: it goes into the address of a request to the server.
PLAYER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LEDGER_ROWS = 50  # the Ledger history's page
MAX_LEDGER_ROWS = 100
NO_OFFSET = "Give the time with its offset, for example 2026-10-20T10:00:00+02:00."
NO_PICTURE = "This picture is not in the gallery. Press Look again."
UNKNOWN_REGION =f"Unknown region. Use {', '.join(REGIONS[:-1])} or {REGIONS[-1]}."
# An achievement's id as the page sends it: digits only. A longer number is no id either, and Python refuses to read
# one of thousands of digits.
WHOLE_NUMBER = re.compile(r"[0-9]{1,18}")
# FastAPI refuses a body that is not a JSON object before the endpoint runs. These endpoints have their own sentence
# for a body of the wrong shape, and that refusal says it too. Every other request FastAPI refuses gets
# UNEXPECTED_REQUEST.
BODY_REFUSALS = {
    "/api/achievements/stage/{stage_id}": ONE_CHANGE,
    "/api/achievements/counter/{group_id}": NOT_A_COUNT,
}
# The most a request's body may hold, weighed before a route reads any of it (_BodyLimit). A request of the page's is
# small: the largest, a Board layout of sixty widgets, is a fraction of this. Three carry a file: the achievement import,
# whose file's text JSON carries (an escaped character is two bytes), the restore of a backup file, sent as the body
# itself, and the Wrapped poster's PNG, sent as the body itself too.
MAX_BODY_BYTES = 1_000_000
MAX_IMPORT_BODY_BYTES = 2 * MAX_FILE_BYTES + MAX_BODY_BYTES
TOO_LARGE = "This request is too large."
IMPORT_PATHS = ("/api/import/preview", "/api/import/apply")
RESTORE_PATH = "/api/backup/restore"
PICTURE_PATH = "/api/stats/year/picture"
MAX_PICTURE_BYTES = 16 * 1024 * 1024  # a poster of 2,560 × 1,440 is a few megabytes
PICTURE_TOO_LARGE = "This picture is too large to save."


def _demo_faults() -> dict[str, SourceError]:
    return {
        "offline": Offline("HoYoLAB could not be reached."),
        "verification_needed": VerificationNeeded("HoYoLAB asked for a verification check."),
        "login_needed": LoginNeeded("HoYoLAB no longer accepts the saved login."),
        "switch_off": DataSwitchOff(
            "HoYoLAB reports that this data is switched off. Turn on the Battle Chronicle and "
            "Real-Time Notes in your HoYoLAB privacy settings."
        ),
        "error": Unexpected("The sync failed unexpectedly. See the log file."),
    }


NO_NOTICES = "The list of open-source licences is not next to the program."


def _valid_tuskfizzy_portrait(value: Any) -> bool:
    if value == "":
        return True
    if not isinstance(value, str) or len(value) > 1_000_000:
        return False
    match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]+={0,2})", value)
    if match is None:
        return False
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except (ValueError, binascii.Error):
        return False
    if len(raw) > 750_000:
        return False
    kind = match.group(1)
    return (
        kind == "png" and raw.startswith(b"\x89PNG\r\n\x1a\n")
        or kind == "jpeg" and raw.startswith(b"\xff\xd8\xff")
        or kind == "webp" and len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"
        or kind == "gif" and raw[:6] in (b"GIF87a", b"GIF89a")
    )


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _refused(request: Request, in_body: bool) -> JSONResponse:
    """The answer to a request that FastAPI refuses before the endpoint runs. FastAPI's own answer repeats what was
    sent, and fails on half of an emoji or on bytes that are not UTF-8. This one never repeats it."""
    route = getattr(request.scope.get("route"), "path", "")
    return _error(422, BODY_REFUSALS.get(route, UNEXPECTED_REQUEST) if in_body else UNEXPECTED_REQUEST)


def _callback_page(line: str, status: int) -> HTMLResponse:
    """The line a browser ends on after signing in. The address it came to holds the code, so neither a cache nor a page it
    goes to next may keep it."""
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><title>TUSKFiZZY</title><p>{line}</p>",
        status_code=status, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def _sse(kind: str, data: Any) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def _share_keys(value: Any) -> list[str] | None:
    """The switches or pins a page sent, when they are a list of keys a player can share."""
    return value if isinstance(value, list) and all(isinstance(key, str) and key in SHARE_KEYS for key in value) else None


def _player(user: str) -> str:
    """A player's id from an address, or the page's refusal: anything else never reaches a request to the server."""
    if PLAYER_ID.fullmatch(user) is None:
        raise OnlineRefused(NOT_A_FRIEND, 404)
    return user


def _whole(value: Any) -> bool:
    """A whole number from JSON. Python counts true and false as ints, but they are not counts or part numbers."""
    return isinstance(value, int) and not isinstance(value, bool)


def _moment(value: Any) -> datetime | None:
    """A time the page sent, as ISO 8601 with its offset; None when it is not one. The page works the time out from the
    core's clock, so the offset says what instant it means."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _stage_change(progress: Progress, stage_id: int, body: dict[str, Any]) -> Callable[[], str] | None:
    """The change to a stage that a request asks for, or None unless it names exactly one thing, with a value of the
    right type. Progress checks the value itself."""
    if len(body) != 1:
        return None
    [(name, value)] = body.items()
    if name == "status" and isinstance(value, str):
        return lambda: progress.set_status(stage_id, value)
    if name == "pinned" and isinstance(value, bool):
        return lambda: progress.set_pinned(stage_id, value)
    if name == "note" and isinstance(value, str):
        return lambda: progress.set_note(stage_id, value)
    if name == "part" and isinstance(value, dict) and value.keys() == {"index", "done"}:
        index, done = value["index"], value["done"]
        if _whole(index) and isinstance(done, bool):
            return lambda: progress.set_part(stage_id, index, done)
    return None


def _pulls_settings(body: dict[str, Any]) -> bool:
    """Whether a request to change the Pulls settings has the expected shape: the settings at most, names as text of at
    most 80 characters (ruling R11), corrections for the four cards and Fate Points for the two paths, the switches
    true or false, and a typed events count as a version with its primogems from 0 to 100,000 or null. The planner
    checks the other values and says what is wrong with them."""
    if set(body) - set(SETTINGS):
        return False
    for name in ("charted_weapon", "chronicled_path"):
        if name in body and not (isinstance(body[name], str) and len(body[name]) <= MAX_NAME):
            return False
    corrections, points = body.get("corrections", {}), body.get("points", {})
    return (
        isinstance(corrections, dict) and set(corrections) <= set(POOLS)
        and isinstance(points, dict) and set(points) <= set(PATHS) and switches_fit(body)
    )


def _import_file(body: dict[str, Any]) -> tuple[str | None, str | None, str | None] | None:
    """The text, file and profile that an import request gives, each None when it is left out. None when one of them
    is not a string."""
    named = (body.get("text"), body.get("file"), body.get("profile"))
    return named if all(value is None or isinstance(value, str) for value in named) else None


def _is_a_file_body(headers: Headers) -> bool:
    """Whether a request sends a file as its body: its Content-Type is application/octet-stream, parameters and case aside.
    The body limit and the restore route both ask it of the request's headers, which Starlette reads the same way for both
    (the first Content-Type, if a request names two), so that the bound applied is the bound of the form the route reads."""
    return headers.get("content-type", "").partition(";")[0].strip().lower() == "application/octet-stream"


def _body_bound(path: str, file_body: bool) -> tuple[int, str]:
    """The most bytes a request's body may hold at its path, and the sentence that refuses a larger one. `file_body` is
    whether the request sends a file as its body (_is_a_file_body)."""
    if path == RESTORE_PATH and file_body:
        return MAX_BACKUP_BYTES, BACKUP_TOO_LARGE
    if path in IMPORT_PATHS:
        return MAX_IMPORT_BODY_BYTES, IMPORT_TOO_LARGE
    if path == PICTURE_PATH:
        return MAX_PICTURE_BYTES, PICTURE_TOO_LARGE
    return MAX_BODY_BYTES, TOO_LARGE


class _BodyLimit:
    """Refuses with 413 a request whose body is larger than its path takes, before a route reads any of it: by the length
    the request gives, else by counting what arrives, so that a body sent in pieces with no length is cut off as well.
    FastAPI reads a JSON body whole before the route runs, and a check after that would come too late."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Any], send: Callable[..., Any]) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)  # as the route's `request.headers` reads them
        bound, sentence = _body_bound(scope["path"], _is_a_file_body(headers))
        length = headers.get("content-length", "")
        if length.isascii() and length.isdigit() and (len(length) > 15 or int(length) > bound):
            await _error(413, sentence)(scope, receive, send)
            return
        arrived = 0

        async def counted() -> Any:
            nonlocal arrived
            message = await receive()
            if message["type"] == "http.request":
                arrived += len(message.get("body", b""))
                if arrived > bound:
                    raise HTTPException(413, sentence)  # FastAPI lets it through its reading of the body
            return message

        await self.app(scope, counted, send)


async def sse_stream(bus: EventBus, hello: Callable[[], dict[str, Any]]) -> AsyncIterator[str]:
    """Server-sent events: a hello with the current status, then every bus event as it happens. The stream listens
    to the bus before it reads the hello, so an event published meanwhile follows the hello and is not lost."""
    queue = bus.subscribe()
    try:
        yield _sse("hello", hello())
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=KEEP_ALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if event["type"] == "shutdown":
                return  # TUSKFiZZY is closing; ending the stream lets the server stop at once
            yield _sse(event["type"], event)
    finally:
        bus.unsubscribe(queue)


def create_app(core: "Core", port: int, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    # Added before `protect`, so that it runs inside it: a request is let in before its size is weighed.
    app.add_middleware(_BodyLimit)

    @app.exception_handler(413)
    async def refuse_too_large(request: Request, error: Exception) -> JSONResponse:
        """A body that _BodyLimit found larger than its path takes while it arrived."""
        return _error(413, str(getattr(error, "detail", TOO_LARGE)))

    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @app.middleware("http")
    async def protect(request: Request, call_next: Any) -> Response:
        response = await _let_in(request, call_next)
        response.headers["X-Content-Type-Options"] = "nosniff"  # a reply is what its Content-Type says, not what a browser guesses
        return response

    async def _let_in(request: Request, call_next: Any) -> Response:
        if request.headers.get("host", "") not in allowed_hosts:
            return _error(400, "This address is not served.")
        if request.url.path.startswith("/api/"):
            # A page served by another program on this PC would send the session cookie too, because
            # cookies ignore the port. Browsers say where a request comes from, so anything that is
            # not TUSKFiZZY's own page is refused. Programs that are not browsers send neither header.
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site")
            if (origin is not None and origin not in allowed_origins) or fetch_site not in (None, "same-origin", "none"):
                return _error(403, "This request did not come from TUSKFiZZY.")
            presented = request.cookies.get(SESSION_COOKIE, "")
            if not _same(presented, core.session_token):
                return _error(401, "Open TUSKFiZZY from its own window or launch link.")
            return await call_next(request)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response

    @app.exception_handler(RequestValidationError)
    async def refuse_invalid(request: Request, error: RequestValidationError) -> JSONResponse:
        """A body that is not a JSON object, or a path or query value of the wrong kind. When the path or a query
        value is wrong too, the answer is about those."""
        in_body = all(tuple(problem.get("loc", ()))[:1] == ("body",) for problem in error.errors())
        return _refused(request, in_body)

    @app.exception_handler(400)
    async def refuse_unreadable(request: Request, error: Exception) -> JSONResponse:
        """FastAPI raises a 400 of its own for a body it cannot read as JSON at all: bytes that are not text, or
        nesting too deep to read. Nothing else here raises one, because the endpoints answer with _error."""
        return _refused(request, in_body=True)

    @app.get("/launch")
    async def launch(k: str = "", to: str = "") -> Response:
        """Exchange the launch secret for the session cookie, and open the page, on the place `to` names when it is
        one the shell asks for. A click on a reminder asks for the Timers category, and one on a Characters notice for
        Your week or a character."""
        if not _same(k, core.launch_secret):
            return _error(403, "This launch link is not valid.")
        response = RedirectResponse(launch_target(to),status_code=303)
        response.set_cookie(SESSION_COOKIE, core.session_token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/auth/callback/{nonce}")
    async def auth_callback(nonce: str, code: str | None = None) -> Response:
        """Where the system browser lands after signing in. The one route that needs no launch secret: that browser never
        had the window's cookie. The nonce lets it in, once. Nothing else is said about why a sign-in did not finish:
        whoever opened this address may not be the player. A code no sign-in gives, longer than 512 characters or with
        anything but URL-safe characters, counts as none. The code becomes a secret of the log only once its nonce
        matched (finish_sign_in), so that a stray request leaves nothing behind."""
        if code is not None and not CALLBACK_CODE.fullmatch(code):
            code = None
        if await core.online.finish_sign_in(nonce, code):
            return _callback_page(SIGNED_IN_LINE, 200)
        return _callback_page(NOT_FINISHED_LINE, 400)

    @app.get("/api/online")
    async def online() -> dict[str, Any]:
        return core.online.view()

    @app.post("/api/online/sign-in")
    async def online_sign_in() -> dict[str, Any]:
        core.online.begin_sign_in(port)
        return core.online.view()

    @app.post("/api/online/sync")
    async def online_sync() -> dict[str, Any]:
        """Sync now: the account's rows come down and this PC's changes go up, at once."""
        return await core.online.sync_now()

    @app.post("/api/online/first-sync")
    async def online_first_sync(body: dict[str, Any] = Body(...)) -> Response:
        """The answer to the question of the first sync: "keep" this PC's data or "use" the account's. Nothing else goes on."""
        choice = body.get("choice")
        if choice not in ("keep", "use"):
            return _error(422, "Choose to keep this PC's data or to use the account's.")
        return JSONResponse(await core.online.first_sync(choice))

    @app.post("/api/online/sign-out")
    async def online_sign_out() -> dict[str, Any]:
        await core.online.sign_out()
        return core.online.view()

    @app.post("/api/online/delete")
    async def online_delete(body: dict[str, Any] = Body(...)) -> Response:
        """Delete my online data. Nothing but a plain true goes on: the page asks the player first."""
        if body.get("confirm") is not True:
            return _error(422, UNEXPECTED_REQUEST)
        await core.online.delete_account()
        return JSONResponse(core.online.view())

    @app.exception_handler(OnlineRefused)
    async def refuse_online(request: Request, error: OnlineRefused) -> JSONResponse:
        """What the server refused or could not be asked, in the player's words."""
        return _error(error.status, error.sentence)

    @app.get("/api/profile")
    async def your_profile() -> dict[str, Any]:
        return await core.online.your_profile()

    @app.put("/api/profile/sharing")
    async def profile_sharing(body: dict[str, Any] = Body(...)) -> Response:
        keys = _share_keys(body.get("keys"))
        if keys is None:
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(await core.online.set_sharing(keys))

    @app.put("/api/profile/pins")
    async def profile_pins(body: dict[str, Any] = Body(...)) -> Response:
        keys = _share_keys(body.get("keys"))
        if keys is None:
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(await core.online.set_pins(keys))

    @app.put("/api/profile/find-by-uid")
    async def profile_find_by_uid(body: dict[str, Any] = Body(...)) -> Response:
        on = body.get("on")
        if not isinstance(on, bool):
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(await core.online.set_find_by_uid(on))

    @app.put("/api/profile/name")
    async def profile_name(body: dict[str, Any] = Body(...)) -> Response:
        name = body.get("name")
        if not isinstance(name, str) or len(name) > MAX_ONLINE_NAME:
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(await core.online.rename(name))

    @app.get("/api/friends")
    async def friends(fresh: bool = False) -> dict[str, Any]:
        return await core.online.friends_model(fresh)

    @app.post("/api/friends/request")
    async def friends_request(body: dict[str, Any] = Body(...)) -> Response:
        query = body.get("query")
        if not isinstance(query, str) or len(query) > MAX_FRIEND_QUERY:
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(await core.online.send_request(query))

    @app.post("/api/friends/invite-link")
    async def friends_invite_link() -> dict[str, Any]:
        return {"link": await core.online.invite_link()}

    @app.post("/api/friends/invite")
    async def friends_send_invite() -> dict[str, Any]:
        """Sends the request of the invite that is waiting: the player's press, which nothing else stands in for."""
        return await core.online.send_invite()

    @app.delete("/api/friends/invite")
    async def friends_drop_invite() -> dict[str, Any]:
        """Drops the invite that is waiting, and answers with the view: a player who is signed out can do this too."""
        core.online.drop_invite()
        return core.online.view()

    @app.post("/api/friends/{user}/accept")
    async def friends_accept(user: str) -> dict[str, Any]:
        return await core.online.accept(_player(user))

    @app.post("/api/friends/{user}/decline")
    async def friends_decline(user: str) -> dict[str, Any]:
        return await core.online.decline(_player(user))

    @app.delete("/api/friends/{user}")
    async def friends_remove(user: str) -> dict[str, Any]:
        return await core.online.remove(_player(user))

    @app.get("/api/friends/{user}")
    async def friend_profile(user: str) -> dict[str, Any]:
        return await core.online.friend_profile(_player(user))

    @app.get("/api/friends/{user}/compare")
    async def friend_compare(user: str) -> dict[str, Any]:
        return await core.online.compare_model(_player(user))

    @app.get("/api/friends/{user}/picture")
    async def friend_picture(user: str) -> Response:
        link = await core.online.friend_picture(_player(user))
        found = await core.icons.get(link) if link is not None else None
        if found is None:
            return _error(404, NO_PICTURE_OF_A_FRIEND)
        data, media_type = found
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return core.status()

    @app.get("/api/notices")
    async def open_source_notices() -> Response:
        """The text of THIRD-PARTY-NOTICES.txt, for Settings' About group."""
        try:
            return JSONResponse({"text": notices.notices_file().read_text(encoding="utf-8")})
        except OSError:
            return _error(404, NO_NOTICES)

    @app.post("/api/sync")
    async def sync() -> dict[str, Any]:
        decision = core.scheduler.request("manual")
        return {"started": decision.run, "reason": decision.reason, "wait_seconds": decision.wait_seconds}

    @app.post("/api/update/install")
    async def install_update() -> Any:
        """Download the newer release's installer and start it; the installer asks TUSKFiZZY to quit."""
        if not core.updater.install():
            return _error(409, "There is no update to install.")
        return core.status()

    @app.get("/api/checkin")
    async def checkin() -> dict[str, Any]:
        """The daily check-in. It looks at HoYoLAB in the background, within its gaps, and answers at once; what it finds
        reaches the page as checkin_changed."""
        core.checkin.look_soon()
        return core.checkin.model()

    @app.post("/api/checkin/claim")
    async def checkin_claim() -> Response:
        """Claim today's reward: the player pressed Claim. Nothing else sends this (the daily check-in design)."""
        try:
            return JSONResponse(await core.checkin.claim())
        except CheckInRefused as error:
            return _error(409, str(error))

    @app.get("/api/checkin/icon/{index}")
    async def checkin_icon(index: int) -> Response:
        link = core.checkin.icon_url(index)
        found = await core.icons.get(link) if link else None
        if found is None:
            return _error(404, "There is no icon for this.")
        data, media_type = found
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/codes")
    async def codes() -> dict[str, Any]:
        """The gift codes page: totals, history, the new livestream codes and the progress of a run. It looks at the
        livestream codes in the background, unless it did a minute ago, and answers at once; a new code reaches the page
        as codes_changed."""
        core.look_for_live()
        return core.codes_model()

    def _primogems_ok(value: Any) -> bool:
        return _whole(value) and 0 <= value <= MAX_PRIMOGEMS

    def _start_redeem(start: Callable[[], bool]) -> Response:
        try:
            started = start()
        except CodesRefused as error:
            return _error(409, str(error))
        return JSONResponse({"started": True}) if started else _error(409, "Codes are being redeemed.")

    @app.post("/api/codes/redeem")
    async def codes_redeem(body: dict[str, Any] = Body(...)) -> Response:
        """Redeem the codes the player typed or pasted, one by one. The only request that changes the account."""
        text, primogems = body.get("text"), body.get("primogems")
        if not isinstance(text, str) or len(text) > 2000 or not (primogems is None or _primogems_ok(primogems)):
            return _error(422, UNEXPECTED_REQUEST)
        return _start_redeem(lambda: core.redeem_codes(text, primogems))

    @app.post("/api/codes/redeem-live")
    async def codes_redeem_live(body: dict[str, Any] = Body(...)) -> Response:
        """Redeem the new livestream codes the player is looking at, which the page names: a code that came since it drew
        them is not sent with them (the audit's WEB-3)."""
        codes = body.get("codes")
        if (
            set(body) != {"codes"} or not isinstance(codes, list) or len(codes) > MAX_LIVE_CODES
            or not all(isinstance(code, str) and 0 < len(code) <= MAX_CODE_CHARS for code in codes)
        ):
            return _error(422, UNEXPECTED_REQUEST)
        return _start_redeem(lambda: core.redeem_live(codes))

    @app.put("/api/codes/{code}")
    async def codes_primogems(code: str, body: dict[str, Any] = Body(...)) -> Response:
        """Correct the primogems a code gave."""
        primogems = body.get("primogems")
        if set(body) != {"primogems"} or not _primogems_ok(primogems):
            return _error(422, UNEXPECTED_REQUEST)
        if not core.set_code_primogems(code, primogems):
            return _error(404, "There is no such code.")
        return JSONResponse(core.codes_model())

    @app.delete("/api/codes/{code}")
    async def codes_remove(code: str) -> Response:
        """Take a code out of the history; it leaves the totals and can be sent again."""
        if not core.remove_code(code):
            return _error(404, "This code is not in your history.")
        return JSONResponse(core.codes_model())

    @app.get("/api/whats-new")
    async def whats_new() -> list[dict[str, Any]]:
        return core.whats_new_history()

    @app.post("/api/whats-new/seen")
    async def whats_new_seen() -> dict[str, Any]:
        core.whats_new_seen()
        return core.status()

    @app.post("/api/sync/pause")
    async def pause() -> dict[str, Any]:
        core.set_paused(True)
        return core.status()

    @app.post("/api/sync/resume")
    async def resume() -> dict[str, Any]:
        core.set_paused(False)
        return core.status()

    @app.get("/api/backlog")
    async def backlog(sort: str = "least") -> Response:
        if sort not in SORTS:
            return _error(422, "Unknown sort. Use least, closest or game.")
        return JSONResponse(core.backlog(sort))

    @app.get("/api/stats")
    async def stats(range_key: str = Query("30", alias="range")) -> Response:
        if range_key not in RANGE_KEYS:
            return _error(422, "Unknown range. Use 7, 30, 90 or all.")
        return JSONResponse(core.stats(range_key))

    @app.get("/api/stats/endgame")
    async def stats_endgame() -> Response:
        """The Overview's Endgame card (the Stats design, 3.1): a column for each mode, its pictures at /api/art. The
        modes' wiki entries and header pictures are read in the background, after consent (the design, 3.2)."""
        core.endgame_art_wanted()
        return JSONResponse(core.stats_endgame())

    @app.get("/api/stats/month")
    async def stats_month(ym: str | None = None) -> Response:
        """The Months tab (the Stats design, section 5): the list of months, and the month `ym`, or the newest when it is
        missing or not one of them."""
        return JSONResponse(core.stats_month(ym))

    @app.get("/api/stats/year")
    async def stats_year(year: str | None = None) -> Response:
        """The Year tab (the Stats design, section 6): the years listed, and the year `year`, or the newest when it is
        missing or not one of them."""
        chosen = int(year) if year is not None and year.isascii() and year.isdigit() and len(year) == 4 else None
        return JSONResponse(core.stats_year(chosen))

    @app.post(PICTURE_PATH)
    async def stats_year_picture(request: Request, name: str = "") -> Response:
        """The Wrapped poster as the PNG the page made (the Stats design, 6.4), its body the picture itself and `name` the
        name the Save dialog offers. The core holds it in memory and asks the window for its Save dialog, which writes the
        file the player chooses. Answers 200 {"opened": true}; 409 when there is no window, and the page then downloads
        the picture itself; 422 for a name that is not the poster's or a body that is no PNG; 413 for one over 16 MB
        (_BodyLimit)."""
        try:
            core.hold_picture(name, await request.body())
        except ValueError as error:
            return _error(422, str(error))
        if not core.request_window("save_picture"):
            core.drop_picture()
            return _error(409, "Open TUSKFiZZY's window to save the picture.")
        return JSONResponse({"opened": True})

    @app.get("/api/synclog")
    async def synclog(limit: int = 50) -> list[dict[str, Any]]:
        rows = core.store.recent_runs(limit=max(1, min(limit, MAX_LOG_ROWS)))
        return [
            {
                "id": row.id,
                "started_at": iso(row.started_at),
                "finished_at": iso(row.finished_at) if row.finished_at is not None else None,
                "trigger": row.trigger,
                "outcome": row.outcome,
                "error_kind": row.error_kind,
                "message": row.message,
            }
            for row in rows
        ]

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        return core.settings.to_dict()

    @app.put("/api/settings")
    async def put_settings(changes: dict[str, Any] = Body(...)) -> Response:
        try:
            return JSONResponse(core.update_settings(changes).to_dict())
        except SettingRefused as error:
            # In `detail`, where the Overlay settings read why a hotkey is refused; nothing of the request was saved.
            return JSONResponse({"detail": str(error)}, status_code=422)

    @app.get("/api/overlay")
    async def overlay() -> dict[str, Any]:
        """What the Overlay settings show beside the settings: the shell's displays and the state of the hotkey."""
        return core.overlay_status()

    async def _ask_window(kind: str) -> Response:
        if not core.request_window(kind):
            return _error(409, "This needs the TUSKFiZZY window.")
        return JSONResponse({"opened": True})

    async def _open_window(kind: str) -> Response:
        if core.demo:
            return _error(409, "Demo mode has no login.")
        return await _ask_window(kind)

    @app.post("/api/data/open")
    async def data_open() -> Response:
        return await _ask_window("data_folder")

    @app.post("/api/demo/open")
    async def demo_open() -> Response:
        return await _ask_window("demo")

    @app.post("/api/auth/login")
    async def auth_login() -> Response:
        return await _open_window("login")

    @app.post("/api/auth/verify")
    async def auth_verify() -> Response:
        return await _open_window("verify")

    @app.post("/api/auth/logout")
    async def auth_logout() -> Response:
        if core.demo:
            return _error(409, "Demo mode has no login.")
        core.logout()
        return JSONResponse(core.status())

    # One backup or restore at a time: their work runs in worker threads, and two at once would share the upload's file,
    # the staged restore and the name of a copy made in the same second.
    backup_work = asyncio.Lock()

    @app.post("/api/backup")
    async def backup() -> dict[str, str]:
        async with backup_work:
            return {"file": await core.backup_now()}

    @app.post("/api/backup/export")
    async def backup_export() -> Response:
        """Ask the window to offer its Save dialog; the window then writes the file through the core."""
        if core.demo:
            return _error(422, BACKUP_DEMO)
        if not core.request_window("export_backup"):
            return _error(409, "Open TUSKFiZZY's window to export a backup.")
        return JSONResponse({"opened": True})

    @app.get("/api/backups")
    async def backups() -> list[dict[str, str]]:
        return core.backup_list()

    @app.post(RESTORE_PATH)
    async def backup_restore(request: Request) -> Response:
        """Stage a restore, then restart. It comes in one of two forms, each bounded by _BodyLimit:

        - a backup file's bytes as the body itself, with Content-Type application/octet-stream, written to the data folder
          as they arrive, never held whole. The page also names the file in the query (?file_name=...); the name is not
          read, as a file is judged by what it holds and not by what it is called, so that a backup that was renamed is
          restored as well;
        - JSON: {"name": ...}, one of the listed copies in the backups folder. A backup file in base64 in JSON is not
          taken any more.

        Answers: 200 {"restarting": ...}; 422 {"error": ...} for a file that is no backup or a request of another shape;
        413 {"error": ...} for a body over its bound (MAX_BACKUP_BYTES as the file's own bytes). What takes a while (writing
        the file, reading the JSON, staging) runs in worker threads, so that the core goes on answering
        meanwhile."""
        if core.demo:
            return _error(422, BACKUP_DEMO)
        async with backup_work:
            return await _restore(request)

    async def _restore(request: Request) -> Response:
        upload = core.paths.data_dir / "restore-upload.tmp"
        try:
            if _is_a_file_body(request.headers):
                try:
                    with open(upload, "wb") as out:
                        async for piece in request.stream():
                            await asyncio.to_thread(out.write, piece)
                    return JSONResponse(await core.restore_from(upload))
                finally:
                    upload.unlink(missing_ok=True)
            try:
                body = await asyncio.to_thread(json.loads, await request.body())
            except (ValueError, RecursionError):
                return _error(422, UNEXPECTED_REQUEST)
            if not isinstance(body, dict):
                return _error(422, UNEXPECTED_REQUEST)
            name = body.get("name")
            if not isinstance(name, str):
                return _error(422, UNEXPECTED_REQUEST)
            if name not in {item["name"] for item in core.backup_list()}:
                return _error(422, "That backup is not in the backups folder.")
            return JSONResponse(await core.restore_from(core.paths.backups_dir / name))
        except BackupRefused as error:
            return _error(422, str(error))

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        # Not core.status(): the stream starts after this returns, and an event published before then would be newer
        # than a status read here. The stream reads it once it listens to the bus.
        return StreamingResponse(
            sse_stream(core.bus, core.status),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/icon/{key:path}")
    async def icon(key: str) -> Response:
        label = core.store.label(key)
        found = await core.icons.get(label.icon_url) if label is not None else None
        if found is None:
            return _error(404, "There is no icon for this.")
        data, media_type = found
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/achievements")
    async def achievements(
        view: str = "left",
        category: str | None = None,
        pinned: bool = False,
        q: str = "",
        unlisted: bool = False,
        keep: list[str] = Query([]),  # repeated: keep=81732&keep=81733
    ) -> Response:
        if view not in VIEWS:
            return _error(422, "Unknown view. Use left, done or all.")
        if not all(WHOLE_NUMBER.fullmatch(value) for value in keep):
            return _error(422, UNEXPECTED_REQUEST)  # as FastAPI's own refusal of a query value of the wrong kind
        query = AchievementQuery(
            view=view, category=category, pinned=pinned, search=q, unlisted=unlisted, keep=frozenset(map(int, keep))
        )
        return JSONResponse(core.achievements(query))

    def _change_progress(change: Callable[[], str]) -> Response:
        """Make a change to the player's progress and tell the pages, or say why it is refused."""
        try:
            label = change()
        except (UnknownAchievement, NotACommission) as error:
            return _error(404, str(error))
        except AlreadyToday as error:
            return _error(409, str(error))
        except BadChange as error:
            return _error(422, str(error))
        core.achievements_changed()
        return JSONResponse({"change": label})

    @app.post("/api/achievements/stage/{stage_id}")
    async def achievements_stage(stage_id: int, body: dict[str, Any] = Body(...)) -> Response:
        change = _stage_change(core.progress, stage_id, body)
        return _error(422, ONE_CHANGE) if change is None else _change_progress(change)

    @app.post("/api/achievements/counter/{group_id}")
    async def achievements_counter(group_id: int, body: dict[str, Any] = Body(...)) -> Response:
        value = body.get("value")
        if not _whole(value):
            return _error(422, NOT_A_COUNT)
        return _change_progress(lambda: core.progress.set_counter(group_id, value))

    @app.post("/api/achievements/group/{group_id}/unpin")
    async def achievements_unpin(group_id: int) -> Response:
        """Unpin every pinned stage of the achievement as one change: Pin on a card is the whole achievement's."""
        return _change_progress(lambda: core.progress.unpin_achievement(group_id))

    @app.post("/api/achievements/undo")
    async def achievements_undo() -> Response:
        label = core.progress.undo()
        if label is None:
            return _error(409, "There is nothing to undo.")
        core.achievements_changed()
        core.timers_changed()  # the change undone may have been a timer's
        return JSONResponse({"undone": label, "more": core.store.newest_change_label() is not None})

    # ------------------------------------------------------------------ daily commissions
    @app.get("/api/commissions")
    async def commissions(region: str | None = None) -> Response:
        """The Daily commissions category's model, for the region named, else for the region set in Settings."""
        if region is not None and region not in REGIONS:
            return _error(422, UNKNOWN_REGION)
        return JSONResponse(core.commissions_model(region))

    @app.post("/api/commissions/{stage_id}/appeared")
    async def commissions_appeared(stage_id: int) -> Response:
        """Appeared today, once a game day. Its undo is the Achievements page's."""
        return _change_progress(lambda: core.commissions.appeared(stage_id))

    # ------------------------------------------------------------------ timers
    def _change_timer(change: Callable[[], Any], *, counted: bool = False) -> Response:
        """Make a change to the timers and tell the pages, or say why it is refused. `counted` says the change can
        also have changed an achievement's count."""
        try:
            result = change()
        except UnknownTimer as error:
            return _error(404, str(error))
        except TimerConflict as error:
            return _error(409, str(error))
        except (TimerError, RuleError) as error:
            return _error(422, str(error))
        core.timers_changed()
        if counted:
            core.achievements_changed()
        if isinstance(result, tuple):
            timer_id, label = result
            return JSONResponse({"id": timer_id, "change": label})
        return JSONResponse({"change": result})

    @app.get("/api/timers")
    async def timers() -> dict[str, Any]:
        return core.timers_model()

    @app.post("/api/timers")
    async def timers_add(body: dict[str, Any] = Body(...)) -> Response:
        """Add a pre-set, {"preset": key}, or a timer of the player's own: {"name", "place", "rule", "visits"}."""
        if "preset" in body:
            key = body["preset"]
            if not isinstance(key, str):
                return _error(422, UNEXPECTED_REQUEST)
            return _change_timer(lambda: core.timers.add_preset(key))
        name, place = body.get("name"), body.get("place", "")
        if not isinstance(name, str) or not isinstance(place, str):
            return _error(422, UNEXPECTED_REQUEST)
        return _change_timer(lambda: core.timers.create(name, place, parse_rule(body.get("rule")), body.get("visits")))

    @app.put("/api/timers/{timer_id}")
    async def timers_edit(timer_id: int, body: dict[str, Any] = Body(...)) -> Response:
        name, place = body.get("name"), body.get("place", "")
        if not isinstance(name, str) or not isinstance(place, str):
            return _error(422, UNEXPECTED_REQUEST)
        return _change_timer(
            lambda: core.timers.edit(
                timer_id, name=name, place=place, rule=parse_rule(body.get("rule")), steps=body.get("visits")
            )
        )

    @app.post("/api/timers/{timer_id}/bell")
    async def timers_bell(timer_id: int, body: dict[str, Any] = Body(...)) -> Response:
        on = body.get("on")
        if not isinstance(on, bool):
            return _error(422, "Give on as true or false.")
        return _change_timer(lambda: core.timers.set_bell(timer_id, on))

    @app.post("/api/timers/{timer_id}/pin")
    async def timers_pin(timer_id: int, body: dict[str, Any] = Body(...)) -> Response:
        """Put a timer on the Board as a One timer widget, or take it off (spec 9.2). No change to undo: it is a
        change to the Board's layout."""
        on = body.get("on")
        if not isinstance(on, bool):
            return _error(422, "Give on as true or false.")
        if core.store.timer(timer_id) is None:
            return _error(404, NO_TIMER)
        if core.layouts.pin_timer(timer_id, on):
            core.layout_changed("board")
        return JSONResponse({"pinned": on})

    @app.post("/api/timers/{timer_id}/cleared")
    async def timers_cleared(timer_id: int, body: dict[str, Any] = Body(...)) -> Response:
        """Cleared now, or at {"at": time} for Cleared earlier."""
        at = _moment(body["at"]) if "at" in body else None
        if "at" in body and at is None:
            return _error(422, NO_OFFSET)
        return _change_timer(lambda: core.timers.cleared(timer_id, at), counted=True)

    @app.post("/api/timers/{timer_id}/ready")
    async def timers_ready(timer_id: int, body: dict[str, Any] = Body(...)) -> Response:
        at = _moment(body.get("at"))
        if at is None:
            return _error(422, NO_OFFSET)
        return _change_timer(lambda: core.timers.set_ready(timer_id, at))

    @app.delete("/api/timers/{timer_id}")
    async def timers_remove(timer_id: int) -> Response:
        return _change_timer(lambda: core.timers.remove(timer_id))

    # ------------------------------------------------------------------ the Board (spec 13)
    @app.get("/api/board")
    async def board() -> dict[str, Any]:
        return core.board_model()

    # ------------------------------------------------------------------ quick timers (spec 9.5)
    @app.get("/api/quick-timers")
    async def quick_timers() -> dict[str, Any]:
        return core.quick.model()

    @app.post("/api/quick-timers")
    async def quick_timer_start(body: dict[str, Any] = Body(...)) -> Response:
        """Start a quick timer. No change to undo: a quick timer stays until it is dismissed."""
        try:
            quick_id = core.quick.start(body.get("seconds"))
        except QuickTimerError as error:
            return _error(422, str(error))
        core.timers_changed()  # the reminders look again
        return JSONResponse({"id": quick_id})

    @app.delete("/api/quick-timers/{quick_id}")
    async def quick_timer_dismiss(quick_id: int) -> Response:
        try:
            core.quick.dismiss(quick_id)
        except UnknownQuickTimer as error:
            return _error(404, str(error))
        core.timers_changed()
        return JSONResponse({"dismissed": quick_id})

    # ------------------------------------------------------------------ the Map page's tabs (the Map design)
    def _map_answer(change: Callable[[], Any]) -> Response:
        """Runs a change to the tabs and answers with the page's model; the shell and the pages hear of it."""
        try:
            change()
        except UnknownTab as error:
            return _error(404, str(error))
        except MapError as error:
            return _error(422, str(error))
        core.map_changed()
        return JSONResponse(core.map_model())

    @app.get("/api/map")
    async def map_model() -> dict[str, Any]:
        return core.map_model()

    @app.post("/api/map/tabs")
    async def map_add(body: dict[str, Any] = Body(default_factory=dict)) -> Response:
        return _map_answer(lambda: core.maptabs.add(body.get("url")))

    @app.get("/api/map/tabs/{tab_id}")
    async def map_tab(tab_id: int) -> Response:
        """The address of one tab: what Copy link and Open in your browser take."""
        try:
            return JSONResponse({"url": core.maptabs.url_of(tab_id)})
        except UnknownTab as error:
            return _error(404, str(error))

    @app.put("/api/map/tabs/{tab_id}")
    async def map_rename(tab_id: int, body: dict[str, Any] = Body(...)) -> Response:
        if not isinstance(body.get("name"), str):
            return _error(422, UNEXPECTED_REQUEST)
        return _map_answer(lambda: core.maptabs.rename(tab_id, body["name"]))

    @app.delete("/api/map/tabs/{tab_id}")
    async def map_close(tab_id: int) -> Response:
        return _map_answer(lambda: core.maptabs.remove(tab_id))

    @app.post("/api/map/tabs/{tab_id}/duplicate")
    async def map_duplicate(tab_id: int) -> Response:
        return _map_answer(lambda: core.maptabs.duplicate(tab_id))

    @app.post("/api/map/tabs/{tab_id}/reload")
    async def map_reload(tab_id: int) -> Response:
        return _map_answer(lambda: core.maptabs.reload(tab_id))

    @app.post("/api/map/reopen")
    async def map_reopen() -> Response:
        return _map_answer(core.maptabs.reopen)

    @app.put("/api/map/active")
    async def map_activate(body: dict[str, Any] = Body(...)) -> Response:
        if not _whole(body.get("id")):
            return _error(422, UNEXPECTED_REQUEST)
        return _map_answer(lambda: core.maptabs.activate(body["id"]))

    @app.put("/api/map/viewport")
    async def map_viewport(body: dict[str, Any] = Body(...)) -> Response:
        """The area of the page the map fills, or null when the Map page is not showing. Only the shell hears of it."""
        if "area" not in body:
            return _error(422, UNEXPECTED_REQUEST)
        try:
            core.maptabs.set_viewport(body["area"])
        except MapError as error:
            return _error(422, str(error))
        core.map_changed(page=False)
        return JSONResponse({"ok": True})

    # ------------------------------------------------------------------ the Board's layout and its widgets' data
    @app.get("/api/layouts/{place}")
    async def layout(place: str) -> Response:
        try:
            return JSONResponse(core.layouts.get(place))
        except UnknownPlace as error:
            return _error(404, str(error))

    @app.put("/api/layouts/{place}")
    async def layout_save(place: str, body: dict[str, Any] = Body(...)) -> Response:
        """Save a layout over the revision the page read. No change to undo: layouts are not part of that history."""
        revision = body.get("revision")
        if not _whole(revision) or "widgets" not in body:
            return _error(422, UNEXPECTED_REQUEST)
        try:
            saved = core.layouts.save(place, revision, body["widgets"])
        except UnknownPlace as error:
            return _error(404, str(error))
        except LayoutChanged as error:
            return _error(409, str(error))
        except LayoutError as error:
            return _error(422, str(error))
        core.layout_changed(place)
        return JSONResponse(saved)

    @app.get("/api/widgets/{widget_id}/data")
    async def widget_data(widget_id: str) -> dict[str, Any]:
        return {"data": core.layouts.data(widget_id)}

    @app.put("/api/widgets/{widget_id}/data")
    async def widget_data_save(widget_id: str, body: dict[str, Any] = Body(...)) -> Response:
        """Keep a widget's own data. Nothing is published: only the widget reads it."""
        if "data" not in body:
            return _error(422, UNEXPECTED_REQUEST)
        try:
            core.layouts.put_data(widget_id, body["data"])
        except UnknownWidget as error:
            return _error(404, str(error))
        except LayoutError as error:
            return _error(422, str(error))
        return JSONResponse({"data": core.layouts.data(widget_id)})

    @app.post("/api/notifications/test")
    async def notifications_test() -> dict[str, bool]:
        """Send a test notification from the tray icon. False when TUSKFiZZY runs without one, as the core alone."""
        return {"sent": core.notify("test", TEST_TEXT)}

    @app.get("/api/achievements/list")
    async def achievements_list() -> dict[str, Any]:
        return core.lists.meta()

    @app.post("/api/achievements/list/check")
    async def list_check() -> Response:
        try:
            return JSONResponse(await core.lists.check())
        except UpdateError as error:
            return _error(503, str(error))

    def _change_list(change: Callable[[], dict[str, Any]]) -> Response:
        """Install a list or go back to the previous one and tell the pages, or say why it cannot be done."""
        try:
            meta = change()
        except UpdateError as error:
            return _error(409, str(error))
        core.achievements_changed()
        return JSONResponse(meta)

    @app.post("/api/achievements/list/install")
    async def list_install(body: dict[str, Any] = Body(...)) -> Response:
        sha256 = body.get("sha256")
        if not isinstance(sha256, str):
            return _error(422, UNEXPECTED_REQUEST)
        return _change_list(lambda: core.lists.install(sha256))

    @app.post("/api/achievements/list/undo")
    async def list_undo() -> Response:
        return _change_list(core.lists.undo)

    async def _answer_import(run: Callable[[], Awaitable[dict[str, Any]]]) -> Response:
        """The import's summary, or why the file cannot be imported, or that its backup or its writes failed."""
        try:
            return JSONResponse(await run())
        except ImportBackupFailed as error:
            return _error(503, str(error))
        except ImportSaveFailed as error:
            return _error(500, str(error))
        except ImportChanged as error:
            return _error(409, str(error))
        except ImportFileError as error:
            return _error(422, str(error))

    @app.post("/api/tuskfizzy/import")
    async def tuskfizzy_import(body: dict[str, Any] = Body(...)) -> Response:
        """Import the legacy tracker's portable, non-secret export. Validation finishes before the one DB transaction."""
        if body.get("format") != "tuskfizzy-local-data" or body.get("version") != 1:
            return _error(422, "This is not a supported TUSKFiZZY data export.")
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > 10_000_000:
            return _error(413, "The TUSKFiZZY export is larger than 10 MB.")
        accounts = body.get("accounts")
        daily = body.get("dailyRecords")
        genshin = body.get("genshinAccounts")
        selected = body.get("selectedGenshinId", "")
        if not isinstance(accounts, list) or not isinstance(daily, dict) or not isinstance(genshin, list):
            return _error(422, "The export is missing its profiles or daily history.")
        if len(accounts) > 100 or len(genshin) > 100 or len(daily) > 5000:
            return _error(422, "The export contains more profiles or days than TUSKFiZZY can import.")
        if not isinstance(selected, str) or len(selected) > 128:
            return _error(422, "The selected Genshin account is invalid.")
        account_ids: set[str] = set()
        genshin_ids: set[str] = set()
        clean_accounts: list[dict[str, Any]] = []
        clean_genshin: list[dict[str, Any]] = []
        for item in accounts:
            if not isinstance(item, dict):
                return _error(422, "A checklist profile is malformed.")
            ident, name = item.get("id"), item.get("name")
            portrait, genshin_id = item.get("portrait", ""), item.get("genshinId")
            if (not isinstance(ident, str) or not ident or len(ident) > 128 or ident in account_ids
                    or not isinstance(name, str) or not name.strip() or len(name) > 80
                    or not _valid_tuskfizzy_portrait(portrait)
                    or (genshin_id is not None and (not isinstance(genshin_id, str) or len(genshin_id) > 128))):
                return _error(422, "A checklist profile has invalid or duplicate fields.")
            account_ids.add(ident)
            clean_accounts.append({"id": ident, "name": name.strip(), "portrait": portrait,
                                   "genshinId": genshin_id, "createdAt": str(item.get("createdAt", ""))[:40]})
        for item in genshin:
            if not isinstance(item, dict):
                return _error(422, "A Genshin profile is malformed.")
            ident, name, uid = item.get("id"), item.get("name"), item.get("uid")
            server = item.get("server", "")
            if (not isinstance(ident, str) or not ident or len(ident) > 128 or ident in genshin_ids
                    or not isinstance(name, str) or not name.strip() or len(name) > 80
                    or not isinstance(uid, str) or not re.fullmatch(r"[0-9]{9,10}", uid)
                    or not isinstance(server, str) or len(server) > 32):
                return _error(422, "A Genshin profile has invalid or duplicate fields. UIDs must contain 9 or 10 digits.")
            genshin_ids.add(ident)
            clean_genshin.append({"id": ident, "name": name.strip(), "uid": uid, "server": server,
                                  "createdAt": str(item.get("createdAt", ""))[:40]})
        if selected and selected not in genshin_ids:
            return _error(422, "The selected Genshin profile is not in this export.")
        if any(item.get("genshinId") and item["genshinId"] not in genshin_ids for item in clean_accounts):
            return _error(422, "A checklist profile refers to a Genshin account that is not in this export.")
        clean_daily: dict[str, dict[str, list[Any]]] = {}
        for day, profiles in daily.items():
            if not isinstance(day, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", day) or not isinstance(profiles, dict):
                return _error(422, "A daily record has an invalid date or shape.")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                return _error(422, "A daily record has an invalid calendar date.")
            clean_profiles: dict[str, list[Any]] = {}
            resin_meta = profiles.get("_resinLast", {})
            if not isinstance(resin_meta, dict):
                return _error(422, "The resin history metadata is malformed.")
            if any(ident not in account_ids or type(value) not in (int, float) or not 0 <= value <= 300 for ident, value in resin_meta.items()):
                return _error(422, "The resin history metadata refers to an unknown profile or invalid resin value.")
            for ident, flags in profiles.items():
                if ident == "_resinLast":
                    continue
                if ident not in account_ids or not isinstance(flags, list) or len(flags) != 3 or any(type(v) is not bool for v in flags):
                    return _error(422, "A daily record refers to an unknown profile or has invalid checklist values.")
                clean_profiles[ident] = flags
            clean_profiles["_resinLast"] = resin_meta
            clean_daily[day] = clean_profiles
        try:
            counts = core.store.import_tuskfizzy_data(clean_accounts, clean_daily, clean_genshin, selected)
        except (ValueError, TypeError) as error:
            return _error(422, f"The export could not be imported: {error}")
        core.bus.publish("tuskfizzy_imported", counts=counts)
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"imported": counts})

    @app.put("/api/tuskfizzy/selected-genshin")
    async def tuskfizzy_select_genshin(body: dict[str, Any] = Body(...)) -> Response:
        ident = body.get("id")
        if ident is not None and (not isinstance(ident, str) or not ident or len(ident) > 128):
            return _error(422, "Choose a valid Genshin profile.")
        if not core.store.tuskfizzy_select_genshin_account(ident):
            return _error(404, "That Genshin profile no longer exists.")
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"selectedGenshinId": ident})

    @app.post("/api/tuskfizzy/genshin-accounts/{account_id}/login")
    async def tuskfizzy_genshin_login(account_id: str, body: dict[str, Any] = Body(...)) -> Response:
        cookie_text = body.get("cookieText")
        if not isinstance(cookie_text, str):
            return _error(422, "Paste the complete HoYoLAB Cookie header.")
        try:
            account = await core.tuskfizzy_login(account_id, cookie_text)
        except ValueError as error:
            return _error(422, str(error))
        except Exception:
            return _error(503, "HoYoLAB could not verify this profile login. Check the connection and try again.")
        return JSONResponse({"connected": True, "account": account})

    @app.delete("/api/tuskfizzy/genshin-accounts/{account_id}/login")
    async def tuskfizzy_genshin_logout(account_id: str) -> Response:
        try:
            core.tuskfizzy_logout(account_id)
        except ValueError as error:
            return _error(404, str(error))
        return JSONResponse({"connected": False})

    @app.post("/api/tuskfizzy/genshin-accounts")
    async def tuskfizzy_create_genshin_account(body: dict[str, Any] = Body(...)) -> Response:
        name, uid, server = body.get("name"), body.get("uid"), body.get("server", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            return _error(422, "Enter a Genshin profile name of up to 80 characters.")
        if not isinstance(uid, str) or not re.fullmatch(r"[0-9]{9,10}", uid):
            return _error(422, "A Genshin UID must contain 9 or 10 digits.")
        if not isinstance(server, str) or len(server) > 32:
            return _error(422, "Choose a valid Genshin server.")
        ident = secrets.token_hex(12)
        created = datetime.now().astimezone().isoformat(timespec="seconds")
        core.store.tuskfizzy_put_genshin_account(ident, name.strip(), uid, server, created)
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"account": next(item for item in core.store.tuskfizzy_genshin_accounts_list() if item["id"] == ident)})

    @app.put("/api/tuskfizzy/genshin-accounts/{account_id}")
    async def tuskfizzy_update_genshin_account(account_id: str, body: dict[str, Any] = Body(...)) -> Response:
        current = next((item for item in core.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id), None)
        if current is None:
            return _error(404, "That Genshin profile no longer exists.")
        name, uid, server = body.get("name"), body.get("uid"), body.get("server", current["server"])
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            return _error(422, "Enter a Genshin profile name of up to 80 characters.")
        if not isinstance(uid, str) or not re.fullmatch(r"[0-9]{9,10}", uid):
            return _error(422, "A Genshin UID must contain 9 or 10 digits.")
        if not isinstance(server, str) or len(server) > 32:
            return _error(422, "Choose a valid Genshin server.")
        if uid != current["uid"] or server != current["server"]:
            try:
                core.tuskfizzy_logout(account_id)
            except ValueError:
                pass
        core.store.tuskfizzy_put_genshin_account(account_id, name.strip(), uid, server, current["createdAt"])
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"account": next(item for item in core.store.tuskfizzy_genshin_accounts_list() if item["id"] == account_id)})

    @app.delete("/api/tuskfizzy/genshin-accounts/{account_id}")
    async def tuskfizzy_remove_genshin_account(account_id: str) -> Response:
        try:
            core.tuskfizzy_logout(account_id)
        except ValueError:
            return _error(404, "That Genshin profile no longer exists.")
        if not core.store.tuskfizzy_delete_genshin_account(account_id):
            return _error(404, "That Genshin profile no longer exists.")
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"deleted": True})

    @app.get("/api/tuskfizzy/month")
    async def tuskfizzy_month(month: str = Query(...)) -> Response:
        if not re.fullmatch(r"20[0-9]{2}-(?:0[1-9]|1[0-2])", month):
            return _error(422, "Choose a valid month.")
        return JSONResponse({
            "accounts": core.store.tuskfizzy_accounts_list(),
            "records": core.store.tuskfizzy_month_for(month),
        })

    @app.get("/api/tuskfizzy/enka/{uid}")
    async def tuskfizzy_enka(uid: str) -> Response:
        """Read a public Enka showcase for one validated Genshin UID; no HoYoLAB session is sent."""
        if not re.fullmatch(r"[0-9]{9,10}", uid):
            return _error(422, "Genshin UID must contain 9 or 10 digits.")
        try:
            profile = await asyncio.to_thread(enka.get_profile, uid)
        except ValueError:
            return _error(422, "Genshin UID must contain 9 or 10 digits.")
        except enka.EnkaNotFound as error:
            return _error(404, str(error))
        except enka.EnkaUnavailable as error:
            return _error(503, str(error))
        except enka.EnkaError as error:
            return _error(502, str(error))
        return JSONResponse(profile)

    @app.get("/api/tuskfizzy/tracker")
    async def tuskfizzy_tracker(day: str = Query(...)) -> Response:
        if not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", day):
            return _error(422, "Choose a valid calendar date.")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return _error(422, "Choose a valid calendar date.")
        accounts = core.store.tuskfizzy_accounts_list()
        genshin_accounts = [
            {**item, "sessionConnected": core.tuskfizzy_session_connected(item["id"])}
            for item in core.store.tuskfizzy_genshin_accounts_list()
        ]
        daily_note = None
        battle_chronicle = None
        # Each Genshin profile reads from its own encrypted HoYoLAB session. Selecting a profile never
        # borrows the primary Guild Board login or another profile's cookies.
        if day == core.clock.now().astimezone().date().isoformat() and not core.demo:
            selected_genshin_id = core.store.tuskfizzy_selected_genshin_id()
            if selected_genshin_id is not None:
                try:
                    battle_chronicle = await core.tuskfizzy_profile_chronicle(selected_genshin_id)
                except (OSError, RuntimeError, ValueError):
                    battle_chronicle = None
                try:
                    note = await core.tuskfizzy_profile_note(selected_genshin_id)
                except (OSError, RuntimeError, ValueError):
                    note = None
                if note is not None:
                    resin, claimed = note["resin"], note["bonusClaimed"]
                    if type(resin) is int:
                        for linked_profile in accounts:
                            if linked_profile["genshinId"] == selected_genshin_id:
                                core.store.tuskfizzy_observe_daily_note(day, linked_profile["id"], resin, claimed)
                    daily_note = note
        return JSONResponse({
            "accounts": accounts,
            "genshinAccounts": genshin_accounts,
            "selectedGenshinId": core.store.tuskfizzy_selected_genshin_id(),
            "daily": core.store.tuskfizzy_daily_for(day),
            "dailyNote": daily_note,
            "battleChronicle": battle_chronicle,
        })

    @app.post("/api/tuskfizzy/accounts")
    async def tuskfizzy_create_account(body: dict[str, Any] = Body(...)) -> Response:
        name, portrait, genshin_id = body.get("name"), body.get("portrait", ""), body.get("genshinId")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            return _error(422, "Enter a profile name of up to 80 characters.")
        if (not _valid_tuskfizzy_portrait(portrait)):
            return _error(422, "Choose a valid PNG, JPEG, WebP or GIF profile picture smaller than 1 MB.")
        if genshin_id is not None and (not isinstance(genshin_id, str) or len(genshin_id) > 128):
            return _error(422, "Choose a valid linked Genshin profile.")
        if genshin_id and not any(item["id"] == genshin_id for item in core.store.tuskfizzy_genshin_accounts_list()):
            return _error(422, "That Genshin profile is not available.")
        ident = secrets.token_hex(12)
        created = datetime.now().astimezone().isoformat(timespec="seconds")
        core.store.tuskfizzy_put_account(ident, name.strip(), portrait, genshin_id, created)
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"account": next(item for item in core.store.tuskfizzy_accounts_list() if item["id"] == ident)})

    @app.put("/api/tuskfizzy/accounts/{account_id}")
    async def tuskfizzy_update_account(account_id: str, body: dict[str, Any] = Body(...)) -> Response:
        current = next((item for item in core.store.tuskfizzy_accounts_list() if item["id"] == account_id), None)
        if current is None:
            return _error(404, "That TUSKFiZZY profile no longer exists.")
        name, portrait, genshin_id = body.get("name"), body.get("portrait", current["portrait"]), body.get("genshinId", current["genshinId"])
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            return _error(422, "Enter a profile name of up to 80 characters.")
        if not _valid_tuskfizzy_portrait(portrait):
            return _error(422, "Choose a valid PNG, JPEG, WebP or GIF profile picture smaller than 1 MB.")
        if genshin_id is not None and (not isinstance(genshin_id, str) or len(genshin_id) > 128):
            return _error(422, "Choose a valid linked Genshin profile.")
        if genshin_id and not any(item["id"] == genshin_id for item in core.store.tuskfizzy_genshin_accounts_list()):
            return _error(422, "That Genshin profile is not available.")
        core.store.tuskfizzy_put_account(account_id, name.strip(), portrait, genshin_id, current["createdAt"])
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"account": next(item for item in core.store.tuskfizzy_accounts_list() if item["id"] == account_id)})

    @app.delete("/api/tuskfizzy/accounts/{account_id}")
    async def tuskfizzy_remove_account(account_id: str) -> Response:
        if not core.store.tuskfizzy_delete_account(account_id):
            return _error(404, "That TUSKFiZZY profile no longer exists.")
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"deleted": True})

    @app.post("/api/tuskfizzy/observe-note")
    async def tuskfizzy_observe_note(body: dict[str, Any] = Body(...)) -> Response:
        day, account_id = body.get("day"), body.get("accountId")
        resin, claimed = body.get("resin"), body.get("extraRewardClaimed")
        if not isinstance(day, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", day):
            return _error(422, "Choose a valid calendar date.")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return _error(422, "Choose a valid calendar date.")
        if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
            return _error(422, "Choose a valid TUSKFiZZY profile.")
        if type(resin) is not int or not 0 <= resin <= 300 or type(claimed) is not bool:
            return _error(422, "The Daily Note resin or commission status is invalid.")
        if not core.store.tuskfizzy_observe_daily_note(day, account_id, resin, claimed):
            return _error(404, "That TUSKFiZZY profile no longer exists.")
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"saved": True, "daily": core.store.tuskfizzy_daily_for(day)})

    @app.put("/api/tuskfizzy/daily")
    async def tuskfizzy_update_daily(body: dict[str, Any] = Body(...)) -> Response:
        day, account_id, task, checked = body.get("day"), body.get("accountId"), body.get("task"), body.get("checked")
        if not isinstance(day, str) or not re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}", day):
            return _error(422, "Choose a valid calendar date.")
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return _error(422, "Choose a valid calendar date.")
        if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
            return _error(422, "Choose a valid TUSKFiZZY profile.")
        if task not in ("login", "commission", "resin") or type(checked) is not bool:
            return _error(422, "Choose Login, Commission or Resin and a valid checkbox value.")
        if not core.store.tuskfizzy_set_task(day, account_id, task, checked):
            return _error(404, "That TUSKFiZZY profile no longer exists.")
        core.bus.publish("tuskfizzy_changed")
        return JSONResponse({"saved": True, "daily": core.store.tuskfizzy_daily_for(day)})

    @app.post("/api/import/preview")
    async def import_preview(body: dict[str, Any] = Body(...)) -> Response:
        named = _import_file(body)
        if named is None:
            return _error(422, UNEXPECTED_REQUEST)
        return await _answer_import(lambda: core.preview_import(*named))

    @app.post("/api/import/apply")
    async def import_apply(body: dict[str, Any] = Body(...)) -> Response:
        """Import the file that was previewed. Core.apply_import tells the pages itself."""
        named, fingerprint = _import_file(body), body.get("fingerprint")
        if named is None or not isinstance(fingerprint, str):
            return _error(422, UNEXPECTED_REQUEST)
        return await _answer_import(lambda: core.apply_import(*named, fingerprint))

    # ------------------------------------------------------------------ Pulls: the import of the wish history (Part B)
    @app.post("/api/pulls/import")
    async def pulls_import() -> Response:
        """Import the histories, on the player's press: it runs on as a task, and its progress comes as pulls_import
        events. With the HoYoLAB login it is the run of the automatic updates, reading no file of the game's, and the
        game's files only when that cannot run (Core.import_pressed). The demo's runs on its stand-ins. Refused while
        an import or an update runs, and before the first run's consent."""
        try:
            await core.import_pressed()
        except (ImportBusy, ImportNotReady) as error:
            return _error(409, str(error))
        return JSONResponse({"started": True})

    @app.post("/api/demo/fault")
    async def demo_fault(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: make the demo account fail in a chosen way, to show each sync state."""
        if not core.demo:
            return _error(404, "Not found.")
        kind = body.get("kind")
        faults = _demo_faults()
        if kind == "none":
            core.source.fault, core.source.delay = None, 0.0
            core.scheduler.clear_block()
            core.forget_login_refusal()  # the stand-in login is accepted again
        elif kind == "slow":
            core.source.fault, core.source.delay = None, SLOW_DEMO_SECONDS
        elif isinstance(kind, str) and kind in faults:
            core.source.fault, core.source.delay = faults[kind], 0.0
        else:
            return _error(422, "Unknown fault.")
        return JSONResponse({"fault": kind})

    @app.post("/api/demo/online")
    async def demo_online(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: put the made-up account in a state, to show each page in it."""
        if not core.demo:
            return _error(404, "Not found.")
        state = body.get("state")
        if state not in DEMO_ONLINE_STATES:
            return _error(422, UNEXPECTED_REQUEST)
        core.online.demo_state(state)
        return JSONResponse(core.online.view())

    @app.post("/api/demo/refresh")
    async def demo_refresh() -> Response:
        """Demo mode only: update the histories with the demo's stand-in login now, as the core does every half hour."""
        if not core.demo:
            return _error(404, "Not found.")
        core.wake_login_refresh()
        return JSONResponse({"woken": True})

    @app.post("/api/demo/window")
    async def demo_window(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: say whether the window is maximized, as the shell does, so that a page without the window shows
        the Board's arrangement for a maximized one."""
        if not core.demo:
            return _error(404, "Not found.")
        maximized = body.get("maximized")
        if not isinstance(maximized, bool):
            return _error(422, "Give maximized as true or false.")
        core.window_state(maximized)
        return JSONResponse({"maximized": maximized})

    @app.post("/api/demo/clock")
    async def demo_clock(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: move the demo clock forward."""
        if not core.demo:
            return _error(404, "Not found.")
        seconds = body.get("advance_seconds")
        valid = isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
        if not valid or not 0 <= seconds <= MAX_CLOCK_STEP_SECONDS:
            return _error(422, f"Give advance_seconds as a number from 0 to {MAX_CLOCK_STEP_SECONDS}.")
        return JSONResponse({"now": iso(core.advance_demo_clock(seconds))})

    @app.post("/api/demo/update")
    async def demo_update(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: show the notice of a newer TUSKFiZZY in one of its states."""
        if not core.demo:
            return _error(404, "Not found.")
        state = body.get("state")
        if state not in ("none", "available", "downloading", "installing", "failed"):
            return _error(422, UNEXPECTED_REQUEST)
        core.demo_update(state)
        return JSONResponse({"state": state})

    @app.post("/api/demo/history-stopped")
    async def demo_history_stopped(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: show the notice that the game's history stopped updating."""
        if not core.demo:
            return _error(404, "Not found.")
        if body.get("how") not in ("failing", "login", "feedback"):
            return _error(422, UNEXPECTED_REQUEST)
        core.demo_history_stopped(body["how"])
        return JSONResponse({"shown": True})

    @app.post("/api/demo/whats-new")
    async def demo_whats_new() -> Response:
        """Demo mode only: show the list of changes that follows an update."""
        if not core.demo:
            return _error(404, "Not found.")
        core.demo_whats_new()
        return JSONResponse({"shown": True})

    @app.post("/api/demo/line")
    async def demo_line(body: dict[str, Any] = Body(...)) -> Response:
        """Demo mode only: show a line over the game, as a timer that is ready does while the game runs."""
        if not core.demo:
            return _error(404, "Not found.")
        text = body.get("text")
        if not isinstance(text, str):
            return _error(422, UNEXPECTED_REQUEST)
        text = valid_text(text)  # the page's JSON can carry half of an emoji, which no answer can hold
        core.overlay_line(text)
        return JSONResponse({"text": text})

    # ------------------------------------------------------------------ Pulls: the model, the settings and the wishlist
    def _change_pulls(change: Callable[[], Any], answer: Callable[[Any], Any]) -> Response:
        """Make a change to what the Pulls page keeps and tell the pages, or say why it is refused."""
        try:
            result = change()
        except UnknownEntry as error:
            return _error(404, str(error))
        except PullsConflict as error:
            return _error(409, str(error))
        except PullsRefused as error:
            return _error(422, str(error))
        core.pulls_changed()
        return JSONResponse(answer(result))

    @app.get("/api/pulls")
    async def pulls(by: str | None = None) -> Response:
        """The Pulls page's model, with the forecast by the day `by` (2026-11-11), else by the running banners' end. Its
        import block is the importer's own: this session's run while it runs and after, else the newest run when Guild
        Board's closing cut it short, else the last that ended well."""
        day = as_day(by) if by is not None else None
        if by is not None and day is None:
            return _error(422, UNEXPECTED_REQUEST)
        try:
            return JSONResponse({**core.pulls.model(day), "import": core.wish_import.state()})
        except PullsRefused as error:
            return _error(422, str(error))

    @app.get("/api/pulls/history")
    async def pulls_history(before: int | None = None, limit: int = HISTORY_ROUTE_ROWS) -> Response:
        """The history's rows, newest first, older than the wish `before` when it is given; 1 to 500 of them."""
        if before is not None and not 0 <= before < 2**63:  # past what SQLite keeps, and so past every id
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(core.pulls.history(before, max(1, min(limit, MAX_HISTORY_ROWS))))

    @app.get("/api/ledger")
    async def ledger() -> Response:
        """The Ledger page: spending, this month's primogems, the last 30 days' artifacts, and the import's state."""
        return JSONResponse(core.ledger())

    @app.get("/api/ledger/history")
    async def ledger_history(kind: str, before: str | None = None, limit: int = LEDGER_ROWS) -> Response:
        """A kind's entries, newest first, older than the entry `before` (its id, as text) when it is given; 1 to 100."""
        if kind not in LEDGER_KINDS:
            return _error(422, UNEXPECTED_REQUEST)
        older: int | None = None
        if before is not None:
            if not (before.isascii() and before.isdigit() and len(before) <= 19 and int(before) < 2**63):
                return _error(422, UNEXPECTED_REQUEST)
            older = int(before)
        return JSONResponse(core.ledger_history(kind, older, max(1, min(limit, MAX_LEDGER_ROWS))))

    @app.put("/api/pulls/settings")
    async def pulls_settings(body: dict[str, Any] = Body(...)) -> Response:
        """The charted weapon, the chronicled path, the corrections and the Fate Points; the answer is the pity cards."""
        if not _pulls_settings(body):
            return _error(422, UNEXPECTED_REQUEST)
        return _change_pulls(lambda: core.pulls.set_settings(body), lambda _: core.pulls.model()["pity"])

    @app.put("/api/pulls/balance")
    async def pulls_balance(body: dict[str, Any] = Body(...)) -> Response:
        """The primogems and fates the player typed, set now; the answer is the savings."""
        return _change_pulls(
            lambda: core.pulls.set_balance(body.get("primogems"), body.get("fates")),
            lambda _: core.pulls.model()["savings"],
        )

    @app.get("/api/pulls/choices")
    async def pulls_choices() -> Response:
        """What Add a wish offers: the characters and weapons that can be wished for, each with its face and group."""
        return JSONResponse(core.pulls.choices())

    @app.post("/api/pulls/wishlist")
    async def pulls_wishlist_add(body: dict[str, Any] = Body(...)) -> Response:
        """Add an entry to the wishlist; its icon is read from the wiki afterwards when the wiki's lists lack it."""

        def add() -> int:
            entry_id = core.pulls.add_wish(body.get("name"), body.get("kind"), body.get("about"), body.get("copies"))
            core.icons_wanted()
            return entry_id

        return _change_pulls(add, lambda entry_id: {"id": entry_id})

    @app.put("/api/pulls/wishlist/{entry_id}")
    async def pulls_wishlist_edit(entry_id: int, body: dict[str, Any] = Body(...)) -> Response:
        changes = {name: body[name] for name in ENTRY_FIELDS if name in body}
        return _change_pulls(lambda: core.pulls.edit_wish(entry_id, **changes), lambda _: {"id": entry_id})

    @app.delete("/api/pulls/wishlist/{entry_id}")
    async def pulls_wishlist_remove(entry_id: int) -> Response:
        if not core.pulls.remove_wish(entry_id):
            return _error(404, NO_ENTRY)
        core.pulls_changed()
        return JSONResponse({"id": entry_id})

    # ------------------------------------------------------------------ Characters: the pages, the plans, the week and the art
    def _change_characters(change: Callable[[], Any], answer: Callable[[], Any]) -> Response:
        """Make a change to what the Characters pages keep and tell the pages, or say why it is refused."""
        try:
            change()
        except UnknownCharacter:
            return _error(404, NO_SUCH_CHARACTER)
        except CharactersRefused as error:
            return _error(422, str(error))
        core.characters_changed()
        return JSONResponse(answer())

    def _week_and_endgame_hidden() -> Response | None:
        """Your week and Endgame are hidden while `characters.WEEK_AND_ENDGAME` is off: their routes are not there."""
        return None if characters.WEEK_AND_ENDGAME else _error(404, "Your week and Endgame are turned off.")

    def _character(make: Callable[[str], Any], key: str) -> Response:
        core.characters.wanted(key)  # the wiki's entries it needs are read in the background, after consent
        try:
            return JSONResponse(make(key))
        except UnknownCharacter:
            return _error(404, NO_SUCH_CHARACTER)

    @app.get("/api/characters")
    async def characters_roster() -> Response:
        core.characters.wanted()
        return JSONResponse(core.characters.roster())

    @app.put("/api/characters/held")
    async def characters_held(body: dict[str, Any] = Body(...)) -> Response:
        """The amounts the player typed as held, by material key; null takes one away."""
        return _change_characters(lambda: core.characters.put_held(body.get("amounts")), lambda: {"ok": True})

    @app.post("/api/characters/refresh")
    async def characters_refresh() -> Response:
        """Ask for a read of the builds and the calculator now, within the sync's limits."""
        answer = core.refresh_characters()
        if answer.get("started"):
            return JSONResponse({"started": True})
        if "wait_minutes" in answer:
            # Once the day's runs are spent, the wait is until the count starts again, even within the gap after the last.
            until_count = core.scheduler.seconds_until_runs_count_again()
            minutes = max(1, math.ceil(until_count / 60)) if until_count else answer["wait_minutes"]
            again = f"Try again in {minutes} minute{'' if minutes == 1 else 's'}."
            if until_count:
                return _error(409, f"TUSKFiZZY has read HoYoLAB {DAILY_CAP} times today, the most it reads in a day. {again}")
            return _error(409, f"TUSKFiZZY read HoYoLAB a moment ago. {again}")
        if answer.get("login") or answer.get("blocked") in ("login_needed", None):
            return _error(409, "Log in to HoYoLAB first.")
        if answer["blocked"] == "paused":
            return _error(409, "Syncing is paused. Resume it in Settings.")
        return _error(409, "HoYoLAB asked for a verification check. Finish it from the sync panel first.")

    @app.get("/api/characters/{key}")
    async def characters_profile(key: str) -> Response:
        return _character(core.characters.profile, key)

    @app.get("/api/characters/{key}/plan")
    async def characters_plan(key: str) -> Response:
        return _character(core.characters.planner, key)

    @app.put("/api/characters/{key}/plan")
    async def characters_put_plan(key: str, body: dict[str, Any] = Body(...)) -> Response:
        return _change_characters(lambda: core.characters.put_plan(key, body), lambda: core.characters.planner(key))

    @app.delete("/api/characters/{key}/plan")
    async def characters_drop_plan(key: str) -> Response:
        return _change_characters(lambda: core.characters.drop_plan(key), lambda: core.characters.planner(key))

    @app.put("/api/characters/{key}/card")
    async def characters_put_card(key: str, body: dict[str, Any] = Body(...)) -> Response:
        return _change_characters(lambda: core.characters.put_card(key, body), lambda: core.characters.profile(key))

    @app.delete("/api/characters/{key}/card")
    async def characters_drop_card(key: str) -> Response:
        return _change_characters(lambda: core.characters.drop_card(key), lambda: core.characters.profile(key))

    @app.put("/api/focus")
    async def characters_focus(body: dict[str, Any] = Body(...)) -> Response:
        # The answer is the week's, which the Your week page takes; with the week hidden it is the roster.
        answer = core.characters.week if characters.WEEK_AND_ENDGAME else core.characters.roster
        return _change_characters(lambda: core.characters.put_focus(body.get("characters")), answer)

    @app.get("/api/week")
    async def characters_week() -> Response:
        if (hidden := _week_and_endgame_hidden()) is not None:
            return hidden
        core.characters.wanted(*core.characters.focus())
        return JSONResponse(core.characters.week())

    @app.put("/api/week/ticks")
    async def characters_tick(body: dict[str, Any] = Body(...)) -> Response:
        if (hidden := _week_and_endgame_hidden()) is not None:
            return hidden
        return _change_characters(
            lambda: core.characters.put_tick(body.get("day"), body.get("task"), body.get("done")), core.characters.week
        )

    @app.get("/api/endgame")
    async def characters_endgame(after: int = 0) -> Response:
        if (hidden := _week_and_endgame_hidden()) is not None:
            return hidden
        if after not in (0, 1):
            return _error(422, UNEXPECTED_REQUEST)
        return JSONResponse(core.characters.endgame(bool(after)))

    @app.put("/api/endgame/theater")
    async def characters_theater(body: dict[str, Any] = Body(...)) -> Response:
        if (hidden := _week_and_endgame_hidden()) is not None:
            return hidden
        return _change_characters(
            lambda: core.characters.put_theater(body.get("elements")), lambda: core.characters.endgame(False)
        )

    @app.delete("/api/endgame/theater")
    async def characters_theater_back() -> Response:
        """The player's pick for the month goes, and the calendar's elements show again."""
        if (hidden := _week_and_endgame_hidden()) is not None:
            return hidden
        return _change_characters(core.characters.drop_theater, lambda: core.characters.endgame(False))

    @app.get("/api/art/{key}")
    async def characters_art(key: str, size: str | None = None) -> Response:
        """A picture of the Characters pages, from the art cache; nothing is fetched before the player's consent. With `size`,
        one of the sizes that are kept (characters/art.py), it is a copy made for display: a page can ask for no other work."""
        if size is not None and size not in {str(kept) for kept in DISPLAY_SIZES}:
            return _error(422, UNEXPECTED_REQUEST)
        found = (
            await core.art.get(key, None if size is None else int(size)) if core.demo or core.settings.risk_acknowledged else None
        )
        if found is None:
            return _error(404, "There is no picture for this.")
        data, media_type = found
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "max-age=86400"})

    # The screenshot gallery (the ledger design, section 7). An id is a file name of the current listing and nothing
    # else: anything else, and every id while the gallery is off, is a 404 that opens no file.
    @app.get("/api/gallery")
    async def gallery() -> Response:
        return JSONResponse(await core.gallery_model())

    @app.post("/api/gallery/rescan")
    async def gallery_rescan() -> Response:
        return JSONResponse(await core.gallery_model(fresh=True))

    @app.get("/api/gallery/{picture_id}/thumb")
    async def gallery_thumb(picture_id: str) -> Response:
        try:
            data = await asyncio.to_thread(core.gallery.thumb, picture_id)
        except PictureMissing:
            return _error(404, NO_PICTURE)
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @app.get("/api/gallery/{picture_id}/full")
    async def gallery_full(picture_id: str) -> Response:
        try:
            data, media_type = await asyncio.to_thread(core.gallery.full, picture_id)
        except PictureMissing:
            return _error(404, NO_PICTURE)
        return Response(content=data, media_type=media_type, headers={"Cache-Control": "no-cache"})

    @app.put("/api/gallery/{picture_id}/favourite")
    async def gallery_favourite(picture_id: str, body: dict[str, Any] = Body(...)) -> Response:
        on = body.get("on")
        if not isinstance(on, bool):
            return _error(422, UNEXPECTED_REQUEST)
        try:
            core.gallery.favourite(picture_id, on)
        except PictureMissing:
            return _error(404, NO_PICTURE)
        return JSONResponse({"id": picture_id, "favourite": on})

    @app.post("/api/gallery/{picture_id}/show")
    async def gallery_show(picture_id: str) -> Response:
        try:
            core.gallery.show(picture_id)
        except PictureMissing:
            return _error(404, NO_PICTURE)
        except OSError:
            return _error(409, "Explorer could not be opened.")
        return JSONResponse({"shown": picture_id})

    if web_dir is not None and (web_dir / "index.html").is_file():
        for extension, media_type in STATIC_TYPES.items():
            mimetypes.add_type(media_type, extension)
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    else:

        @app.get("/")
        async def placeholder() -> HTMLResponse:
            return HTMLResponse(PLACEHOLDER_PAGE)

    return app
