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
    owed_start,
    parse_note,
    parse_start,
    parse_rootchat,
    parse_rootchat_moved,
    parse_served,
    replaced_anchor,
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
#: A task its owner will not start by itself: the requester asked it to wait.
HELD_WORD = "held"
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
    #: A message id that is this conversation whatever it is called: its
    #: identity note, else the root note that opened it for its parent, else
    #: its oldest post read. The key every consumer of a trace dedupes on
    #: (robust_workflow p2 step 2) — a name is display only.
    anchor: int = 0
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


def _by_id(messages: list[dict], message_id: int) -> dict:
    return next((m for m in messages if int(m.get("id") or 0) == int(message_id)), {})


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


def _served_marks(home_messages: list[dict] | None, remote: tuple[str, str],
                  remote_ids: frozenset[int] = frozenset(), complete: bool = False) -> int:
    """Highest `[served] <remote> <id>` in the requester's home, 0 when none.

    A mark covers this conversation when the post it names **is in it**
    (robust_workflow p2 step 2): the id is the post the mark was written for,
    and it moves with every rename, where the name written beside it does
    not. The name decides only for a post older than the history read — a
    long conversation whose beginning the read did not reach."""
    oldest = min(remote_ids) if remote_ids else 0
    best = 0
    for message in home_messages or ():
        parsed = parse_served(message.get("content"))
        if parsed is None:
            continue
        conversation, up_to = parsed
        if int(up_to) in remote_ids:
            covers = True
        elif not complete and remote_ids and int(up_to) < oldest:
            covers = _key(conversation.channel, conversation.topic) == remote
        else:
            covers = not remote_ids and _key(conversation.channel, conversation.topic) == remote
        if covers:
            best = max(best, int(up_to))
    return best


def _unserved_answer(messages, owner, homes, home_messages, here, here_ids=frozenset(), complete=False,
                     receipts_from=0):
    """`(answer, requester name, home)` for the owner's newest answer that
    names a requester whose home has not marked it served, or None."""
    if owner is None or not homes:
        return None
    answers = [m for m in messages if m.get("sender_id") == owner and is_speech(m)
               and not is_ack(str(m.get("content") or "")) and not _is_progress(m.get("content"))]
    if not answers:
        return None
    answer = answers[-1]
    named = {match.group("name").strip() for match in MENTION.finditer(str(answer.get("content") or ""))}
    for requester_id, home in homes.items():
        names = {_sender(m) for m in messages if m.get("sender_id") == requester_id}
        if not names or not (names & named):
            continue
        if not _taken_up((home_messages or {}).get(requester_id), requester_id, int(answer.get("id") or 0), here,
                         here_ids, complete, receipts_from):
            return answer, ", ".join(sorted(names)), home
    return None


