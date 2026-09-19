"""What a run needs to continue a conversation it did not start.

`explicit_reply` p1 step 4. A run is one reply and remembers nothing; the
next serving of the same conversation gets the conversation (bounded) as
text and the threads it took part in as files, and has to work out for
itself what arrived since last time, what it is still waiting for and what
it had decided to do when the answers came. Long conversations, callbacks
that arrive out of order, a restart in the middle and a human correction
posted while a delegate was working all make that reconstruction the
place a run goes wrong.

The **continuation view** is a compact block carried in the prompt beside
the conversation, refreshed for every serving (callbacks included):

- **new input**: speech by others past the input boundary the last
  delivered serving processed (`serving.input_up_to`), or — without a
  record — past this bot's last real post;
- **the agent's own summary**: goal, agreed conditions and intended next
  actions, as the agent last wrote them in an `ag-continue` block, with
  the message it was written after; anything the human said after that
  message overrides it, and the view says so;
- **outstanding requests**: every conversation this one reached out to
  (root notes), with its state read from evidence — awaiting a reply,
  answered and not yet served (the served mark), served, or finished;
- **an interrupted previous serving** of the same input, with the stage it
  reached, so actions it may have started are reconciled rather than
  repeated;
- **what is omitted**: history not carried, evidence that could not be
  read.

Request ids, delivery state and pending relationships are derived from the
record and the notes; only the summary is the agent's, and it is kept as a
selfnote in the served conversation (`[selfnote][continuation] {…}`) — the
same place every other memory in this realm lives, hidden from chatlogs,
buying nobody a run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable

from .selfnote import Conversation, is_speech, note, parse_note
from .zulip import RESOLVED_TOPIC_PREFIX

#: The fence's info string, and the note's tag.
CONTINUE_LANGUAGE = "ag-continue"
CONTINUATION_TAG = "continuation"
#: The keys the block may carry; unknown keys are kept as written.
KEYS = ("goal", "conditions", "next", "notes")
#: How much of a post the view quotes.
QUOTE_CHARS = 240
#: How many new posts the view lists before it says "and N more".
NEW_INPUT_LIMIT = 8

BEGIN = "===== BEGIN CONTINUATION ====="
END = "===== END CONTINUATION ====="

_BLOCK = re.compile(r"^[ \t]{0,3}```[ \t]*" + re.escape(CONTINUE_LANGUAGE) + r"[ \t]*\n(?P<body>.*?)^[ \t]{0,3}```[ \t]*$",
                    re.MULTILINE | re.DOTALL)

__all__ = [
    "BEGIN",
    "CONTINUATION_GUIDE",
    "CONTINUATION_TAG",
    "CONTINUE_LANGUAGE",
    "END",
    "Continuation",
    "Remote",
    "continuation_note",
    "continuation_view",
    "latest_continuation",
    "new_input",
    "parse_continuation",
    "remote_state",
    "split_continuation",
]


@dataclass(frozen=True)
class Continuation:
    """The agent's own carry-forward: what it wrote, and after which post."""

    fields: dict[str, str]
    written_after: int = 0
    message_id: int = 0

    def get(self, key: str) -> str:
        return self.fields.get(key, "")


def split_continuation(output: str) -> tuple[str, Continuation | None, str | None]:
    """`(output without the block, the continuation, error)`.

    The **last** block wins when there are several. `key: value` lines;
    a line starting with whitespace continues the previous value. An
    unreadable line is an error and the block is dropped, but the output
    is still returned without it.
    """
    matches = list(_BLOCK.finditer(output or ""))
    if not matches:
        return (output or ""), None, None
    text = output
    for match in reversed(matches):
        text = text[: match.start()] + text[match.end():]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    fields: dict[str, str] = {}
    current: str | None = None
    for line in matches[-1].group("body").splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and current is not None:
            fields[current] = (fields[current] + "\n" + line.strip()).strip()
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or " " in key.strip():
            return text, None, f"unreadable line in the {CONTINUE_LANGUAGE} block: {line.strip()!r}"
        current = key.strip().lower()
        fields[current] = value.strip()
    return text, Continuation({k: v for k, v in fields.items() if v}), None


def continuation_note(continuation: Continuation, written_after: int) -> str:
    """`[selfnote][continuation] {json}` for the served conversation."""
    body = {"after": int(written_after), **{k: v for k, v in continuation.fields.items()}}
    return note(CONTINUATION_TAG, json.dumps(body, ensure_ascii=False))


