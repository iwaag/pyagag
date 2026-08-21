"""Notes an agent writes to itself, in the conversation, for its next run.

A run is one reply. It says something somewhere and ends; when an answer
arrives it is served again. For that to work something has to remember,
across the end of the run, that *this* agent is party to *that* conversation
and on behalf of which of its own. Until `agent_standardize` p8 that memory
was a local ledger file. It is now the chat itself.

A **selfnote** is a message whose content starts with `[selfnote]`. It is
machine-to-machine: hidden from every rendered `chatlog.md`, from every
`threads/` file, and from `agentchat read` unless `--all` is asked for —
hidden from its own author too, because an agent that sees its own notes
starts writing them by hand, and then they are prose rather than a record.

The note that carries the memory is the **root note**:

    [selfnote][rootchat] <channel>/<topic>

written by `agentchat send` before the first real post it makes in a topic.
It says: whatever I am doing here, I am doing on behalf of that conversation
of mine. A reply naming this agent then resolves to a home to serve, and
every topic anchored this way is one this agent is party to — the two
questions the ledger used to answer, asked of the chat instead.

The second note this module names is the **served note**:

    [selfnote][served] <channel>/<topic> <message id>

written **into home** after a callback from that topic has been answered. It
is the answer to "have I already dealt with this?", which the chat could not
be asked before: a called-back run replies at home, so the agent never
becomes the last poster in the topic that named it, and "the last real post
there names me" stays true forever. Recovery would then re-serve every
exchange it ever had. The note bounds that — a naming post at or below the
served id is one already answered.

The general shape is `[selfnote][<tag>] <value>`; `rootchat` and `served` are
the ones this module names, and consumers add their own (agforge anchors an
`assetrun-` topic to its Work with a `work` note).

**The crux is that a selfnote must never buy anybody a run.** Everywhere a
listener asks "who spoke last", the answer has to be the last *non-selfnote*
message — otherwise the note an agent writes to itself is a post by somebody
else in the other agent's topic, and the ack loop of p7 comes back wearing a
new coat. `last_real_message` is that answer, and the sweep, the event path
and `serve_topic`'s post-run re-check all go through it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Marks a message as machine-to-machine. First thing in the content, always.
SELFNOTE_MARKER = "[selfnote]"
#: The tag of the note that names the conversation a run is working for.
ROOTCHAT_TAG = "rootchat"
#: The tag of the note that says a callback has already been answered.
SERVED_TAG = "served"

#: The conversation a run is serving. The listener sets it; `agentchat`
#: reads it and writes the root note from it. A run without it posts without
#: a note — which is correct for a run nobody will call back.
HOME_VARIABLE = "AGENTCHAT_HOME"

__all__ = [
    "HOME_VARIABLE",
    "ROOTCHAT_TAG",
    "SELFNOTE_MARKER",
    "SERVED_TAG",
    "Conversation",
    "home_from_environment",
    "is_selfnote",
    "last_real_message",
    "last_real_sender",
    "note",
    "own_rootchat",
    "parse_conversation",
    "parse_note",
    "parse_rootchat",
    "parse_served",
    "rootchat_note",
    "served_note",
    "without_selfnotes",
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


# --- the convention --------------------------------------------------------


def is_selfnote(content) -> bool:
    """Whether this message body is a note an agent wrote to itself."""
    return str(content or "").lstrip().startswith(SELFNOTE_MARKER)


def note(tag: str, value: str) -> str:
    """`[selfnote][<tag>] <value>` — the whole format."""
    return f"{SELFNOTE_MARKER}[{tag}] {value}"


def parse_note(content, tag: str) -> str | None:
    """The value of a `[selfnote][<tag>]` message, or None for anything else.

    Anything that is not this exact shape is not a note of this kind, and
    saying so is the only error handling a one-line convention needs.
    """
    text = str(content or "").strip()
    if not is_selfnote(text):
        return None
    rest = text[len(SELFNOTE_MARKER):].lstrip()
    head = f"[{tag}]"
    if not rest.startswith(head):
        return None
    value = rest[len(head):].strip()
    return value or None


def rootchat_note(home: Conversation) -> str:
    """The root note for a run serving `home`."""
    return note(ROOTCHAT_TAG, str(home))


def parse_rootchat(content) -> Conversation | None:
    """The conversation a root note names, or None if this is not one."""
    return parse_conversation(parse_note(content, ROOTCHAT_TAG))


def served_note(remote: Conversation, message_id: int) -> str:
    """The note saying a callback from `remote` up to `message_id` is answered.

    Written **into home** — the agent's own conversation — because a post in
    the remote topic would be a post in somebody else's conversation, and
    answering at home exists precisely not to do that. Home is this agent's
    own topic, so the note triggers nobody either.
    """
    return note(SERVED_TAG, f"{remote} {int(message_id)}")


def parse_served(content) -> tuple[Conversation, int] | None:
    """`(remote, message id)` of a served note, or None for anything else."""
    value = parse_note(content, SERVED_TAG)
    if value is None:
        return None
    text, _, tail = value.rpartition(" ")
    remote = parse_conversation(text)
    if remote is None:
        return None
    try:
        return remote, int(tail)
    except ValueError:
        return None


def own_rootchat(messages, self_id: int) -> Conversation | None:
    """The home this bot anchored this topic to, reading its history.

    The **earliest** of our own root notes wins. A topic is anchored once, by
    the run that opened it; a later note would be a repeat, and the first one
    is the conversation the topic was actually opened for.
    """
    for message in messages:
        if message.get("sender_id") != self_id:
            continue
        home = parse_rootchat(message.get("content"))
        if home is not None:
            return home
    return None


# --- who spoke last, for real ---------------------------------------------


def without_selfnotes(messages):
    """The conversation with the machine-to-machine lines taken out."""
    return [m for m in messages if not is_selfnote(m.get("content"))]


def last_real_message(messages) -> dict | None:
    """The newest message that is not a selfnote, or None if there is none."""
    for message in reversed(list(messages)):
        if not is_selfnote(message.get("content")):
            return message
    return None


def last_real_sender(messages) -> int | None:
    """Who spoke last, ignoring selfnotes. None when nobody really has.

    This is the predicate every "does this topic await me?" check is built
    on. A topic holding nothing but notes awaits nobody.
    """
    message = last_real_message(messages)
    if message is None:
        return None
    sender = message.get("sender_id")
    return None if sender is None else int(sender)