def _taken_up(home_messages, requester_id: int, answer_id: int, here, here_ids=frozenset(), complete=False,
              receipts_from: int = 0) -> bool:
    """Whether the requester dealt with an answer: its home holds a served
    mark covering it — the receipt its listener writes after a *delivered*
    serving, bound to the post that serving processed.

    Nothing weaker counts (robust_workflow p2 step 3). p1 also accepted the
    requester speaking at home after the answer, and a reply to something
    else — a serving whose input ended before the answer arrived — then
    consumed an answer nothing had read (p2 step 1, R8). An answer that
    arrives during a serving stays owed until a serving marks it, which the
    listener does on its next pass."""
    if _served_marks(home_messages, here, here_ids, complete) >= answer_id:
        return True
    if answer_id >= receipts_from:
        return False
    # An answer from before `receipts_from` is read as p1 read it: the
    # listeners of that time left marks one post short of an answer that
    # arrived with its topic's ✔ (fixed in 87ac87e), so the requester
    # speaking at home since is taken as the answer taken up.
    return any(
        m.get("sender_id") == requester_id and is_speech(m) and int(m.get("id") or 0) > answer_id
        and not is_ack(str(m.get("content") or ""))
        for m in home_messages or ()
    )


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
    receipts_from: int = 0,
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
    here_ids = frozenset(int(m.get("id") or 0) for m in messages)
    complete = len(messages) < HISTORY
    identity, owner = _identity(messages)
    if owner is None:
        owner = _owner_by_ack(messages)
    owner_name = next((_sender(m) for m in messages if m.get("sender_id") == owner), "") if owner else ""
    last_activity = int(messages[-1].get("timestamp") or 0) if messages else 0

    word, word_id = _note_state(messages, owner)
    if word in DONE_WORDS:
        # Finished in the owner's record is not received by the requester:
        # forge writes `delivered` the moment it posts, and when the asker's
        # listener is down that answer is owed all the same (robust_workflow
        # p1 step 5, trial N2 — missed until this).
        owed = _unserved_answer(messages, owner, homes, home_messages, here, here_ids, complete, receipts_from)
        if owed is not None:
            answer, requester, home = owed
            return (
                "awaiting_delivery",
                f"{word}; {owner_name} answered #{answer['id']} naming {requester}; not marked served in "
                f"{home[0]}/{home[1]} after {_age(now, int(answer.get('timestamp') or 0))}",
                identity, owner_name, [int(answer["id"])], int(answer.get("timestamp") or 0),
            )
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
        starts = [m for m in messages
                  if m.get("sender_id") == owner and parse_start(m.get("content")) is not None]
        pending = owed_start(messages, owner, is_ack=is_ack)
        if pending is not None:
            acked = [m for m in acks if mid(m) > pending["id"]]
            if acked:
                newest = max([acked[-1], *progress[-1:]], key=mid)
                return (
                    "executing",
                    f"started by {owner_name} at #{pending['id']} for {pending['sender_full_name']}; "
                    f"last sign of work {_age(now, int(newest.get('timestamp') or 0))} ago",
                    identity, owner_name, [pending["id"], mid(acked[-1])], int(newest.get("timestamp") or 0),
                )
            return (
                "queued",
                f"started by {owner_name} at #{pending['id']}, not picked up for "
                f"{_age(now, int(_by_id(messages, pending['id']).get('timestamp') or 0))}",
                identity, owner_name, [pending["id"]], last_activity,
            )
        if starts and last_answer is not None and mid(last_answer) > mid(starts[-1]):
            start = parse_start(starts[-1].get("content"))
            return (
                "awaiting_requester",
                f"started by {owner_name} at #{mid(starts[-1])}; answered #{mid(last_answer)} "
                f"for {start[2] if start else 'the requester'} {_age(now, int(last_answer.get('timestamp') or 0))} ago",
                identity, owner_name, [mid(last_answer)], last_activity,
            )
        if word == HELD_WORD:
            return (
                "awaiting_requester",
                "held: the requester asked for this task to wait; a post here starts it",
                identity, owner_name, [word_id] if word_id else [], last_activity,
            )
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
        served = _served_marks((home_messages or {}).get(requester_id), here, here_ids, complete)
        if _taken_up((home_messages or {}).get(requester_id), requester_id, mid(last_answer), here, here_ids, complete,
                     receipts_from):
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


@dataclass
class _Link:
    """One effective root note: the conversation it is in now (`child_*`,
    from the note's own current topic) was opened on behalf of `home`, as the
    note names it — with the anchor it carries, when it carries one."""

    note_id: int
    author: int
    author_name: str
    child_channel: str
    child_topic: str
    home: Conversation
    message: dict


def _links(notes: list[dict]) -> list[_Link]:
    """The effective root notes, per author and conversation: the earliest
    ordinary note anchors, the newest deliberate move replaces it
    (`effective_rootchat`'s rule, per author)."""
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
        slot = (channel, _bare(topic), author)
        previous = per_author.get(slot)
        if previous is None or (
            moved is not None
            and (not previous[2] or int(message.get("id") or 0) >= int(previous[1].get("id") or 0))
        ):
            per_author[slot] = (home, message, moved is not None)
    links = [
        _Link(int(message.get("id") or 0), author, _sender(message), channel, str(message.get("subject") or ""),
              home, message)
        for (channel, _, author), (home, message, _) in per_author.items()
    ]
    links.sort(key=lambda link: link.note_id)
    return links


