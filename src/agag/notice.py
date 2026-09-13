"""Notices: the fixed line that says a watch has an outcome, and waiting for it.

A watcher (agobserver) tells a requester that its condition holds by posting
into the requester's conversation. That post is **speech** — it is the
requester's turn, and a listener serving that conversation must see it — so
it cannot be a selfnote. But whatever waits for it by program needs more than
"something was posted": a waiter that treats any output as arrival wakes on
an error, on an unrelated post, and on `agentchat read`'s own "nothing newer"
line (all three seen, `observer` p2 ex1).

So the notification carries, as its **first line**, a line only a program
writes:

    [notice][watch] <watch name> <outcome>

and everything after it is for people. The watcher builds it with
`notice_line`; a waiter recognizes it with `parse_notice`, which matches the
first line exactly and nothing else — a notice quoted inside somebody's
prose is not a notice. The watch name is a message id (`w6917`), so nothing
else in the realm produces the same line by coincidence.

`agag wait` is the waiting half, and it is deliberately **not** in
`agentchat`: an agentic run is one reply (see the README), and a run given a
blocking wait is a run that is gone when the answer arrives. It is for a
session outside the realm's listeners — an interactive coding session, a
script — that has nothing else to be woken by.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass

#: The first thing in a notice's content, always.
NOTICE_MARKER = "[notice]"
#: The kind of notice agobserver writes.
WATCH_KIND = "watch"
#: What a watch can end as, as a notice says it.
MET = "met"
UNDELIVERABLE = "undeliverable"

_LINE = re.compile(r"\[notice\]\[(?P<kind>[a-z]+)\] (?P<name>w\d+) (?P<outcome>[a-z_]+)")

#: `agag wait` exits with this when nothing came in time — the same code the
#: retired `agentchat wait` used, so "still quiet" is never read as success
#: or as failure.
TIMEOUT_EXIT = 3


@dataclass(frozen=True)
class Notice:
    kind: str
    name: str
    outcome: str


def notice_line(name: str, outcome: str = MET, kind: str = WATCH_KIND) -> str:
    """The machine line for one watch outcome. Put it first in the post."""
    line = f"{NOTICE_MARKER}[{kind}] {name} {outcome}"
    if parse_notice(line) != Notice(kind, name, outcome):
        raise ValueError(f"not a well-formed notice: {line!r}")
    return line


def parse_notice(content) -> Notice | None:
    """The notice this message carries, or None. First line, exact match."""
    first = str(content or "").split("\n", 1)[0].strip()
    match = _LINE.fullmatch(first)
    if match is None:
        return None
    return Notice(match["kind"], match["name"], match["outcome"])


def find_notice(messages, name: str, kind: str = WATCH_KIND) -> tuple[dict, Notice] | None:
    """The first message (oldest first) carrying a notice for this watch."""
    for message in messages:
        notice = parse_notice(message.get("content"))
        if notice is not None and notice.kind == kind and notice.name == name:
            return message, notice
    return None


def add_wait_parser(sub) -> None:
    wait = sub.add_parser(
        "wait",
        help="block until a watch's notice arrives in one conversation",
        description=(
            "Block until <channel> > <topic> holds a notice for --watch newer "
            "than --since, then print that message and exit 0. Exit 3 when "
            "--timeout passes first. Other posts, and failed reads, do not end "
            "the wait. Credentials come from AGENTCHAT_ZULIP_ENV, as for "
            "agentchat. Not for agentic runs: a run is one reply."
        ),
    )
    wait.add_argument("channel", help="channel name, without the leading '#'")
    wait.add_argument("topic", help="topic name; followed across the ✔ rename")
    wait.add_argument("--watch", required=True, metavar="NAME", help="the watch name, e.g. w6917")
    wait.add_argument(
        "--since", type=int, default=None, metavar="MESSAGE_ID",
        help="only a notice newer than this counts (default: the topic's newest message now)",
    )
    wait.add_argument("--timeout", type=float, default=3600.0, help="seconds (default 3600)")
    wait.add_argument("--interval", type=float, default=10.0, help="seconds between reads (default 10)")
    wait.set_defaults(func=wait_command)


def wait_command(args, client=None, out=None, err=None, clock=time.monotonic, sleep=time.sleep) -> int:
    from .chat import AgentChatError, client_from_environment, format_messages, last_id, messages_since

    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        client = client_from_environment() if client is None else client
    except AgentChatError as error:
        print(f"agag wait: {error}", file=err)
        return 2
    since = args.since if args.since is not None else last_id(client, args.channel, args.topic)
    deadline = clock() + args.timeout
    while True:
        try:
            found = find_notice(messages_since(client, args.channel, args.topic, since), args.watch)
        except Exception as error:  # noqa: BLE001 - a failed read is not an arrival
            print(f"agag wait: read failed, retrying: {error}", file=err)
            found = None
        if found is not None:
            print(format_messages([found[0]]), file=out)
            return 0
        if clock() >= deadline:
            print(
                f"agag wait: no notice for {args.watch} in #{args.channel} > "
                f"{args.topic} within {args.timeout:g}s",
                file=err,
            )
            return TIMEOUT_EXIT
        sleep(args.interval)