def parse_continuation(content, message_id: int = 0) -> Continuation | None:
    value = parse_note(content, CONTINUATION_TAG)
    if value is None:
        return None
    try:
        body = json.loads(value)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    after = body.pop("after", 0)
    try:
        after = int(after)
    except (TypeError, ValueError):
        after = 0
    return Continuation({str(k): str(v) for k, v in body.items() if str(v)}, after, int(message_id or 0))


def latest_continuation(history: Iterable[dict], self_id: int) -> Continuation | None:
    """The newest continuation note **this bot** wrote in the conversation."""
    for message in reversed(list(history)):
        if message.get("sender_id") != self_id:
            continue
        found = parse_continuation(message.get("content"), int(message.get("id") or 0))
        if found is not None:
            return found
    return None


# --- evidence ---------------------------------------------------------------------


def new_input(history: Iterable[dict], self_id: int, since_id: int | None) -> list[dict]:
    """Speech by others newer than `since_id`; without one, newer than this
    bot's last real post (its reply, not its ack — the caller's `drop`
    filters acks out of `history` as it does for the chatlog)."""
    rows = list(history)
    if since_id is None:
        since_id = 0
        for message in rows:
            if message.get("sender_id") == self_id and is_speech(message):
                since_id = max(since_id, int(message.get("id") or 0))
    return [m for m in rows if m.get("sender_id") != self_id and is_speech(m) and int(m.get("id") or 0) > since_id]


@dataclass
class Remote:
    """One conversation this one reached out to, with what was read of it."""

    conversation: Conversation
    messages: list[dict] = field(default_factory=list)
    live_name: str = ""
    unavailable: str | None = None
    served_up_to: int = 0


def remote_state(remote: Remote, self_id: int) -> tuple[str, dict | None]:
    """`(state, the post that decides it)`: `unreadable`, `finished`,
    `awaiting` (our post is the last real one), `answered` (somebody
    replied after our last post and the served mark is below it), or
    `served` (their last reply is at or below the mark)."""
    if remote.unavailable:
        return "unreadable", None
    speech = [m for m in remote.messages if is_speech(m)]
    theirs = [m for m in speech if m.get("sender_id") != self_id]
    ours = [m for m in speech if m.get("sender_id") == self_id]
    last_theirs = theirs[-1] if theirs else None
    last_ours_id = int(ours[-1].get("id") or 0) if ours else 0
    live = remote.live_name or remote.conversation.topic
    if live.startswith(RESOLVED_TOPIC_PREFIX):
        return "finished", last_theirs
    if last_theirs is None or int(last_theirs.get("id") or 0) < last_ours_id:
        return "awaiting", ours[-1] if ours else None
    if int(last_theirs.get("id") or 0) <= remote.served_up_to:
        return "served", last_theirs
    return "answered", last_theirs


def _quote(message: dict | None) -> str:
    if message is None:
        return ""
    who = str(message.get("sender_full_name") or f"user{message.get('sender_id')}")
    text = " ".join(str(message.get("content") or "").split())
    if len(text) > QUOTE_CHARS:
        text = text[:QUOTE_CHARS].rstrip() + " […]"
    return f"[{who} #{message.get('id')}] {text}"