def _settle(link: _Link, named: tuple[str, str], messages: list[dict] | None, locate) -> tuple[str, str] | None:
    """Which conversation a root note means, given the one its name points
    at now (`named`, read as `messages`).

    robust_workflow p2 step 2. The name was right when the note was
    written; since then the conversation may have been renamed, ✔'d,
    retired, or its name taken by another. In order:

    1. **The anchor decides** when the note carries one: a post in the
       named conversation means it is that one; a post found elsewhere in
       the same channel means it moved there. An anchor in another channel
       is not home's (callback servings used to write the calling post's
       id) and is ignored.
    2. Without a usable anchor, **a note older than the conversation now
       holding the name cannot mean it**: that conversation began after the
       note was written. It means the one that held the name before, which
       is known only when the holder says so — `[replaces] <id>`, one hop.
       Otherwise the note is attached nowhere, rather than to a stranger.
    3. Otherwise the name stands. A read that failed or did not reach the
       beginning cannot contradict it.
    """
    if messages is None:
        return named
    ids = {int(m.get("id") or 0) for m in messages}
    complete = len(messages) < HISTORY
    oldest = min(ids) if ids else 0
    anchor = int(link.home.anchor or 0)
    if anchor:
        if anchor in ids:
            return named
        if complete or anchor >= oldest:
            where = locate(anchor)
            if where is not None and where[0] == link.home.channel:
                return _key(*where)
    if not complete:
        return named
    if not ids:
        return None
    if link.note_id > oldest:
        return named
    predecessor = replaced_anchor(messages)
    if predecessor is not None:
        where = locate(predecessor)
        if where is not None:
            return _key(*where)
    return None


def _identity_id(messages: list[dict]) -> int:
    for message in messages:
        for tag in IDENTITY_TAGS:
            if parse_note(message.get("content"), tag) is not None:
                return int(message.get("id") or 0)
    return 0


