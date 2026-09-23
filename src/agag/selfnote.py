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

written **into home** after a callback from that topic has been answered
(under home's `\u2714 ` name when the serving resolved it on the way out). It
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
import re
from dataclasses import dataclass

#: Marks a message as machine-to-machine. First thing in the content, always.
SELFNOTE_MARKER = "[selfnote]"
#: The tag of the note that names the conversation a run is working for.
ROOTCHAT_TAG = "rootchat"
#: The tag of the note that says a callback has already been answered.
SERVED_TAG = "served"
#: The tag of a **deliberate** correction of a topic's anchor. See
#: `effective_rootchat` for the one rule that reads it.
MOVED_TAG = "rootchat-moved"
#: The tag of the relation naming the work a conversation was opened to
#: replace, by the message id of that work's own anchor. Shared since
#: `routine_tests` p2 ex1: it is written by the *replacing* agent (autolab
#: today) and read by every agent that was anchored in the conversation the
#: replacement took the name of.
REPLACES_TAG = "replaces"

#: The conversation a run is serving. The listener sets it; `agentchat`
#: reads it and writes the root note from it. A run without it posts without
#: a note — which is correct for a run nobody will call back.
HOME_VARIABLE = "AGENTCHAT_HOME"

__all__ = [
    "HOME_ANCHOR_VARIABLE",
    "HOME_VARIABLE",
    "MOVED_TAG",
    "REPLACES_TAG",
    "ROOTCHAT_TAG",
    "SELFNOTE_MARKER",
    "SERVED_TAG",
    "OWED_TAG",
    "owed_note",
    "parse_owed",
    "START_TAG",
    "Conversation",
    "effective_rootchat",
    "home_from_environment",
    "is_selfnote",
    "last_real_message",
    "last_real_sender",
    "note",
    "own_rootchat",
    "parse_conversation",
    "own_rootchat_moved",
    "parse_note",
    "parse_replaces",
    "parse_rootchat_moved",
    "parse_rootchat",
    "parse_served",
    "replaced_anchor",
    "replaces_note",
    "rootchat_moved_note",
    "rootchat_note",
    "served_note",
    "owed_start",
    "parse_start",
    "start_note",
    "is_progress",
    "without_selfnotes",
]


@dataclass(frozen=True, eq=False)
class Conversation:
    """One channel/topic pair — Zulip's unit of conversation.

    `anchor` (`explicit_reply` p1 step 3) is the id of a message *in* that
    conversation — the post a serving was started for — so a note that
    names the conversation can be followed to where that message is **now**
    (`agag.zulip.locate`), after a rename, a resolve or a reuse of the
    display name. Identity is still the pair: two conversations with the
    same channel and topic are equal whatever anchor either carries, so
    every existing comparison keeps its meaning.
    """

    channel: str
    topic: str
    anchor: int | None = None

    def __str__(self) -> str:
        base = f"{self.channel}/{self.topic}"
        return f"{base} #{int(self.anchor)}" if self.anchor else base

    def __eq__(self, other) -> bool:
        return isinstance(other, Conversation) and self.as_pair() == other.as_pair()

    def __hash__(self) -> int:
        return hash(self.as_pair())

    def as_pair(self) -> tuple[str, str]:
        return (self.channel, self.topic)


_ANCHOR_SUFFIX = re.compile(r"^(?P<text>.*?)\s+#(?P<anchor>\d+)$", re.DOTALL)


def parse_conversation(value: str | None) -> Conversation | None:
    """`"<channel>/<topic>"` — optionally `"<channel>/<topic> #<message id>"`
    — as a `Conversation`, or None when it is not one.

    Split on the *first* separator: a topic may contain slashes, a channel
    may not — the same rule the topic workspaces already live by.
    """
    text = (value or "").strip()
    anchor = None
    match = _ANCHOR_SUFFIX.match(text)
    if match is not None:
        text, anchor = match.group("text").strip(), int(match.group("anchor"))
    if "/" not in text:
        return None
    channel, topic = text.split("/", 1)
    channel, topic = channel.strip(), topic.strip()
    if not channel or not topic:
        return None
    return Conversation(channel, topic, anchor)


#: The id of the post the serving was started for, beside `AGENTCHAT_HOME`,
#: so the root note a run writes elsewhere can be followed by id.
HOME_ANCHOR_VARIABLE = "AGENTCHAT_HOME_ANCHOR"


def home_from_environment(environ=None) -> Conversation | None:
    """The conversation this run is serving, per `AGENTCHAT_HOME` (and the
    post it was started for, per `AGENTCHAT_HOME_ANCHOR`, when set)."""
    environ = os.environ if environ is None else environ
    home = parse_conversation(environ.get(HOME_VARIABLE))
    if home is None:
        return None
    raw = str(environ.get(HOME_ANCHOR_VARIABLE) or "").strip()
    if raw.isdigit() and int(raw) > 0 and home.anchor is None:
        return Conversation(home.channel, home.topic, int(raw))
    return home


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


def replaces_note(anchor_id: int) -> str:
    """`[selfnote][replaces] <message id>` — what this conversation replaced.

    Written once, by the agent that opens the replacement, naming the retired
    work's own anchor **by id**. By id because the replacement usually takes
    over the retired conversation's display name — releasing that name is the
    point of retiring it — so a name would point at the replacement itself.
    """
    return note(REPLACES_TAG, str(int(anchor_id)))


def parse_replaces(content) -> int | None:
    """The anchor id a replaces note names, or None for anything else."""
    value = parse_note(content, REPLACES_TAG)
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def replaced_anchor(messages) -> int | None:
    """The anchor id this conversation replaces, whoever wrote the relation.

    Deliberately **not** filtered to one sender. The relation is written by
    the replacing agent — autolab, when it retires a mission and opens the
    successor under the freed name — and every third party that was anchored
    in the retired conversation has to be able to read it. Filtering it to
    the reader's own id would reproduce `routine_tests` p2's defect exactly:
    Front was anchored in the retired topic and had written nothing in the
    replacement, which is why it had nothing to find.

    The earliest valid note wins: a conversation replaces one thing, decided
    when it was opened. A malformed one is not a note of this kind and is
    skipped, which is the only error handling a one-line convention needs.
    """
    for message in messages:
        anchor = parse_replaces(message.get("content"))
        if anchor is not None:
            return anchor
    return None


#: The tag of an owner's deliberate start of its own conversation
#: (`robust_workflow` p1 step 3).
START_TAG = "start"
#: A line of live progress (`🔧 tool: detail`, `💬 text`) — autolab's
#: `RunProgress` — is evidence that a run is working, not an answer.
_PROGRESS = re.compile(r"^(\U0001F527|\U0001F4AC)")


def is_progress(content) -> bool:
    """Whether a post is nothing but progress lines."""
    lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
    return bool(lines) and all(_PROGRESS.match(line) for line in lines)


def start_note(because_id: int, requester_id: int, requester_name: str) -> str:
    """`[selfnote][start] #<because> for <user id> <name>`.

    An owner's own decision to serve one of its conversations that nobody
    else has posted into: autolab starting the next task of a mission the
    requester authorized, after the requester accepted the previous one.
    Until `robust_workflow` p1 the only thing that could start a
    conversation was somebody else's post, so every task needed a relay —
    and the relay was the step adventure_game p3 lost 24 minutes to.

    `because` is the post that made the start due (the acceptance), so the
    record says why; the requester is who the answer is handed to, because
    in a conversation where only its owner has spoken there is nobody else
    to hand it to. It is a selfnote — it buys nobody *else* a run; it is the
    owner owing itself one.
    """
    name = " ".join(str(requester_name or "").split())
    return note(START_TAG, f"#{int(because_id)} for {int(requester_id)} {name}".rstrip())


_START = re.compile(r"^#(?P<because>\d+)\s+for\s+(?P<user>\d+)(?:\s+(?P<name>.+))?$")


def parse_start(content) -> tuple[int, int, str] | None:
    """`(because id, requester id, requester name)`, or None."""
    value = parse_note(content, START_TAG)
    if value is None:
        return None
    match = _START.match(value.strip())
    if match is None:
        return None
    return int(match.group("because")), int(match.group("user")), (match.group("name") or "").strip()


def owed_start(messages, self_id: int, is_ack=lambda content: False) -> dict | None:
    """The newest start note of this bot that no serving has answered yet,
    as a requester-shaped message (`id`, `sender_id`, `sender_full_name`,
    `because`), or None.

    Answered means: this bot said something after the note that is neither
    its ack nor a progress line. An ack or progress alone is a run that
    started and did not finish — a crash there leaves the start owed, which
    is the rule every other serving already follows.
    """
    pending = None
    for message in messages:
        if message.get("sender_id") != self_id:
            continue
        parsed = parse_start(message.get("content"))
        if parsed is not None:
            because, requester, name = parsed
            pending = {"id": int(message.get("id") or 0), "sender_id": requester,
                       "sender_full_name": name, "because": because}
            continue
        content = str(message.get("content") or "")
        if pending is None or is_selfnote(content) or is_ack(content.strip()) or is_progress(content):
            continue
        if not is_system_notice(message):
            pending = None
    return pending


def served_note(remote: Conversation, message_id: int) -> str:
    """The note saying a callback from `remote` up to `message_id` is answered.

    Written **into home** — the agent's own conversation — because a post in
    the remote topic would be a post in somebody else's conversation, and
    answering at home exists precisely not to do that. Home is this agent's
    own topic, so the note triggers nobody either.
    """
    return note(SERVED_TAG, f"{remote} {int(message_id)}")


#: A note posted beside a request to recover an answer somebody's listener
#: never served (robust_workflow p2 step 5): `[selfnote][owed] <remote> <id>`.
#: The serving whose processed input includes it, once its reply is
#: delivered, is the receipt for that answer — its listener writes the
#: served mark (`agag.listen`).
OWED_TAG = "owed"


def owed_note(remote: Conversation, message_id: int) -> str:
    return note(OWED_TAG, f"{remote} {int(message_id)}")


def parse_owed(content) -> tuple[Conversation, int] | None:
    """`(remote, answer id)` of an owed note, or None."""
    value = parse_note(content, OWED_TAG)
    if value is None:
        return None
    return parse_served(note(SERVED_TAG, value))


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
    """The home this bot's **ordinary** root notes anchor this topic to.

    The **earliest** of our own root notes wins. A topic is anchored once, by
    the run that opened it; a later note would be a repeat, and the first one
    is the conversation the topic was actually opened for. That rule is what
    stops a repeat redirecting a live conversation, and it is kept exactly.

    A pure history selector: no network, no correction. `effective_rootchat`
    is the one that reads a deliberate move as well, and it is what every
    routing lookup asks.
    """
    for message in messages:
        if message.get("sender_id") != self_id:
            continue
        home = parse_rootchat(message.get("content"))
        if home is not None:
            return home
    return None


def rootchat_moved_note(home: Conversation) -> str:
    """`[selfnote][rootchat-moved] <channel>/<topic>` — a deliberate move.

    `routine_tests` p2 ex1, problem B2. "A topic is anchored once" is the
    right default — a bare repeat must not redirect a live conversation —
    but until now there was no way to say a topic was anchored *wrongly*,
    and p1 and p2 between them wanted one six times. p2's manual repair
    wrote an ordinary root note into the delegation topic and it changed
    nothing, correctly, because a repeat loses.

    This note is not a repeat. It says, in its own tag, that the agent is
    correcting the anchor on purpose, and only an agent's own move moves its
    own anchor. Still a selfnote: hidden from every chatlog and thread file,
    and never enough on its own to buy anybody a run.
    """
    return note(MOVED_TAG, str(home))


def parse_rootchat_moved(content) -> Conversation | None:
    """The conversation a move note names, or None if this is not one."""
    return parse_conversation(parse_note(content, MOVED_TAG))


def own_rootchat_moved(messages, self_id: int) -> Conversation | None:
    """The newest deliberate move **this bot** wrote in this topic.

    Newest, not earliest: unlike an anchor, a correction is not identity. An
    agent that gets it wrong twice must be able to say so twice, and the
    last thing it said is what it means. A move written by somebody else is
    somebody else's anchor and is ignored here — this is the one place the
    sender filter is the whole point, which is why it is not the reader of
    the `replaces` relation.
    """
    for message in reversed(list(messages)):
        if message.get("sender_id") != self_id:
            continue
        home = parse_rootchat_moved(message.get("content"))
        if home is not None:
            return home
    return None


def effective_rootchat(messages, self_id: int) -> Conversation | None:
    """Where this bot's answers for this topic belong, all rules applied.

    **One rule, everywhere**: the newest valid explicit move written by this
    agent wins; otherwise its earliest ordinary root note wins. A later
    ordinary repeat never redirects the topic.

    This is what every routing lookup asks — the callback route, the note
    search, the thread placement and the recovery sweeps — so a correction
    lands in all of them at once, and a corrected delegate appears under its
    new home rather than its old one.
    """
    moved = own_rootchat_moved(messages, self_id)
    return moved if moved is not None else own_rootchat(messages, self_id)


# --- who spoke last, for real ---------------------------------------------

#: The realm Zulip's own system bots post from — Notification Bot ("… has
#: marked this topic as resolved / unresolved", "… moved here"), Welcome Bot,
#: the email gateway. Their lines are the server talking about the
#: conversation, not anybody talking in it. The realm string is the marker,
#: not the display name (renameable) or the user id (realm-local; these
#: senders are not even in the realm's member list).
SYSTEM_REALM = "zulipinternal"


def is_system_notice(message) -> bool:
    """A post by one of Zulip's cross-realm system bots.

    Un-resolving a `#front` run topic posts "Developer has marked this topic
    as unresolved" into it, and until `operation_room` p8 that line counted
    as somebody speaking: the topic's owner was served and a run was bought to
    answer a notice. Same shape as the selfnote ack loop, one realm over.
    """
    return message.get("sender_realm_str") == SYSTEM_REALM


def is_speech(message) -> bool:
    """A message somebody actually said: not a selfnote, not a system notice."""
    return not is_selfnote(message.get("content")) and not is_system_notice(message)


def without_selfnotes(messages):
    """The conversation with the machine-to-machine lines taken out —
    selfnotes and system notices alike."""
    return [m for m in messages if is_speech(m)]


def last_real_message(messages) -> dict | None:
    """The newest message that is speech, or None if there is none."""
    for message in reversed(list(messages)):
        if is_speech(message):
            return message
    return None


def last_real_sender(messages) -> int | None:
    """Who spoke last, ignoring selfnotes and system notices. None when
    nobody really has.

    This is the predicate every "does this topic await me?" check is built
    on. A topic holding nothing but notes and notices awaits nobody.
    """
    message = last_real_message(messages)
    if message is None:
        return None
    sender = message.get("sender_id")
    return None if sender is None else int(sender)
