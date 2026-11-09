"""TUSKFiZZY's online account (the online design): one Supabase project, reached only by cloud/client.py, and only after
the player signs in. Row level security decides who can read what; everything local works without it."""
from __future__ import annotations

import re

from guildboard import netpolicy

# The project's publishable key, public by design: it names the project, and row level security protects the data. A fork
# changes this and netpolicy.CLOUD_HOST, its two settings, which are constants so that no file can redirect the session.
CLOUD_KEY = "sb_publishable_26S4cv0tPoejhONXbnfyAg_0NWJok8P"
SITE = "https://" + netpolicy.CLOUD_HOST
# What a player can share, one switch each (the online design, section 6). Status is a switch but no stat.
SHARE_KEYS = ("status", "primogems", "playtime", "achievements", "endgame", "account", "wishes", "spending")
STAT_KEYS = SHARE_KEYS[1:]
PIN_ORDER = ("primogems", "playtime", "endgame", "achievements", "account", "wishes", "spending")
# The address the invite function sends a browser to, which Windows hands to TUSKFiZZY. Its code has the function's pattern.
INVITE = re.compile(r"guildboard://invite/([A-Za-z0-9_-]{16,64})")


def invite_code(link: str) -> str | None:
    """The code of an invite link, or None for anything else: another program can start TUSKFiZZY with any argument."""
    found = INVITE.fullmatch(link)
    return found.group(1) if found is not None else None