def trace(client, message_id: int, *, now: int | None = None, max_depth: int = MAX_DEPTH,
          receipts_from: int = 0) -> Trace:
    """The progress tree below the conversation holding `message_id`.

    `receipts_from`: answers older than this id are taken up as p1 read them
    (a served mark, or the requester speaking at home since); newer ones
    only by a served mark. 0, the default, is the strict rule throughout."""
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
    links = _links(list(notes) + list(moved or []))

    root_key = _key(channel, topic)
    #: The name each conversation is read under: where its notes are now.
    names: dict[tuple[str, str], str] = {root_key: topic}
    for link in links:
        names.setdefault(_key(link.child_channel, link.child_topic), link.child_topic)
    histories: dict[tuple[str, str], list[dict] | None] = {}

    def read(key: tuple[str, str]) -> list[dict] | None:
        if key not in histories:
            name = names.get(key, key[1])
            messages = reader.history(key[0], name)
            if messages == [] and not name.startswith(RESOLVED_TOPIC_PREFIX):
                messages = reader.history(key[0], f"{RESOLVED_TOPIC_PREFIX}{name}")
            elif messages == [] and name != key[1]:
                messages = reader.history(key[0], key[1])
            histories[key] = messages
        return histories[key]

    def locate(anchor: int) -> tuple[str, str] | None:
        found = reader.message(anchor)
        if not found:
            return None
        where = (channel_name(found), str(found.get("subject") or ""))
        if not where[0] or not where[1]:
            return None
        names.setdefault(_key(*where), where[1])
        return where

    # Which conversation each note means (`_settle`), decided against the
    # histories the tree needs anyway: a note is checked when the
    # conversation its name points at is reached, and a note re-homed there
    # may make another conversation reachable — so, to a fixed point.
    home_of: dict[int, tuple[str, str] | None] = {
        link.note_id: _key(link.home.channel, link.home.topic) for link in links
    }
    settled: set[int] = set()
    for _ in range(MAX_DEPTH + 2):
        children_of: dict[tuple[str, str], list[_Link]] = {}
        for link in links:
            home = home_of[link.note_id]
            if home is not None:
                children_of.setdefault(home, []).append(link)
        reachable, frontier = {root_key}, [root_key]
        while frontier:
            following = []
            for home in frontier:
                for link in children_of.get(home, []):
                    child = _key(link.child_channel, link.child_topic)
                    if child not in reachable:
                        reachable.add(child)
                        following.append(child)
            frontier = following
        changed = False
        held_by: dict[int, tuple[str, str]] = {}
        for home in sorted(reachable):
            messages = read(home)
            for message in messages or ():
                held_by[int(message.get("id") or 0)] = home
            waiting = [link for link in children_of.get(home, []) if link.note_id not in settled]
            for link in waiting:
                settled.add(link.note_id)
                verdict = _settle(link, home, messages, locate)
                if verdict != home:
                    home_of[link.note_id] = verdict
                    changed = True
        # A note naming a conversation the tree does not reach, whose anchor
        # is a post in one it does: that conversation was renamed since the
        # note was written. Membership needs no lookup.
        for link in links:
            if link.note_id in settled or home_of[link.note_id] in reachable:
                continue
            anchor = int(link.home.anchor or 0)
            if anchor in held_by and held_by[anchor][0] == link.home.channel:
                settled.add(link.note_id)
                home_of[link.note_id] = held_by[anchor]
                changed = True
        if not changed:
            break

    children_of = {}
    for link in links:
        home = home_of[link.note_id]
        if home is not None and _key(link.child_channel, link.child_topic) != home:
            children_of.setdefault(home, []).append(link)

    # Where each anchored conversation is shown: under the **most specific**
    # conversation anchoring it that the tree reaches — a task Front started
    # and autolab's planner opened sits under the mission, not beside it.
    depth_of: dict[tuple[str, str], int] = {root_key: 0}
    frontier = [root_key]
    while frontier:
        following = []
        for home in frontier:
            for link in children_of.get(home, []):
                key = _key(link.child_channel, link.child_topic)
                if key not in depth_of:
                    depth_of[key] = depth_of[home] + 1
                    following.append(key)
        frontier = following
    placement: dict[tuple[str, str], tuple[str, str]] = {}
    anchors_of: dict[tuple[str, str], list[_Link]] = {}
    for home, rows in children_of.items():
        if home not in depth_of:
            continue
        for link in rows:
            key = _key(link.child_channel, link.child_topic)
            anchors_of.setdefault(key, []).append(link)
            current = placement.get(key)
            if current is None or depth_of[home] > depth_of[current]:
                placement[key] = home

    def build(key: tuple[str, str], depth: int, seen: set, human: bool,
              requested: list[tuple[int, str, tuple[str, str]]]) -> Node:
        messages = read(key)
        if not messages and anchors_of.get(key):
            # Its own root notes are in it — that is how it was found — so a
            # read that returns nothing is a read that failed (a mirror that
            # does not hold it, a lost coverage), never an empty conversation
            # and never "not started" (robust_workflow p2 step 1, R1).
            messages = None
        homes = {requester: home for requester, _, home in requested}
        home_messages = {requester: read(home) for requester, _, home in requested}
        state, detail, identity, owner, evidence, last = classify(
            messages, human=human, now=now, home_messages=home_messages, homes=homes, here=key,
            receipts_from=receipts_from,
        )
        live = names.get(key, key[1])
        if messages:
            live = str(messages[-1].get("subject") or live)
        _, owner_id = _identity(messages or [])
        word, _ = _note_state(messages or [], owner_id or _owner_by_ack(messages or []))
        if live.startswith(RESOLVED_TOPIC_PREFIX) and state not in ("done", "cancelled"):
            # A ✔ on unfinished work is either a mistake or a closure nobody
            # recorded; either way the reader must see it.
            detail = f"{detail}; resolved (✔) without a finished state" if detail else "resolved (✔) without a finished state"
        opened_by = [link.note_id for link in anchors_of.get(key, [])]
        stable = [i for i in (_identity_id(messages or []), *opened_by) if i]
        anchor = min(stable) if stable else min((int(m.get("id") or 0) for m in messages or ()), default=0)
        if depth == 0:
            anchor = min((int(m.get("id") or 0) for m in messages or ()), default=anchor)
        node = Node(
            channel=key[0], topic=live, state=state, detail=detail, identity=identity, anchor=anchor,
            owner=owner, note_state=word, evidence=evidence, last_activity=last,
            failures=[
                f"#{m.get('id')} {_sender(m)}: {value}"
                for m in messages or ()
                if (value := parse_note(m.get("content"), OPFAIL_TAG)) is not None
            ],
        )
        if depth >= max_depth:
            return node
        seen = seen | {key}
        order: list[tuple[str, str]] = []
        for link in children_of.get(key, []):
            child = _key(link.child_channel, link.child_topic)
            if child in seen or placement.get(child) != key or child in order:
                continue
            order.append(child)
        for child in order:
            notes_for = anchors_of.get(child, [])
            requesters = []
            for link in notes_for:
                home = home_of[link.note_id]
                if home is not None:
                    requesters.append((link.author, link.author_name, home))
            built = build(child, depth + 1, seen, False, requesters)
            built.requested_by = [f"{link.author_name} #{link.note_id}" for link in notes_for]
            node.children.append(built)
        _order_tasks(node)
        return node

    result.root = build(root_key, 0, set(), True, [])
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
                        f"{child.channel}/{child.topic}: {child.identity} has no post and no start since it "
                        "was opened, and every task before it is finished"
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
    lines = [f"trace from message {result.origin}, observed {when} ({result.calls} reads)"]
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


