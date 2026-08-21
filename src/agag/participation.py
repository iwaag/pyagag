"""Where a run has posted, and which conversation it was serving when it did.

A run is one reply. It says something somewhere and ends; when an answer
arrives it is served again, with that conversation in front of it. For that
to work something has to remember, across the end of the run, that *this*
agent is party to *that* conversation and on behalf of which of its own.

That memory is this ledger: one JSON object per line, appended by
`agentchat send`, read by the listener when a mention arrives.

  {"remote": "<channel>/<topic>", "home": "<channel>/<topic>",
   "message_id": 123, "at": "2026-08-21T09:00:00+00:00"}

`remote` is where the run posted. `home` is the conversation the run was
serving — `AGENTCHAT_HOME` in its environment, put there by the listener that
started it. So a reply arriving in `remote` names the topic to serve, and the
run that is started sees the remote thread beside its own.

String operations and a file. Nothing here calls a model, and nothing here
decides anything: the ledger records what happened, and the listener's own
rules decide what to do about it.

A conversation is written as `<channel>/<topic>` — the same shape the topic
workspaces use, so a channel name with a `/` in it is out of scope here as it
is there.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: The conversation a run is serving. The listener sets it; `agentchat`
#: reads it. A run without it posts without being recorded — which is
#: correct for a run nobody will call back.
HOME_VARIABLE = "AGENTCHAT_HOME"
#: Where the ledger file is. Set by the listener so the file belongs to the
#: agent rather than to whatever directory a run happened to start in.
LEDGER_VARIABLE = "AGENTCHAT_LEDGER"
#: Used when `AGENTCHAT_LEDGER` is unset, relative to the run's own directory.
DEFAULT_LEDGER = Path(".local") / "agentchat" / "participations.jsonl"

__all__ = [
    "DEFAULT_LEDGER",
    "HOME_VARIABLE",
    "LEDGER_VARIABLE",
    "Conversation",
    "entries",
    "home_for",
    "home_from_environment",
    "ledger_from_environment",
    "parse_conversation",
    "record",
    "remotes_for_home",
]


@dataclass(frozen=True)
class Conversation:
    """One channel/topic pair — Zulip's unit of conversation."""

    channel: str
    topic: str

    def __str__(self) -> str:
        return f"{self.channel}/{self.topic}"

    def as_pair(self) -> tuple[str, str]:
        return (self.channel, self.topic)


def parse_conversation(value: str | None) -> Conversation | None:
    """`"<channel>/<topic>"` as a `Conversation`, or None when it is not one.

    Split on the *first* separator: a topic may contain slashes, a channel
    may not — the same rule the topic workspaces already live by.
    """
    text = (value or "").strip()
    if "/" not in text:
        return None
    channel, topic = text.split("/", 1)
    channel, topic = channel.strip(), topic.strip()
    if not channel or not topic:
        return None
    return Conversation(channel, topic)


def home_from_environment(environ=None) -> Conversation | None:
    """The conversation this run is serving, per `AGENTCHAT_HOME`."""
    environ = os.environ if environ is None else environ
    return parse_conversation(environ.get(HOME_VARIABLE))


def ledger_from_environment(environ=None) -> Path:
    """The ledger file, per `AGENTCHAT_LEDGER` or the default path."""
    environ = os.environ if environ is None else environ
    reference = (environ.get(LEDGER_VARIABLE) or "").strip()
    if reference:
        return Path(os.path.expanduser(reference))
    return DEFAULT_LEDGER


def record(
    path: Path,
    *,
    remote: Conversation,
    home: Conversation,
    message_id: int,
    at: str | None = None,
) -> dict:
    """Append one participation and return it.

    Append-only, one line, created with its parents: a ledger that a crash
    can truncate to a whole number of intact lines is the whole durability
    story this needs.
    """
    entry = {
        "remote": str(remote),
        "home": str(home),
        "message_id": int(message_id),
        "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def entries(path: Path) -> list[dict]:
    """Every readable line of the ledger, oldest first.

    A line that is not JSON is skipped rather than fatal: a half-written last
    line must not cost an agent every conversation it is part of.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def home_for(path: Path, channel: str, topic: str) -> Conversation | None:
    """Which of our own conversations this remote one was opened for.

    The most recent entry wins: the same remote topic can be reused, and what
    matters is the run that is actually waiting on it now.
    """
    remote = f"{channel}/{topic}"
    for row in reversed(entries(path)):
        if row.get("remote") == remote:
            return parse_conversation(row.get("home"))
    return None


def remotes_for_home(path: Path, channel: str, topic: str) -> list[Conversation]:
    """Every conversation this one has reached out to, in first-posted order.

    This is the list of threads a run serving `<channel>/<topic>` is party to,
    which is what decides the `threads/` folder it gets.
    """
    home = f"{channel}/{topic}"
    found: list[Conversation] = []
    for row in entries(path):
        if row.get("home") != home:
            continue
        remote = parse_conversation(row.get("remote"))
        if remote is not None and remote not in found:
            found.append(remote)
    return found
