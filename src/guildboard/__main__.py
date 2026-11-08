"""Command line entry point."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import truststore
import uvicorn

from guildboard import characters, netpolicy
from guildboard.api.app import create_app
from guildboard.cloud import invite_code
from guildboard.config import AppPaths, default_data_dir
from guildboard.core import Core
from guildboard.logging_setup import setup_logging, shutdown_logging
from guildboard.store import StoreError


_TLS_READY = False
NO_SHELL_TEXT = (
    "TUSKFiZZY's window is its shell. Start it with guildboard-app.exe, or with npm start --prefix shell in a checkout."
)


def use_the_windows_store() -> None:
    """Have every TLS connection of this process verified through Windows' own chain building, once, before any request. The
    certificates OpenSSL reads from the store are those the PC holds already; Windows fetches a root it lacks on the way, and a
    fresh PC could not reach HoYoverse until another program had made it fetch one. The frozen core calls this too: it is
    the one entry (packaging/core_entry.py runs main). Where truststore cannot, the default roots stay."""
    global _TLS_READY
    if _TLS_READY:
        return
    _TLS_READY = True
    try:
        truststore.inject_into_ssl()
        netpolicy.take_the_tls_in_use()
    except Exception:  # a connection then fails with its own reason, which the player is told
        logging.getLogger(__name__).warning("The Windows certificate store could not be used for TLS", exc_info=True)


def default_web_dir() -> Path:
    """Where the built interface lives: <project>/web/dist, or web/dist in the installed copy's own folder."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "web" / "dist"
    return Path(__file__).resolve().parents[2] / "web" / "dist"


def _announce(link: str) -> None:
    """Print the launch link at once. A program that started TUSKFiZZY reads it from a pipe, where
    output would otherwise be held back until the process ends."""
    print(link, flush=True)


async def _stop_when(stop: asyncio.Event, server: uvicorn.Server, core: Core) -> None:
    await stop.wait()
    core.bus.publish("shutdown")  # ends the open event streams, which would keep the server waiting
    server.should_exit = True


async def run_headless(
    paths: AppPaths,
    *,
    demo: bool,
    port: int = 0,
    ready: Callable[[str], None] = _announce,
    stop: asyncio.Event | None = None,
    demo_now: datetime | None = None,
    invite: str | None = None,
) -> int:
    """Run the core alone, serving the API on 127.0.0.1, until interrupted or `stop` is set. `invite` is the code of an
    invite link this start was opened with."""
    core = Core(paths, demo=demo, demo_now=demo_now)
    if invite is not None:
        await core.online.offer_invite(invite)
    await serve(core, port=port, web_dir=default_web_dir(), ready=ready, stop=stop)
    return 0


async def serve(
    core: Core,
    *,
    port: int = 0,
    web_dir: Path | None = None,
    ready: Callable[[str], None] = _announce,
    stop: asyncio.Event | None = None,
) -> None:
    """Start the core and serve the API and the interface on 127.0.0.1 until `stop` is set; then stop
    the core. `ready` receives the launch link once connections are accepted."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    watcher: asyncio.Task[None] | None = None
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(128)  # from here on a connection is queued, so the link can be announced
        bound = listener.getsockname()[1]
        app = create_app(core, bound, web_dir)
        # log_config=None: the server's own messages go to TUSKFiZZY's log file, not only to the console.
        # timeout_graceful_shutdown: a connection that stays open cannot keep TUSKFiZZY from closing.
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", log_config=None, timeout_graceful_shutdown=3))
        core.start()
        ready(f"http://127.0.0.1:{bound}/launch?k={core.launch_secret}")
        if stop is not None:
            watcher = asyncio.create_task(_stop_when(stop, server, core))
        await server.serve(sockets=[listener])
    finally:
        if watcher is not None:
            watcher.cancel()
        await core.stop()
        listener.close()


def _run_for_shell(paths: AppPaths, *, demo: bool, demo_now: datetime | None) -> int:
    """The core for a shell that started it (shellchannel). The channel writes to a copy of the standard output handle,
    with UTF-8 and a newline alone whatever Windows does to a console's output. The program's own `sys.stdout` is never
    written to, so that a shell that has gone, with a broken pipe, cannot make the flush at Python's exit fail."""
    from guildboard.shellchannel import run_core_for_shell

    setup_logging(paths.logs_dir)
    channel_out = open(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    try:
        return asyncio.run(run_core_for_shell(
            paths, demo=demo, demo_now=demo_now, stdin=sys.stdin.buffer, stdout=channel_out,
        ))
    finally:
        try:
            channel_out.close()
        except (OSError, ValueError):  # the pipe is broken: what was left to write has nowhere to go
            pass


def main(argv: Sequence[str] | None = None) -> int:
    use_the_windows_store()
    parser = argparse.ArgumentParser(prog="guildboard", description="TUSKFiZZY")
    parser.add_argument("link", nargs="?", default=None, help="an invite link, which Windows hands over when one is opened")
    parser.add_argument("--demo", action="store_true", help="run on a synthetic account, with no network access")
    parser.add_argument("--no-shell", action="store_true", help="run the core alone and print a launch link")
    parser.add_argument(
        "--shell", action="store_true",
        help="run the core for a shell that starts it: JSON lines on standard input and output, and no window",
    )
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: any free port)")
    parser.add_argument("--data-dir", type=Path, default=None, help="folder for the database, settings and logs")
    parser.add_argument(
        "--demo-now", default=None,
        help="with --demo: the instant the demo clock starts at, for example 2026-10-13T08:00:00+00:00",
    )
    parser.add_argument(
        "--week-and-endgame", action="store_true",
        help="with --demo: show Your week and Endgame, which are hidden (the browser tests run with them shown)",
    )
    args = parser.parse_args(argv)
    if args.shell and args.no_shell:
        print("--shell and --no-shell cannot be used together.")
        return 2
    if not args.shell and not args.no_shell:
        # The window is the Electron shell's, and it starts the core itself with --shell. A start with neither switch has
        # no window to show, and makes nothing: not even the data folder.
        print(NO_SHELL_TEXT)
        return 2
    if args.week_and_endgame and not args.demo:
        print("--week-and-endgame needs --demo.")
        return 2
    if args.week_and_endgame:
        characters.WEEK_AND_ENDGAME = True
    demo_now = None
    if args.demo_now is not None:
        if not args.demo:
            print("--demo-now needs --demo.")
            return 2
        try:
            demo_now = datetime.fromisoformat(args.demo_now)
        except ValueError:
            demo_now = None
        if demo_now is None or demo_now.tzinfo is None:
            print("--demo-now needs a date and time with a time zone, for example 2026-10-13T08:00:00+00:00.")
            return 2
    # Anything else in its place is ignored: another program can start TUSKFiZZY with any argument.
    invite = invite_code(args.link) if args.link is not None else None
    paths = AppPaths(args.data_dir or default_data_dir(demo=args.demo))
    paths.ensure()
    try:
        if args.shell:
            return _run_for_shell(paths, demo=args.demo, demo_now=demo_now)
        setup_logging(paths.logs_dir)
        return asyncio.run(run_headless(paths, demo=args.demo, port=args.port, demo_now=demo_now, invite=invite))
    except KeyboardInterrupt:
        return 0
    except StoreError as error:
        print(str(error))
        return 1
    except Exception:
        # The log closes below, before Python reports the error, so it is written here; under pythonw nothing else
        # would show it.
        logging.getLogger("guildboard").exception("TUSKFiZZY stopped on an error nothing expected")
        raise
    finally:
        shutdown_logging()


if __name__ == "__main__":
    sys.exit(main())