# --- reading the trace off a mirror (robust_workflow p1 step 4) -------------------


class MirrorReader:
    """The trace's four reads answered from an `agag.mirror.Mirror`: no Zulip
    call at all. A process that already holds a mirror — every listener, the
    Observer's worker — traces for free, which is what makes looking at every
    active request on a timer affordable."""

    def __init__(self, mirror):
        self.mirror = mirror

    def message(self, message_id: int, *, strict: bool = False) -> dict | None:
        found = self.mirror.message(int(message_id))
        return found.as_zulip() if found is not None and not getattr(found, "deleted", False) else None

    def topic_history(self, channel: str, topic: str, num_before: int = HISTORY) -> list[dict]:
        return self.mirror.history(channel, topic, num_before=num_before, across_resolve=False)

    def public_notes(self, tag: str, num_before: int = NOTE_HISTORY) -> list[dict]:
        found = []
        for row in self.mirror.notes(tag=tag)[-num_before:]:
            message = self.mirror.message(row.message_id)
            if message is not None:
                found.append(message.as_zulip())
        return found


# --- stall candidates: the mechanical half of detection ---------------------------


@dataclass(frozen=True)
class Candidate:
    """One thing the records say is owed and has not happened.

    Elapsed time made it a candidate; it is not a verdict. `judgment` says
    the facts alone cannot tell a stall from a legitimate wait (a ✔ on live
    work may be a correction or a mistake; a long silence may be a long job),
    and a reader with judgment — the Observer — decides those. The rest are
    mechanical: a task nobody started after its predecessor closed, a post
    nobody acknowledged, an answer nobody served, a failure notice.
    """

    kind: str
    channel: str
    topic: str
    identity: str
    fact: str
    responsible: str
    next_action: str
    since: int
    evidence: tuple[int, ...] = ()
    judgment: bool = False
    #: The stalled conversation's `Node.anchor`: what it *is*, whatever it
    #: is called by the time anybody reads this.
    anchor: int = 0

    @property
    def key(self) -> str:
        """Kind, the conversation by anchor (by name only when the trace had
        none), and the evidence post."""
        evidence = self.evidence[0] if self.evidence else 0
        where = f"a{self.anchor}" if self.anchor else f"{self.channel}/{_bare(self.topic)}"
        return f"{self.kind}:{where}:{evidence}"


