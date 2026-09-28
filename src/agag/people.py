"""Who acts with whose authority in this realm (failsafe p6 ex2).

A decision about work — accepting it, holding it, releasing a hold, answering
a question put to a person — belongs to a person, and the checks that guard
those decisions compare user ids. One realm account is not a person of its
own: the **Omni Agent** works on the system from outside with the
Developer's full delegated authority. Its instructions, approvals,
cancellations and hold releases carry the Developer's weight, and nobody
needs the Developer's own confirmation of them. p6 ex1's trial L3 is what
happened without a shared statement of that: Front took the Omni Agent's
confirmation twice and refused the same one the third time, and the
credential was switched to the Developer's account to get past it — which
made the record say the Developer spoke.

This module is the one statement, read by every check that compares a
speaker with a decision holder (`agag.acceptance`, `agag.holds`,
`agag.outstanding`) and by the chatlog a run reads
(`agag.topics.format_chatlog`), so guidance and checks cannot disagree. It
lives in the host's `~/.config/agag/people.toml` (or
`$XDG_CONFIG_HOME/agag/people.toml`; `AGAG_PEOPLE_CONFIG` names another
file), beside `refs.toml`, because every process that makes these checks
runs on the host and user ids are realm facts, not code:

    [[proxy]]
    user = 9            # the account that speaks
    name = "Omni Agent"
    for = 8             # whose full authority it carries
    for_name = "Developer"

**Equal authority is not the same identity.** Nothing here aliases one id to
another: a post is still the post of whoever wrote it, replies still go to
the sender, and a record names the actual speaker (`by 9 (Omni Agent)`)
with the authority it acted on (`for 8 (Developer)`) — never the principal
as if they had spoken. A post under the Developer's account proves the
account, not that a human typed it.

Authority runs both ways: a proxy may make any decision its principal holds,
and the principal any decision its proxy holds (the Developer may release a
hold the Omni Agent placed). Ordinary agents are never proxies: a worker
still cannot accept its own work. A missing or unreadable file names no
proxy, so every check falls back to plain id equality.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CONFIG_NAME",
    "CONFIG_VARIABLE",
    "Proxy",
    "config_path",
    "proxies",
    "principal_of",
    "acts_for",
    "for_suffix",
    "speaker_label",
]

CONFIG_NAME = "people.toml"
CONFIG_VARIABLE = "AGAG_PEOPLE_CONFIG"


@dataclass(frozen=True)
class Proxy:
    user: int
    name: str
    principal: int
    principal_name: str


def config_path(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    value = str(environ.get(CONFIG_VARIABLE, "")).strip()
    if value:
        return Path(value).expanduser()
    base = str(environ.get("XDG_CONFIG_HOME", "")).strip()
    return (Path(base).expanduser() if base else Path.home() / ".config") / "agag" / CONFIG_NAME


_cache: dict[str, tuple[float, dict[int, Proxy]]] = {}


def proxies(environ=None) -> dict[int, Proxy]:
    """Proxy user id → `Proxy`, re-read whenever the file changes."""
    path = config_path(environ)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    cached = _cache.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    found: dict[int, Proxy] = {}
    for row in data.get("proxy", []) or []:
        try:
            user, principal = int(row["user"]), int(row["for"])
        except (KeyError, TypeError, ValueError):
            continue
        if user and principal and user != principal:
            found[user] = Proxy(user, str(row.get("name") or ""), principal, str(row.get("for_name") or ""))
    _cache[str(path)] = (stamp, found)
    return found


def principal_of(user_id) -> Proxy | None:
    """The `Proxy` entry when `user_id` carries somebody's full authority."""
    return proxies().get(int(user_id or 0))


def acts_for(speaker, holder) -> bool:
    """`speaker` may make a decision `holder` holds: the same person, or one
    carrying the other's full authority (either way round)."""
    speaker, holder = int(speaker or 0), int(holder or 0)
    if not speaker or not holder:
        return False
    if speaker == holder:
        return True
    table = proxies()
    return (speaker in table and table[speaker].principal == holder) or (
        holder in table and table[holder].principal == speaker)


def for_suffix(speaker, holder, holder_name: str = "") -> str:
    """` for <holder> (<name>)` when `speaker` decided on `holder`'s
    authority rather than as `holder`; "" when they are the same person or
    unrelated."""
    if int(speaker or 0) == int(holder or 0) or not acts_for(speaker, holder):
        return ""
    return f" for {int(holder)}" + (f" ({holder_name})" if holder_name else "")


def speaker_label(user_id, name: str) -> str:
    """How a chatlog names a speaker: a proxy says whose authority it carries,
    so a run reads it on every line rather than deciding it afresh."""
    entry = principal_of(user_id)
    if entry is None:
        return name
    return f"{name} — with {entry.principal_name or entry.principal}'s full authority"