def continuation_view(
    history: list[dict], self_id: int, *,
    last_input_up_to: int | None = None,
    last_delivered_id: int | None = None,
    remotes: Iterable[Remote] = (),
    brought_by: tuple[str, str, int] | None = None,
    interrupted=None,
    omitted_before: int | None = None,
    omitted_count: int = 0,
    file_name: str = "chatlog.md",
) -> str:
    """The view, between visible markers, for the prompt.

    `history` is the served conversation as read for this serving (acks
    and notes included: the notes are what the summary is read from).
    `last_input_up_to` / `last_delivered_id` come from the last delivered
    serving record when there is one. `brought_by` names the remote post
    that caused this serving when it is a callback. `interrupted` is the
    previous serving record of the same input a restart left behind.
    """
    lines: list[str] = []
    fresh = new_input(history, self_id, last_input_up_to)
    if last_delivered_id:
        head = (f"Your last reply here is message {last_delivered_id}; it answered everything up to "
                f"message {last_input_up_to}.")
    else:
        head = "You have not replied in this conversation yet." if not any(
            m.get("sender_id") == self_id and is_speech(m) for m in history) else \
            "Your last reply here is your newest post below."
    lines.append(head)
    if brought_by is not None:
        channel, topic, message_id = brought_by
        lines.append(f"This serving was brought by a post in #{channel} › {topic} (message {message_id}); "
                     f"that thread is placed beside the chatlog.")
    if fresh:
        lines.append(f"New here since then ({len(fresh)} post{'s' if len(fresh) != 1 else ''}):")
        if len(fresh) > NEW_INPUT_LIMIT:
            # The newest are what a decision turns on; the rest are counted,
            # not quoted — the conversation carries them, bounded as it is.
            lines.append(f"  - … {len(fresh) - NEW_INPUT_LIMIT} earlier of them are not quoted here; "
                         f"they are in the conversation.")
        for message in fresh[-NEW_INPUT_LIMIT:]:
            lines.append(f"  - {_quote(message)}")
    else:
        lines.append("Nothing new has been said here since then.")

    summary = latest_continuation(history, self_id)
    if summary is not None:
        overriding = [m for m in fresh if int(m.get("id") or 0) > summary.written_after] if summary.written_after else fresh
        lines.append("")
        lines.append(f"What you last recorded for this conversation (written after message {summary.written_after}):")
        for key in KEYS:
            if summary.get(key):
                lines.append(f"  {key}: {summary.get(key)}")
        for key, value in summary.fields.items():
            if key not in KEYS:
                lines.append(f"  {key}: {value}")
        if overriding:
            lines.append(f"  Posts newer than message {summary.written_after} (listed above) override this "
                         f"where they disagree; the newest instruction wins.")
    else:
        lines.append("")
        lines.append("You have recorded no goal or next action for this conversation yet.")

    remotes = list(remotes)
    lines.append("")
    if remotes:
        lines.append("Requests you made in other conversations:")
        for remote in remotes:
            state, decisive = remote_state(remote, self_id)
            name = f"#{remote.conversation.channel} › {remote.conversation.topic}"
            if state == "unreadable":
                lines.append(f"  - {name}: could not be read ({remote.unavailable}); its state is unknown.")
            elif state == "finished":
                lines.append(f"  - {name}: finished (✔)." + (f" Last word: {_quote(decisive)}" if decisive else ""))
            elif state == "awaiting":
                lines.append(f"  - {name}: awaiting a reply to your post"
                             + (f" (message {decisive.get('id')})." if decisive else "."))
            elif state == "answered":
                lines.append(f"  - {name}: answered, not yet dealt with: {_quote(decisive)}")
            else:
                lines.append(f"  - {name}: answered and already dealt with (up to message {remote.served_up_to}).")
    else:
        lines.append("You have made no request in another conversation for this one.")

    if interrupted is not None:
        lines.append("")
        stage = getattr(interrupted, "state", "unknown")
        ack = getattr(interrupted, "ack_id", None)
        up_to = getattr(interrupted, "input_up_to", None)
        detail = f" (acknowledged as message {ack}" + (f", input read up to message {up_to}" if up_to else "") + ")" if ack else ""
        lines.append(f"A previous run on this same input was interrupted at stage {stage!r}{detail} and never "
                     f"replied. It may have started actions — posts elsewhere, files, commands. Check the threads "
                     f"and the workspace before repeating any of them.")

    notes: list[str] = []
    if omitted_count:
        where = f"before message {omitted_before}" if omitted_before else "at the start"
        notes.append(f"{omitted_count} earlier message{'s' if omitted_count != 1 else ''} of this conversation "
                     f"{'are' if omitted_count != 1 else 'is'} not carried in the prompt ({where}); \"{file_name}\" "
                     f"holds the whole of it.")
    unreadable = [r for r in remotes if r.unavailable]
    if unreadable:
        notes.append(f"{len(unreadable)} thread{'s' if len(unreadable) != 1 else ''} could not be read; "
                     f"the state of {'those requests' if len(unreadable) != 1 else 'that request'} is unknown, not settled.")
    if notes:
        lines.append("")
        lines.append("Not carried here: " + " ".join(notes))
    lead = ("How this conversation stands, derived from the record; the conversation itself is the evidence "
            "and the newest post in it is the authority.")
    return f"{lead}\n\n{BEGIN}\n" + "\n".join(lines) + f"\n{END}"


CONTINUATION_GUIDE = f"""\
# Carrying the conversation forward

A serving remembers nothing by itself; the next one is given the block between `{BEGIN}` and `{END}` — what arrived since your last reply, what you last recorded, and where each request you made elsewhere stands — derived from the record. What is *yours* to keep is the goal, the conditions agreed so far and what you intend to do when the answers arrive. Record them, when they change, in one fenced block anywhere in your output:

```{CONTINUE_LANGUAGE}
goal: <what this conversation is for, in one line>
conditions: <what has been agreed or constrained, with message ids where it matters>
next: <what to do when which answer arrives; what to do if it does not>
```

It is never posted; it is kept for your next serving. Anything the developer says after you wrote it overrides it, and the view says so. Do not restate what has not changed."""