#: Seconds before each kind is a candidate — the grace a healthy system
#: needs to do the thing by itself. Step 5 of the episode sets the targets.
THRESHOLDS = {
    "unstarted": 180,
    "unacknowledged": 300,
    "undelivered": 300,
    "failed": 60,
    "resolved_live": 60,
    "silent": 2700,
}


def stall_candidates(result: Trace, now: int | None = None, thresholds: dict | None = None) -> list[Candidate]:
    """Everything in the tree that is owed and overdue."""
    now = int(now if now is not None else time.time())
    limits = {**THRESHOLDS, **(thresholds or {})}
    found: list[Candidate] = []

    def overdue(kind: str, since: int) -> bool:
        return bool(since) and now - int(since) >= limits[kind]

    root = result.root
    for node in result.nodes():
        is_root = node is root
        resolved = node.topic.startswith(RESOLVED_TOPIC_PREFIX)
        if node.state == "queued" and overdue("unacknowledged", node.last_activity):
            found.append(Candidate(
                "unacknowledged", node.channel, node.topic, node.identity, node.detail,
                node.owner or "the agent that owns this conversation",
                f"{node.owner or 'its owner'}'s listener picks up #{node.evidence[0] if node.evidence else '?'} and serves it",
                node.last_activity, tuple(node.evidence), anchor=node.anchor,
            ))
        if is_root:
            continue
        if node.state == "awaiting_delivery" and overdue("undelivered", node.last_activity):
            requester = node.requested_by[0].split(" #")[0] if node.requested_by else "the requester"
            found.append(Candidate(
                "undelivered", node.channel, node.topic, node.identity, node.detail, requester,
                f"{requester} is served with the answer #{node.evidence[0] if node.evidence else '?'} and takes it up",
                node.last_activity, tuple(node.evidence), anchor=node.anchor,
            ))
        if node.state == "failed" and overdue("failed", node.last_activity):
            found.append(Candidate(
                "failed", node.channel, node.topic, node.identity, node.detail,
                node.owner or "the owner",
                "whoever asked decides what to do about the failure (retry, change the request, or stop and say so)",
                node.last_activity, tuple(node.evidence), anchor=node.anchor,
            ))
        if resolved and node.state in ("queued", "executing", "awaiting_delivery", "awaiting_requester") \
                and overdue("resolved_live", node.last_activity):
            found.append(Candidate(
                "resolved_live", node.channel, node.topic, node.identity,
                f"✔ while {node.state.replace('_', ' ')}: {node.detail}",
                "whoever resolved it",
                f"`agentchat unresolve {node.channel} {_bare(node.topic)}` if the ✔ was a mistake; nothing if it was a deliberate close",
                node.last_activity, tuple(node.evidence) or (0,), judgment=True, anchor=node.anchor,
            ))
        if node.state == "executing" and overdue("silent", node.last_activity):
            found.append(Candidate(
                "silent", node.channel, node.topic, node.identity, node.detail,
                node.owner or "the owner",
                f"{node.owner or 'the owner'} answers, or says the work is still running",
                node.last_activity, tuple(node.evidence), judgment=True, anchor=node.anchor,
            ))
        tasks = [child for child in node.children if _task_serial(child.identity)]
        if tasks and node.note_state == "started":
            finished_at = 0
            for child in tasks:
                if child.state in ("done", "cancelled"):
                    finished_at = max(finished_at, child.last_activity)
                    continue
                if child.state == "not_started" and overdue("unstarted", finished_at or node.last_activity):
                    found.append(Candidate(
                        "unstarted", child.channel, child.topic, child.identity,
                        f"{child.identity} has no post and no start although every task before it is finished",
                        child.owner or node.owner or "the owner",
                        f"{child.identity} starts (a post in {child.channel}/{_bare(child.topic)} starts it)",
                        finished_at or node.last_activity, tuple(child.evidence) or (0,), anchor=child.anchor,
                    ))
                break
    return found
