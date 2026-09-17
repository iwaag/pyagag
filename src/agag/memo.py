"""Memos: conversations that are read and never answered.

`argue` p2 step 1. The realm has had one kind of conversation until now —
the *actionable* kind: a post in it may serve its owner, a mention in it is
an invitation, a command line in it is obeyed, a selfnote in it is somebody's
memory. A **memo** is the other kind. It is presentation only: something a
program wrote down so that a screen can show it (Front's character
re-voicing of a discussion is the first one), where **nothing that is
written ever starts anything** — not plain text, not a real `@**bot**`
mention, not a notifier command, not a copied `[selfnote]` line, not a
rename or a resolve, and not a restart with work already queued.

**The distinction is the channel.** A conversation is a memo when its
channel is `memo` or is named `memo-…`. That is deliberately not a topic
prefix, a checkmark or a note inside the conversation:

- it is decidable **before the first message** exists, so the very first
  post of a memo is already quiet;
- it survives every topic rename, a resolve included — the ComfyUI notifier
  accepts commands under `✔ ` on purpose, so a checkmark could never be the
  silence;
- it needs no read: every consumer already knows the channel of the message
  in its hand.

**One rule, applied everywhere a conversation can become work**
(`is_memo_channel`): `agag.listen` at intake, at the mention route, at
recovery and again at execution time (so an entry queued by an older process
is dropped, not served); `agag.topics.serve_topic` for a handler reached some
other way; the mirror's note index and `ZulipClient.own_notes`, so a selfnote
copied into a memo is nobody's memory; `agentchat send`, which writes no root
note there; and the consumers outside the shared listener (the ComfyUI
notifier's command intake). The mirror still *holds* memo conversations like
any other — display reads them; only obligation ignores them.

**A memo names its source with a link of its own**:

    [selfnote][memosource] <message id>

the id of a message in the source conversation (an argue's anchor note, a
Front Desk conversation's first post). An id survives every rename, so the
link follows the source wherever it is now. It is *not* `rootchat`: a root
note takes part in callback routing, `threads/` and evidence discovery, and
a presentation is none of those. Nothing but a memo reader asks for this tag,
and it asks with `include_memos=True`.

**What a memo holds** is the writer's business; the one shared shape is the
fenced record (`ag-memo`, one JSON object per post) that lets a reader tell
a result from prose without knowing who wrote it.
"""

from __future__ import annotations

import json
import re

from .selfnote import note, parse_note

#: The realm's memo channel, and the prefix of any further one.
MEMO_CHANNEL = "memo"
MEMO_CHANNEL_PREFIX = "memo-"
#: `[selfnote][memosource] <message id>` — which conversation a memo presents.
SOURCE_TAG = "memosource"
#: The fenced record a memo post carries.
RECORD_FENCE = "ag-memo"
#: Zulip topic names are at most 60 characters.
TOPIC_LIMIT = 60

_RECORD = re.compile(r"```[ \t]*" + re.escape(RECORD_FENCE) + r"[ \t]*\n(.*?)\n[ \t]*```[ \t]*", re.DOTALL)
_SLUG = re.compile(r"[^a-z0-9-]+")

__all__ = [
    "MEMO_CHANNEL",
    "MEMO_CHANNEL_PREFIX",
    "RECORD_FENCE",
    "SOURCE_TAG",
    "is_memo_channel",
    "is_memo_message",
    "memo_topic",
    "parse_record",
    "parse_source",
    "render_record",
    "source_note",
]


def is_memo_channel(channel) -> bool:
    """Whether every conversation in this channel is a memo."""
    name = str(channel or "").strip().lower()
    return name == MEMO_CHANNEL or name.startswith(MEMO_CHANNEL_PREFIX)


def is_memo_message(message) -> bool:
    """The same question asked of a Zulip message dict (`display_recipient`)."""
    if not isinstance(message, dict):
        return False
    recipient = message.get("display_recipient")
    return isinstance(recipient, str) and is_memo_channel(recipient)


def source_note(message_id: int) -> str:
    return note(SOURCE_TAG, str(int(message_id)))


def parse_source(content) -> int | None:
    value = parse_note(content, SOURCE_TAG)
    if value is None:
        return None
    try:
        return int(value.split()[0])
    except (IndexError, ValueError):
        return None


def memo_topic(source_id: int, label: str = "") -> str:
    """The memo topic of one source: a readable stem and the source's id.

    The id is what makes the name unique — a source's display name is
    reusable, its anchor is not. The name is a convenience all the same: a
    reader finds a memo by its `memosource` note, never by this string.
    """
    suffix = f"-s{int(source_id)}"
    stem = _SLUG.sub("-", str(label or "").lower()).strip("-") or "memo"
    return f"{stem[:TOPIC_LIMIT - len(suffix)].rstrip('-')}{suffix}"


def render_record(payload: dict) -> str:
    """One record as a memo post: the JSON inside its fence."""
    return f"```{RECORD_FENCE}\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n```"


def parse_record(content) -> dict | None:
    """The record a memo post carries, or None when it carries none."""
    match = _RECORD.search(str(content or ""))
    if match is None:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
