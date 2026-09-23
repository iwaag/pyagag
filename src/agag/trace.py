"""Where a request stands, read from the conversations it went through.

`robust_workflow` p1 step 2. A request reaches Front as a post; Front opens a
`workplan-` topic for autolab or an `assetplan-` topic for forge; those open
task and run topics; each is served, answered, accepted and closed. Every
one of those steps already leaves a message behind — a root note naming the
conversation it was opened for, the owner's `Message received` ack when its
listener picks a post up, its progress lines, its answer, `[state]` words,
the requester's `[served]` mark once a callback has been dealt with. Nobody
could *ask* for them as one answer, so a stall was found by a person reading
topics one at a time: adventure_game p3 lost 24 minutes to a start that was
reported and never posted, and the report was the only thing that said
otherwise.

`trace(client, message_id)` follows those records from one message down:

- the **conversation** the message is in (wherever it is now: an id survives
  a rename and a resolve);
- every conversation opened **on its behalf** — a topic carrying a root note
  (`[selfnote][rootchat] <channel>/<topic>`) that names it, *whoever* wrote
  the note — recursively, so a mission's task topics hang under the
  `workplan-` topic whose planner anchored them there;
- for each, one **state** decided from the posts alone:

  | state | what the conversation shows |
  |---|---|
  | `not_started` | its owner opened it and nobody has posted into it since — a task or run nobody has started |
  | `queued` | somebody posted after the owner's last word and the owner's listener has not acknowledged it |
  | `executing` | the owner acknowledged the newest post and has not answered yet (the age says for how long) |
  | `awaiting_requester` | the owner answered last; the requester has not taken it up |
  | `awaiting_delivery` | the owner answered the requester by name and the requester's listener has not marked the answer served |
  | `awaiting_human` | the conversation a human asked in: the agent answered last |
  | `failed` | the newest word is a failure notice (a run that produced no reply, a refused start) |
  | `done` / `cancelled` | the owner's `[state]` note says so |
  | `unobservable` | the conversation could not be read; nothing is concluded about the work |

It is a **read**: it posts nothing, serves nobody, and a conversation it
could not read is `unobservable` — never "not started". It also does not
claim that a worker is alive: `executing` means an acknowledgement exists
and no answer yet, and the age beside it is the evidence, not a promise.

The state is decided from what each message *is*, not from topic names:
identity notes (`[mission]`, `[task]`, `[asset]`, `[assetrun]`, `[change]`)
and acks say who owns a conversation; `[state]` words are the owners' own
record (plus the words a requester writes on acceptance). A topic name is
display only.

Cost: one realm-wide note search, one message lookup, and one history read
per conversation in the tree (two when a name has to be followed across the
`✔ ` rename). `calls` in the result counts them.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .agent import SWEEP_ACK, is_ack
from .selfnote import (
    MOVED_TAG,
    ROOTCHAT_TAG,
    SERVED_TAG,
    Conversation,
    is_speech,
    parse_note,
    parse_rootchat,
    parse_rootchat_moved,
    parse_served,
)
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipError, ZulipRejected, channel_name

__all__ = [
    "Node",
    "Trace",
    "STATES",
    "trace",
    "trace_lines",
]

#: The whole vocabulary, in the order a reader should worry about them.
STATES = (
    "failed",
    "not_started",
    "queued",
    "awaiting_delivery",
    "executing",
    "awaiting_requester",
    "awaiting_human",
    "unobservable",
    "done",
    "cancelled",
)

#: Notes that say "this conversation is a unit of work, and I own it".
IDENTITY_TAGS = ("mission", "task", "asset", "assetrun", "change")
STATE_TAG = "state"
#: `agag.chat.OPFAIL_TAG`, spelled here so the reader needs no CLI import.
OPFAIL_TAG = "opfail"
DONE_WORDS = frozenset({"completed", "accepted", "done", "delivered"})
CANCELLED_WORDS = frozenset({"cancelled", "replaced", "retired"})
#: A post by the owner whose text begins with one of these is a failure
#: notice rather than an answer. Each is what a listener already posts:
#: `agag.reply.no_reply_notice`, autolab's previous-work gate, the send refusal.
FAILURE_PREFIXES = (
    "(this run produced no reply",
    "Please complete previous work",
)
#: autolab's live progress lines (`RunProgress`): evidence of execution, not
#: an answer.
PROGRESS_LINE = re.compile(r"^(🔧|💬)")
MENTION = re.compile(r"@\*\*(?P<name>[^*|]+?)(?:\|\d+)?\*\*")

HISTORY = 200
NOTE_HISTORY = 1000
MAX_DEPTH = 4


@dataclass
class Node:
    """One conversation of the tree and what its posts say about it."""

    channel: str
    topic: str
    state: str
    detail: str = ""
    identity: str = ""
    owner: str = ""
    note_state: str = ""
    requested_by: list[str] = field(default_factory=list)
    evidence: list[int] = field(default_factory=list)
    last_activity: int = 0
    #: `[selfnote][opfail]` notes in this conversation: operations a run
    #: serving it tried and that were refused or ended uncertain.
    failures: list[str] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trace:
    """The answer: the tree, when it was observed, and what it cost."""

    root: Node | None
    origin: int
    observed_at: int
    calls: int = 0
    problem: str = ""

    def as_dict(self) -> dict:
        return {
            "schema": "agag.trace.v1",
            "origin": self.origin,
            "observed_at": self.observed_at,
            "calls": self.calls,
            "problem": self.problem,
            "root": self.root.as_dict() if self.root else None,
        }

    def nodes(self) -> Iterable[Node]:
        stack = [self.root] if self.root else []
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


class _Reader:
    """Every Zulip read the trace makes, counted, with one rule for failures:
    a read that got no answer is `None`, a refusal is an empty conversation."""

    def __init__(self, client):
        self.client = client
        self.calls = 0

    def message(self, message_id: int) -> dict | None | bool:
        """The message, `None` when Zulip says it is gone, `False` when the
        lookup got no answer."""
        self.calls += 1
        try:
            return self.client.message(int(message_id), strict=True)
        except ZulipError:
            return False

    def history(self, channel: str, topic: str) -> list[dict] | None:
        self.calls += 1
        try:
            return list(self.client.topic_history(channel, topic, num_before=HISTORY))
        except ZulipRejected:
            return []
        except ZulipError:
            return None

    def notes(self, tag: str) -> list[dict] | None:
        self.calls += 1
        try:
            return list(self.client.public_notes(tag, NOTE_HISTORY))
        except ZulipError:
            return None


def _bare(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def _key(channel: str, topic: str) -> tuple[str, str]:
    return (channel, _bare(topic))


def _sender(message: dict) -> str:
    return str(message.get("sender_full_name") or message.get("sender_id") or "?")


def _anchored_children(notes: list[dict]) -> dict[tuple[str, str], list[tuple[str, str, dict]]]:
    """home (channel, bare topic) → [(channel, live topic, note)] of the topics
    anchored to it. A deliberate move by the same author replaces its ordinary
    note for that topic (`effective_rootchat`'s rule, per author)."""
    per_author: dict[tuple[str, str, int], tuple[Conversation, dict, bool]] = {}
    for message in notes:
        if message.get("type") not in (None, "stream"):
            continue
        content = message.get("content")
        moved = parse_rootchat_moved(content)
        home = moved if moved is not None else parse_rootchat(content)
        if home is None:
            continue
        channel = channel_name(message)
        topic = str(message.get("subject") or "")
        if not channel or not topic:
            continue
        author = int(message.get("sender_id") or 0)
        key = (channel, _bare(topic), author)
        previous = per_author.get(key)
        # The earliest ordinary note anchors; the newest move replaces it.
        if previous is None or (
            moved is not None
            and (not previous[2] or int(message.get("id") or 0) >= int(previous[1].get("id") or 0))
        ):
            per_author[key] = (home, message, moved is not None)
    children: dict[tuple[str, str], list[tuple[str, str, dict]]] = {}
    for (channel, _, _), (home, message, _) in per_author.items():
        children.setdefault(_key(home.channel, home.topic), []).append(
            (channel, str(message.get("subject") or ""), message)
        )
    for rows in children.values():
        rows.sort(key=lambda row: int(row[2].get("id") or 0))
    return children


def _home_of(note_message: dict) -> tuple[str, str] | None:
    content = note_message.get("content")
    home = parse_rootchat_moved(content) or parse_rootchat(content)
    return _key(home.channel, home.topic) if home is not None else None


def _identity(messages: list[dict]) -> tuple[str, int | None]:
    """`(label, owner id)` from the first identity note in the conversation."""
    for message in messages:
        for tag in IDENTITY_TAGS:
            value = parse_note(message.get("content"), tag)
            if value is None:
                continue
            owner = int(message.get("sender_id") or 0) or None
            if tag == "mission":
                return f"mission m{message.get('id')} ({value})", owner
            if tag == "task":
                return f"task {value}", owner
            if tag == "asset":
                return f"asset a{message.get('id')} ({value})", owner
            if tag == "assetrun":
                return f"run r{message.get('id')} of a{value}", owner
            return f"{tag} {value}", owner
    return "", None


def _task_serial(identity: str) -> tuple[int, int] | None:
    match = re.match(r"^task (\d+)\s*#\s*(\d+)$", identity)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _note_state(messages: list[dict], owner: int | None) -> tuple[str, int | None]:
    """The newest `[state]` word by the owner — or `accepted`/`done`, which a
    requester writes (`EXTERNAL_STATES` in autolab and forge)."""
    for message in reversed(messages):
        value = parse_note(message.get("content"), STATE_TAG)
        if value is None:
            continue
        word = value.split()[0].lower() if value.split() else ""
        if owner is None or message.get("sender_id") == owner or word in ("accepted", "done"):
            return word, int(message.get("id") or 0)
    return "", None


def _is_progress(content: str) -> bool:
    lines = [line for line in str(content or "").splitlines() if line.strip()]
    return bool(lines) and all(PROGRESS_LINE.match(line.strip()) for line in lines)


def _is_failure(content: str) -> bool:
    text = str(content or "").strip()
    return any(text.startswith(prefix) or f"\n{prefix}" in text for prefix in FAILURE_PREFIXES)


def _owner_by_ack(messages: list[dict]) -> int | None:
    for message in reversed(messages):
        if is_ack(str(message.get("content") or "")):
            return int(message.get("sender_id") or 0) or None
    return None


def _served_marks(home_messages: list[dict] | None, remote: tuple[str, str]) -> int:
    """Highest `[served] <remote> <id>` in the requester's home, 0 when none."""
    best = 0
    for message in home_messages or ():
        parsed = parse_served(message.get("content"))
        if parsed is None:
            continue
        conversation, up_to = parsed
        if _key(conversation.channel, conversation.topic) == remote:
            best = max(best, int(up_to))
    return best


def _age(now: int, timestamp: int) -> str:
    if not timestamp:
        return "?"
    seconds = max(0, now - int(timestamp))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def classify(
    messages: list[dict] | None,
    *,
    human: bool = False,
    now: int | None = None,
    home_messages: dict[int, list[dict] | None] | None = None,
    homes: dict[int, tuple[str, str]] | None = None,
    here: tuple[str, str] = ("", ""),
) -> tuple[str, str, str, str, list[int], int]:
    """`(state, detail, identity, owner name, evidence ids, last activity)`
    for one conversation's messages (oldest first).

    `human` marks the conversation a human asked in: there the agent that
    acknowledges posts is the owner, and its answer being last means the
    human is the one to act. `home_messages` / `homes` (requester id → its
    home's messages / its home) let an answer addressed to a requester be
    checked against that requester's `[served]` marks.
    """
    now = int(now if now is not None else time.time())
    if messages is None:
        return "unobservable", "could not be read", "", "", [], 0
    identity, owner = _identity(messages)
    if owner is None:
        owner = _owner_by_ack(messages)
    owner_name = next((_sender(m) for m in messages if m.get("sender_id") == owner), "") if owner else ""
    last_activity = int(messages[-1].get("timestamp") or 0) if messages else 0

    word, word_id = _note_state(messages, owner)
    if word in DONE_WORDS:
        return "done", word, identity, owner_name, [word_id] if word_id else [], last_activity
    if word in CANCELLED_WORDS:
        return "cancelled", word, identity, owner_name, [word_id] if word_id else [], last_activity

    speech = [m for m in messages if is_speech(m)]
    others = [m for m in speech if m.get("sender_id") != owner]
    owned = [m for m in speech if owner is not None and m.get("sender_id") == owner]
    acks = [m for m in owned if is_ack(str(m.get("content") or ""))]
    answers = [
        m for m in owned
        if not is_ack(str(m.get("content") or "")) and not _is_progress(m.get("content"))
    ]
    progress = [m for m in owned if _is_progress(m.get("content"))]

    def mid(message: dict | None) -> int:
        return int(message.get("id") or 0) if message else 0

    last_other = others[-1] if others else None
    last_answer = answers[-1] if answers else None
    last_ack = acks[-1] if acks else None

    if owner is None:
        if last_other is None:
            return "not_started", "nothing has been said here", identity, "", [], last_activity
        return "queued", "no agent has acknowledged it", identity, "", [mid(last_other)], last_activity

    if last_answer is not None and mid(last_answer) > mid(last_other) and _is_failure(last_answer.get("content")):
        text = str(last_answer.get("content") or "").strip().splitlines()[0][:80]
        return "failed", text, identity, owner_name, [mid(last_answer)], last_activity

    if last_other is None:
        if human:
            return "awaiting_human", "only the agent has spoken", identity, owner_name, [], last_activity
        where = f" (opened by {owner_name})" if owner_name else ""
        return (
            "not_started",
            f"nobody has posted here since it was opened{where}; a post here starts it",
            identity, owner_name, [mid(answers[0])] if answers else [], last_activity,
        )

    if mid(last_other) > mid(last_answer):
        if last_ack is not None and mid(last_ack) > mid(last_other):
            newest = max([last_ack, *progress[-1:]], key=mid)
            return (
                "executing",
                f"acknowledged #{mid(last_ack)}; last sign of work {_age(now, int(newest.get('timestamp') or 0))} ago",
                identity, owner_name, [mid(last_other), mid(last_ack)], int(newest.get("timestamp") or 0),
            )
        return (
            "queued",
            f"#{mid(last_other)} by {_sender(last_other)} not acknowledged for {_age(now, int(last_other.get('timestamp') or 0))}",
            identity, owner_name, [mid(last_other)], last_activity,
        )

    # The owner answered last.
    if human:
        return (
            "awaiting_human",
            f"{owner_name} answered #{mid(last_answer)} {_age(now, int(last_answer.get('timestamp') or 0))} ago",
            identity, owner_name, [mid(last_answer)], last_activity,
        )
    named = {match.group("name").strip() for match in MENTION.finditer(str(last_answer.get("content") or ""))}
    for requester_id, home in (homes or {}).items():
        requester_names = {_sender(m) for m in others if m.get("sender_id") == requester_id}
        if not requester_names or not (requester_names & named):
            continue
        served = _served_marks((home_messages or {}).get(requester_id), here)
        if served >= mid(last_answer):
            return (
                "awaiting_requester",
                f"{owner_name} answered #{mid(last_answer)}; {', '.join(sorted(requester_names))} has taken it up (served up to {served})",
                identity, owner_name, [mid(last_answer)], last_activity,
            )
        return (
            "awaiting_delivery",
            f"{owner_name} answered #{mid(last_answer)} naming {', '.join(sorted(requester_names))}; "
            f"not marked served in {home[0]}/{home[1]} after {_age(now, int(last_answer.get('timestamp') or 0))}",
            identity, owner_name, [mid(last_answer)], last_activity,
        )
    return (
        "awaiting_requester",
        f"{owner_name} answered #{mid(last_answer)} {_age(now, int(last_answer.get('timestamp') or 0))} ago",
        identity, owner_name, [mid(last_answer)], last_activity,
    )


def trace(client, message_id: int, *, now: int | None = None, max_depth: int = MAX_DEPTH) -> Trace:
    """The progress tree below the conversation holding `message_id`."""
    now = int(now if now is not None else time.time())
    reader = _Reader(client)
    result = Trace(root=None, origin=int(message_id), observed_at=now)

    message = reader.message(message_id)
    if message is False:
        result.problem = f"message {message_id} could not be looked up; nothing is concluded"
        result.calls = reader.calls
        return result
    if message is None:
        result.problem = f"message {message_id} does not exist (deleted or never posted)"
        result.calls = reader.calls
        return result
    channel = channel_name(message)
    topic = str(message.get("subject") or "")

    notes = reader.notes(ROOTCHAT_TAG)
    moved = reader.notes(MOVED_TAG)
    index_problem = ""
    if notes is None:
        notes, index_problem = [], "the root-note search got no answer; delegations are not listed"
    children_of = _anchored_children(list(notes) + list(moved or []))

    # Where each anchored conversation is shown: under the **most specific**
    # conversation anchoring it that the tree reaches — a task Front started
    # and autolab's planner opened sits under the mission, not beside it —
    # decided on the index alone, before any history is read.
    depth_of: dict[tuple[str, str], int] = {_key(channel, topic): 0}
    frontier = [_key(channel, topic)]
    while frontier:
        following = []
        for home in frontier:
            for child_channel, child_topic, _ in children_of.get(home, []):
                key = _key(child_channel, child_topic)
                if key not in depth_of:
                    depth_of[key] = depth_of[home] + 1
                    following.append(key)
        frontier = following
    placement: dict[tuple[str, str], tuple[str, str]] = {}
    for home, rows in children_of.items():
        if home not in depth_of:
            continue
        for child_channel, child_topic, _ in rows:
            key = _key(child_channel, child_topic)
            current = placement.get(key)
            if key != home and (current is None or depth_of[home] > depth_of[current]):
                placement[key] = home
    anchors_of: dict[tuple[str, str], list[dict]] = {}
    for home, rows in children_of.items():
        if home in depth_of:
            for child_channel, child_topic, note_message in rows:
                anchors_of.setdefault(_key(child_channel, child_topic), []).append(note_message)

    histories: dict[tuple[str, str], list[dict] | None] = {}

    def read(channel: str, topic: str) -> list[dict] | None:
        key = _key(channel, topic)
        if key not in histories:
            messages = reader.history(channel, topic)
            if messages == [] and not topic.startswith(RESOLVED_TOPIC_PREFIX):
                messages = reader.history(channel, f"{RESOLVED_TOPIC_PREFIX}{topic}")
            histories[key] = messages
        return histories[key]

    def build(channel: str, topic: str, depth: int, seen: set, human: bool,
              requested: list[tuple[int, str, tuple[str, str]]]) -> Node:
        messages = read(channel, topic)
        homes = {requester: home for requester, _, home in requested}
        home_messages = {requester: read(*home) for requester, _, home in requested}
        state, detail, identity, owner, evidence, last = classify(
            messages, human=human, now=now, home_messages=home_messages, homes=homes,
            here=_key(channel, topic),
        )
        live = topic
        if messages:
            live = str(messages[-1].get("subject") or topic)
        _, owner_id = _identity(messages or [])
        word, _ = _note_state(messages or [], owner_id or _owner_by_ack(messages or []))
        if live.startswith(RESOLVED_TOPIC_PREFIX) and state not in ("done", "cancelled"):
            # A ✔ on unfinished work is either a mistake or a closure nobody
            # recorded; either way the reader must see it.
            detail = f"{detail}; resolved (✔) without a finished state" if detail else "resolved (✔) without a finished state"
        node = Node(
            channel=channel, topic=live, state=state, detail=detail, identity=identity,
            owner=owner, note_state=word, evidence=evidence, last_activity=last,
            failures=[
                f"#{m.get('id')} {_sender(m)}: {value}"
                for m in messages or ()
                if (value := parse_note(m.get("content"), OPFAIL_TAG)) is not None
            ],
        )
        if depth >= max_depth:
            return node
        here = _key(channel, topic)
        seen = seen | {here}
        order: list[tuple[str, str]] = []
        first: dict[tuple[str, str], tuple[str, str]] = {}
        for child_channel, child_topic, _ in children_of.get(here, []):
            key = _key(child_channel, child_topic)
            if key in seen or placement.get(key) != here or key in first:
                continue
            order.append(key)
            first[key] = (child_channel, child_topic)
        for key in order:
            notes_for = anchors_of.get(key, [])
            requesters = []
            for note_message in notes_for:
                home = _home_of(note_message)
                if home is not None:
                    requesters.append((int(note_message.get("sender_id") or 0), _sender(note_message), home))
            child = build(*first[key], depth + 1, seen, False, requesters)
            child.requested_by = [f"{_sender(n)} #{n.get('id')}" for n in notes_for]
            node.children.append(child)
        _order_tasks(node)
        return node

    result.root = build(channel, topic, 0, set(), True, [])
    result.calls = reader.calls
    result.problem = index_problem
    return result


def _order_tasks(node: Node) -> None:
    """Task children in serial order; everything else in the order found."""
    tasks = [(serial, child) for child in node.children if (serial := _task_serial(child.identity))]
    if not tasks:
        return
    others = [child for child in node.children if not _task_serial(child.identity)]
    node.children = others + [child for _, child in sorted(tasks, key=lambda pair: pair[0])]


def next_actions(result: Trace) -> list[str]:
    """What the tree says somebody owes, one line each.

    Only mechanical facts: a task whose predecessors are finished and that
    nobody has posted into; a post nobody acknowledged; an answer the
    requester's listener has not served. Whether a wait is *wrong* is not
    decided here — an elapsed time is a candidate, never a verdict.
    """
    lines: list[str] = []
    for node in result.nodes():
        tasks = [child for child in node.children if _task_serial(child.identity)]
        if tasks and node.note_state not in ("done", "cancelled", "replaced"):
            finished = True
            for child in tasks:
                if child.state == "not_started" and finished and node.note_state == "started":
                    lines.append(
                        f"{child.channel}/{child.topic}: {child.identity} has no post since it was opened, "
                        "and every task before it is finished"
                    )
                    break
                finished = finished and child.state in ("done", "cancelled")
        if node.state == "queued":
            lines.append(f"{node.channel}/{node.topic}: {node.detail}")
        if node.state == "awaiting_delivery":
            lines.append(f"{node.channel}/{node.topic}: {node.detail}")
        if node.state == "failed":
            lines.append(f"{node.channel}/{node.topic}: {node.detail}")
    return lines


def trace_lines(result: Trace) -> list[str]:
    """The tree as text: one line per conversation, indented by depth."""
    when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(result.observed_at))
    lines = [f"trace from message {result.origin}, observed {when} ({result.calls} Zulip calls)"]
    if result.problem:
        lines.append(f"! {result.problem}")
    if result.root is None:
        return lines

    def walk(node: Node, depth: int) -> None:
        pad = "  " * depth
        label = f"{node.channel}/{node.topic}"
        extra = f" — {node.identity}" if node.identity else ""
        owner = f" [{node.owner}]" if node.owner else ""
        lines.append(f"{pad}{'└ ' if depth else ''}{label}{extra}{owner}: {node.state.upper()}")
        detail = node.detail
        if node.note_state and node.note_state not in detail:
            detail = f"{detail}; state note: {node.note_state}" if detail else f"state note: {node.note_state}"
        if detail:
            lines.append(f"{pad}{'  ' if depth else ''}  {detail}")
        if node.requested_by:
            lines.append(f"{pad}{'  ' if depth else ''}  anchored by {', '.join(node.requested_by)}")
        for failure in node.failures:
            lines.append(f"{pad}{'  ' if depth else ''}  ! operation failed: {failure}")
        for child in node.children:
            walk(child, depth + 1)

    walk(result.root, 0)
    owed = next_actions(result)
    if owed:
        lines.append("")
        lines.append("owed now:")
        lines.extend(f"  - {line}" for line in owed)
    return lines
