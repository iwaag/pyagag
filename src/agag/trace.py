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
  | `awaiting_human` | the conversation a human asked in holds a response request (`agag.post`) still pending for its recipient |
  | `answered` | the conversation a human asked in: the agent answered last and asks nobody anything |
  | `failed` | the newest word is a failure notice (a run that produced no reply, a refused start) |
  | `done` / `cancelled` | the owner's `[state]` note says so |
  | `unobservable` | the conversation could not be read; nothing is concluded about the work |

It is a **read**: it posts nothing, serves nobody, and a conversation it
could not read is `unobservable` — never "not started". It also does not
claim that a worker is alive: `executing` means an acknowledgement exists
and no answer yet, and the age beside it is the evidence, not a promise.

Beside the state, each conversation carries two facts that the state used
to be made to stand for (failsafe p1):

- **`execution`** — the owner's newest serving: `open` (acknowledged, and
  nothing has said it ended), `ended` (a reply after the ack says it ends
  that serving, `ag-post … end=<ack>`, or an answer followed it), or
  `unknown` (the owner never acknowledged anything here). An open serving
  is a claim, not proof of life.
- **`holder`** — who holds the next move of unfinished work: `owner`,
  `delegate` (a conversation opened from this one still holds something),
  `requester`, `human`, **`none`**, `unknown` or `done`. `none` is the stall
  nothing else can see: the last serving ended saying only that work goes
  on, and nobody was handed anything (m11741, 2026-09-26).

A post Zulip cut (`[message truncated]`) lost its `ag-post` line with its
tail; it is read as output, never as an answer.

For readers of progress (progress_panel p1, `agag.progress`): a
conversation its owner opens and serves itself (Front's `routinerun-`) is
read from its own acks and ends; a routine run's `ag-routinerun` block is
its end record (`finished`/`ended`); each node carries its record notes
(`records`) and, while an answer waits for delivery, whom it is owed to
(`owed_to`) — the owner of a conversation an answer is owed to holds it.
A requester's `[served]` mark anywhere in the request's tree is its
receipt.

Completion, acceptance and receipt are separate facts (failsafe p6). An
answer without a receipt is owed — whatever its producer's record says —
until a **decision** recorded after it covers it (`decisions`: the
requester's acceptance of that result, the mission's acceptance up to its
shown result, a cancellation above it). Then the unit reads as its record
does and `Node.receipt` keeps the missing receipt as bookkeeping
(`settled`), which `agentchat receipt` repairs; a reconciled receipt
(`[selfnote][receipt]`) covers exactly the answer it names. Every reader —
the panel, Observer, `agentchat trace` — applies this one rule; nothing is
lenient for one of them.

A root note hangs its conversation under its home only when its relation is
**work** (failsafe p6 ex1, `agag.relations`): stated on the note, a
deliberate move, the author's correction, or the author's legacy record. A
`reference` (a citation, a comment) or an `unknown` relation adopts nothing;
the home lists it (`Node.relations`), and an unknown one says how to resolve
it. No history length decides it: p6 guessed from the first post a read
happened to show, and at 200 posts a citation became an adoption.

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
    is_progress,
    is_speech,
    owed_start,
    parse_note,
    parse_start,
    parse_rootchat,
    parse_rootchat_moved,
    parse_receipt,
    parse_served,
    replaced_anchor,
)
from .post import PROGRESS, RESPONSE_REQUEST, is_truncated, parse_post
from .relations import RELATION_TAG, Relation, Relations
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
    "answered",
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
DONE_WORDS = frozenset({"completed", "accepted", "done", "delivered", "finished", "ended"})
CANCELLED_WORDS = frozenset({"cancelled", "replaced", "retired"})
#: `[state]` words a unit's **requester** writes on deciding its result
#: (`agag.acceptance`, the completion door) — never the owner's claim.
DECIDED_WORDS = frozenset({"accepted", "done"})
#: `[selfnote][change] accepted … +shown=<id>` (autolab's close-out, bound to
#: the result shown for review) and `[selfnote][acceptance] #<evidence> by
#: <user> (<name>) [after=#<shown>]` (`agag.acceptance`).
_SHOWN = re.compile(r"\+shown=(\d+)")
_ACCEPTANCE_RECORD = re.compile(r"^#(?P<evidence>\d+) by (?P<by>\d+)(?: \((?P<name>[^)]*)\))?"
                                r"(?: for (?P<for>\d+)(?: \((?P<for_name>[^)]*)\))?)?(?: after=#(?P<after>\d+))?")
#: A routine run's end is its owner's `ag-routinerun` block
#: (`ag.routinerun-finish.v1`, agfront `routine.record_text`), echoed into the
#: run topic before the ✔: `finished` when the routine's goal was reached,
#: `ended` when the run stopped without it (progress_panel p1). Until then a
#: ✔'d run read as "resolved without a finished state".
FINISH_FENCE = "ag-routinerun"
FINISH_SCHEMA = "ag.routinerun-finish.v1"
_FINISH_BLOCK = re.compile(r"```" + FINISH_FENCE + r"[ \t]*\n(?P<body>.*?)\n```", re.S)
#: Notes a reader of progress needs besides the state (progress_panel p1):
#: plan revisions (`doc`), acceptance, the change record, a routine report
#: delivered home, a sage refreshed from its study (`sagesync`, archsage),
#: every `[state]` word in order, and a person's holds and their releases
#: (`agag.holds`, failsafe p6).
RECORD_TAGS = ("doc", "acceptance", "change", "delivered", "sagesync", "state", "hold", "hold-release",
               "disposition", "disposition-reversed")
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
    #: The owner's newest serving: `open`, `ended` or `unknown` (module doc).
    execution: str = "unknown"
    #: Who holds the next move: `owner`, `delegate`, `requester`, `human`,
    #: `none`, `unknown` or `done` (module doc).
    holder: str = "unknown"
    #: The owner's newest acknowledgement here, the post that ended that
    #: serving (0 while it is open or unknown) and when; the owner's newest
    #: sign of work (speech that is not an ack, or a `[change]` note).
    ack: int = 0
    ended_by: int = 0
    ended_at: int = 0
    work: int = 0
    work_at: int = 0
    #: The owner's answer here was taken up by its requester (a served mark
    #: covers it): the move went back to whoever asked.
    taken_up: bool = False
    #: Whom the owner's newest serving asked, by name, in this conversation
    #: and who has not spoken here since — `@**Comfy Notifier** watch …` is
    #: acked with a reaction and answered by a mention later. They hold it.
    waiting_on: list[str] = field(default_factory=list)
    #: What the post that ended the newest serving declared (`ag.post.v1`):
    #: its intent, and whom a response request asked (failsafe p2). A
    #: request to a person or an agent is an explained wait; a report or
    #: nothing at all hands the move to nobody in particular.
    ending_intent: str = ""
    ending_to: int = 0
    ack_at: int = 0
    #: Whom this conversation's newest answer is owed to while it is
    #: `awaiting_delivery`: their listener owes a serving (progress_panel p1).
    owed_to: list[str] = field(default_factory=list)
    #: The record notes in this conversation, oldest first (`RECORD_TAGS`,
    #: plus `finish` for a routine run's end): `{tag, value, id, at, by}`.
    records: list[dict] = field(default_factory=list)
    #: The newest answer here owed to a requester whose receipt is not on
    #: record (failsafe p6): `{answer, at, to, home, state, settled_by}`.
    #: `state` is `missing` (the answer is owed: `awaiting_delivery`) or
    #: `settled` (a decision recorded after it covers it — the requester's
    #: acceptance, a cancellation — so nothing is owed, and the missing
    #: receipt is bookkeeping `agentchat receipt` can repair). Empty when the
    #: answer was received (served, or reconciled) or nothing is owed.
    receipt: dict = field(default_factory=dict)
    #: Root notes naming this conversation that do not make their
    #: conversation its work (failsafe p6 ex1): `{note, channel, topic,
    #: relation, decided_by, author}` — a citation (`reference`), or an
    #: `unknown` relation with the command that resolves it.
    relations: list[dict] = field(default_factory=list)
    #: Who the answers here are owed to, by name — its root notes' authors
    #: and, one hop up, the requester of the conversation it was opened for
    #: (a task its owner started itself): the parties the trace checks
    #: receipts against (failsafe p6 ex1: the panel named the owner).
    requesters: list[str] = field(default_factory=list)
    #: The newest substantive post here — speech that is not an ack; not a
    #: selfnote, not a system notice (failsafe p6 ex1: a disposition covers
    #: activity up to its `upto`, and bookkeeping is not activity).
    last_substantive: int = 0
    #: Every substantive post here as `[id, sender id, ack its reply ends
    #: (ag-post end=, 0 when none)]` — what a disposition's boundary is
    #: judged against.
    activity: list[list[int]] = field(default_factory=list)
    #: The decision in force for this conversation (`agag.dispositions`):
    #: `{id, kind, covered, unit, says}`; `covered` is false when something
    #: substantive happened here after it.
    disposition: dict = field(default_factory=dict)
    #: Claims here still open (failsafe p7, `agag.claims`): a reply said an
    #: act was done and no record shows it — `{id, reply, attempt, state,
    #: at, by, missing}`, `state` `repairing` (its owner is served once with
    #: the mismatch) or `escalated` (it said it again; the owners are told).
    claims: list[dict] = field(default_factory=list)

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
    #: Root notes into this tree whose relation nothing records (failsafe p6
    #: ex1): each adopts nothing until its author says what it is.
    unknown_relations: list[dict] = field(default_factory=list)
    #: The request's dispositions (`agag.dispositions.Disposition`), applied
    #: to the tree.
    dispositions: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "schema": "agag.trace.v1",
            "origin": self.origin,
            "observed_at": self.observed_at,
            "calls": self.calls,
            "problem": self.problem,
            "unknown_relations": list(self.unknown_relations),
            "dispositions": [d.as_dict() for d in self.dispositions],
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


def finish_record(content) -> dict | None:
    """The `ag.routinerun-finish.v1` block in a post, or None."""
    import json

    for match in reversed(list(_FINISH_BLOCK.finditer(str(content or "")))):
        try:
            data = json.loads(match.group("body"))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("schema") == FINISH_SCHEMA and isinstance(data.get("achieved"), bool):
            return data
    return None


def _note_state(messages: list[dict], owner: int | None) -> tuple[str, int | None]:
    """The newest `[state]` word by the owner — or `accepted`/`done`, which a
    requester writes (`EXTERNAL_STATES` in autolab and forge) — or the
    owner's routine-run finish block (`finished`/`ended`)."""
    for message in reversed(messages):
        value = parse_note(message.get("content"), STATE_TAG)
        if value is None:
            if owner is not None and message.get("sender_id") == owner and is_speech(message):
                finish = finish_record(message.get("content"))
                if finish is not None:
                    return ("finished" if finish["achieved"] else "ended"), int(message.get("id") or 0)
            continue
        word = value.split()[0].lower() if value.split() else ""
        if owner is None or message.get("sender_id") == owner or word in ("accepted", "done"):
            return word, int(message.get("id") or 0)
    return "", None


def _is_progress(content: str) -> bool:
    """Progress, or a post Zulip cut: its line went with its tail, and what
    is left is output, never an answer (m11741's #11758/#11759)."""
    return is_progress(content) or is_truncated(content)


def _waiting_on(messages: list[dict], owner: int, ack: int, served: set[str]) -> list[str]:
    """Names the owner mentioned after its newest ack that have not posted
    here since the mention, apart from whoever it serves (`served`)."""
    owner_name = next((_sender(m) for m in messages if m.get("sender_id") == owner), "")
    pending: dict[str, int] = {}
    for message in messages:
        mid = int(message.get("id") or 0)
        if message.get("sender_id") == owner and mid > ack and is_speech(message):
            for match in MENTION.finditer(str(message.get("content") or "")):
                name = match.group("name").strip()
                if name and name != owner_name and name not in served:
                    pending.setdefault(name, mid)
        elif message.get("sender_id") != owner and is_speech(message):
            pending.pop(_sender(message), None)
    return sorted(pending)


def _serving(messages: list[dict] | None, owner: int | None, served: set[str] = frozenset()) -> dict:
    """The owner's newest serving here, from its posts alone.

    `ended` needs positive evidence: the listener's `end=<ack>` on the
    reply that closed it, or — for an owner that posts nothing between its
    ack and its reply — an answer after the ack. Progress after the ack
    ends nothing: it is what a live serving posts, and what m11741's last
    serving said before nothing was running."""
    facts = {"execution": "unknown", "ack": 0, "ack_at": 0, "ended_by": 0, "ended_at": 0, "work": 0, "work_at": 0,
             "ending": None, "waiting_on": [], "ending_intent": "", "ending_to": 0}
    if not messages or owner is None:
        return facts
    for message in messages:
        if message.get("sender_id") != owner:
            continue
        content = str(message.get("content") or "")
        if (is_speech(message) and not is_ack(content)) or parse_note(content, "change") is not None:
            facts["work"], facts["work_at"] = int(message.get("id") or 0), int(message.get("timestamp") or 0)
    owned = [m for m in messages if m.get("sender_id") == owner and is_speech(m)]
    acks = [m for m in owned if is_ack(str(m.get("content") or ""))]
    if not acks:
        return facts
    ack = acks[-1]
    facts["ack"] = int(ack.get("id") or 0)
    facts["ack_at"] = int(ack.get("timestamp") or 0)
    # Whoever spoke here before this serving began is who it serves.
    served = set(served) | {_sender(m) for m in messages
                            if m.get("sender_id") != owner and int(m.get("id") or 0) < facts["ack"]}
    served |= {start[2] for m in messages if m.get("sender_id") == owner
               and (start := parse_start(m.get("content"))) is not None and start[2]}
    facts["waiting_on"] = _waiting_on(messages, owner, facts["ack"], served)
    after = [m for m in owned if int(m.get("id") or 0) > facts["ack"] and not is_ack(str(m.get("content") or ""))]
    ending = next((m for m in after if (parse_post(m.get("content")).meta or _NO_META).end), None)
    if ending is None:
        answers = [m for m in after if not _is_progress(m.get("content"))]
        ending = answers[-1] if answers else None
    if ending is None:
        facts["execution"] = "open"
        return facts
    meta = parse_post(ending.get("content")).meta
    facts.update(execution="ended", ended_by=int(ending.get("id") or 0),
                 ended_at=int(ending.get("timestamp") or 0), ending=ending,
                 ending_intent=(meta.intent or "") if meta is not None else "",
                 ending_to=int(meta.to or 0) if meta is not None and meta.to is not None else 0)
    return facts


class _NoMeta:
    end = None
    intent = None


_NO_META = _NoMeta()


def _answer_taken_up(messages, owner, homes, home_messages, here, known_names=None) -> bool:
    """Whether the owner's newest answer names a requester whose home holds
    a served mark covering it — the move went back to whoever asked."""
    if owner is None or not homes or not messages:
        return False
    answers = [m for m in messages if m.get("sender_id") == owner and is_speech(m)
               and not is_ack(str(m.get("content") or "")) and not _is_progress(m.get("content"))]
    if not answers:
        return False
    answer = answers[-1]
    here_ids = frozenset(int(m.get("id") or 0) for m in messages)
    complete = len(messages) < HISTORY
    named = {match.group("name").strip() for match in MENTION.finditer(str(answer.get("content") or ""))}
    for requester_id in homes:
        names = {_sender(m) for m in messages if m.get("sender_id") == requester_id}
        names |= {name for name in [(known_names or {}).get(requester_id)] if name}
        if names & named and _taken_up((home_messages or {}).get(requester_id), requester_id,
                                       int(answer.get("id") or 0), here, here_ids, complete):
            return True
    return False


#: A child holds its parent's work while it is unfinished and has not
#: handed its answer back: nobody waits on a parent whose delegate is still
#: at it. A child waiting only on its requester (the parent's owner) holds
#: nothing — so a wait that goes round in a circle comes out as `none`.
def _holds(child: "Node") -> bool:
    if child.holder in ("done", "human"):
        return False
    if child.holder == "requester":
        return not child.taken_up and child.state not in ("awaiting_delivery", "failed")
    return True


def _holder(node: "Node", ending: dict | None) -> str:
    """Who holds the next move of `node` (module doc), its children decided."""
    if node.state in ("done", "cancelled"):
        return "done"
    if node.state == "queued" or node.execution == "open":
        return "owner"
    if node.waiting_on or any(_holds(child) for child in node.children):
        return "delegate"
    if node.owner and _owed_below(node, node.owner):
        # An answer below is addressed to this conversation's owner and its
        # listener has not served it yet: the owner holds the next move
        # (progress_panel p1 step 5: a routine run whose task result was
        # queued behind Front's other serving read "nobody holds it").
        return "owner"
    if node.state in ("awaiting_delivery", "failed") or node.note_state == HELD_WORD:
        return "requester"
    if node.state == "awaiting_human":
        return "human"
    if node.execution == "ended":
        meta = parse_post((ending or {}).get("content")).meta
        if meta is not None and meta.intent == PROGRESS:
            return "none"
        return "requester"
    if node.state in ("not_started", "executing"):
        return "owner"
    if node.state in ("awaiting_requester", "answered"):
        return "requester"
    return "unknown"


def _owed_below(node: "Node", name: str) -> bool:
    stack = list(node.children)
    while stack:
        child = stack.pop()
        if child.state == "awaiting_delivery" and name in child.owed_to:
            return True
        stack.extend(child.children)
    return False


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
                     known_names=None):
    """`(answer, requester name, home)` for the owner's newest answer that
    names a requester and has no receipt, or None.

    Every answer above the requester's served mark is looked at, newest
    first: a served mark covers everything up to its id, a reconciled
    receipt only the answer it names (failsafe p6), so an older answer
    nobody was given stays owed under a newer one that was reconciled."""
    if owner is None or not homes:
        return None
    answers = [m for m in messages if m.get("sender_id") == owner and is_speech(m)
               and not is_ack(str(m.get("content") or "")) and not _is_progress(m.get("content"))]
    if not answers:
        return None
    for requester_id, home in homes.items():
        names = {_sender(m) for m in messages if m.get("sender_id") == requester_id}
        names |= {name for name in [(known_names or {}).get(requester_id)] if name}
        if not names:
            continue
        received = (home_messages or {}).get(requester_id)
        mark = _served_marks(received, here, here_ids, complete)
        for answer in reversed(answers):
            answer_id = int(answer.get("id") or 0)
            if answer_id <= mark:
                break
            named = {match.group("name").strip() for match in MENTION.finditer(str(answer.get("content") or ""))}
            if not (names & named) or _reconciled(received, requester_id, answer_id, here_ids) is not None:
                continue
            return answer, ", ".join(sorted(names)), home
    return None


def _taken_up(home_messages, requester_id: int, answer_id: int, here, here_ids=frozenset(), complete=False) -> bool:
    """Whether the requester dealt with an answer: its home holds a served
    mark covering it — the receipt its listener writes after a *delivered*
    serving, bound to the post that serving processed — or a reconciled
    receipt (`[selfnote][receipt]`, failsafe p6) the requester wrote for
    exactly that answer.

    Nothing weaker counts (robust_workflow p2 step 3). p1 also accepted the
    requester speaking at home after the answer, and a reply to something
    else — a serving whose input ended before the answer arrived — then
    consumed an answer nothing had read (p2 step 1, R8). An answer that
    arrives during a serving stays owed until a serving marks it, which the
    listener does on its next pass. failsafe p6 retired the last of that
    leniency (`receipts_from`, which only Observer applied): a reader that
    wants an old answer settled asks the records that settle it
    (`_settlement`), the same for everybody."""
    if _served_marks(home_messages, here, here_ids, complete) >= answer_id:
        return True
    return _reconciled(home_messages, requester_id, answer_id, here_ids) is not None


def _reconciled(home_messages, requester_id: int, answer_id: int, here_ids=frozenset()) -> dict | None:
    """The requester's `[selfnote][receipt]` naming exactly this answer, which
    is in this conversation (`here_ids`): a receipt names a post, never a
    range."""
    if here_ids and int(answer_id) not in here_ids:
        return None
    for message in home_messages or ():
        if int(message.get("sender_id") or 0) != int(requester_id):
            continue
        parsed = parse_receipt(message.get("content"))
        if parsed is not None and parsed[1] == int(answer_id):
            return {"id": int(message.get("id") or 0), "evidence": parsed[2], "why": parsed[3]}
    return None


#: The decisions that settle an answer's review without its receipt
#: (failsafe p6, README_DEV "Completion, receipts and holds").
def decisions(messages: list[dict] | None, owner: int | None) -> tuple[list[dict], list[dict]]:
    """`(this unit's, handed down to the units below)` decision records in
    one conversation, oldest first: `{kind, id, covers, by, what}`. An
    answer is covered when its id is at most `covers`.

    - the requester's `[state] accepted`/`done` (not the owner's): this
      unit's answers before the note;
    - `[change] accepted … +shown=<id>`: the result shown for review, as the
      close-out bound it — a later close-out report is not covered;
    - `[acceptance] #<evidence> … after=#<shown>`: the mission's answers and
      its tasks' up to the shown result (up to the evidence post for a note
      written before `after=` existed);
    - `[state] cancelled`/`replaced`/`retired`: the units below (the unit's
      own is its `cancelled` state). A cancellation is written on a holder's
      request, and a cancelled mission's results are nobody's to take up.

    A producer's completion claim (`completed`, `delivered`, `finished`) is
    none of these: an answer it follows is still owed."""
    own: list[dict] = []
    down: list[dict] = []
    for message in messages or ():
        content = message.get("content")
        mid = int(message.get("id") or 0)
        by = int(message.get("sender_id") or 0)
        who = _sender(message)
        value = parse_note(content, STATE_TAG)
        if value is not None:
            word = value.split()[0].lower() if value.split() else ""
            if word in DECIDED_WORDS and owner is not None and by != owner:
                own.append({"kind": "accepted", "id": mid, "covers": mid, "by": by,
                            "what": f"{who}'s `[state] {word}` #{mid}"})
            elif word in CANCELLED_WORDS:
                down.append({"kind": "cancelled", "id": mid, "covers": mid, "by": by,
                             "what": f"`{word}` #{mid}"})
            continue
        value = parse_note(content, "change")
        if value is not None and value.split()[:1] == ["accepted"]:
            shown = _SHOWN.search(value)
            if shown:
                own.append({"kind": "accepted", "id": mid, "covers": int(shown.group(1)), "by": by,
                            "what": f"the acceptance recorded at #{mid} (shown #{shown.group(1)})"})
            continue
        value = parse_note(content, "acceptance")
        match = _ACCEPTANCE_RECORD.match(value.strip()) if value is not None else None
        if match is not None:
            bound = int(match.group("after") or 0) or int(match.group("evidence"))
            record = {"kind": "accepted", "id": mid, "covers": bound, "by": by,
                      "what": f"the acceptance #{match.group('evidence')} by "
                              f"{match.group('name') or match.group('by')}"
                              + (f" for {match.group('for_name') or match.group('for')}" if match.group("for") else "")
                              + f" recorded at #{mid}"}
            own.append(record)
            down.append(record)
    return own, down


def _receipt_facts(answer: dict, requester: str, home: tuple[str, str], settled: dict | None) -> dict:
    return {"answer": int(answer.get("id") or 0), "at": int(answer.get("timestamp") or 0), "to": requester,
            "home": f"{home[0]}/{home[1]}", "state": "settled" if settled else "missing",
            "settled_by": dict(settled) if settled else None}


def _settlement(answer_id: int, found: Iterable[dict]) -> dict | None:
    """The first decision covering this answer, or None."""
    covering = [d for d in found if int(d["covers"]) >= int(answer_id)]
    return min(covering, key=lambda d: d["id"]) if covering else None


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
    requester_names: dict[int, str] | None = None,
    inherited: Iterable[dict] = (),
    facts: dict | None = None,
) -> tuple[str, str, str, str, list[int], int]:
    """`(state, detail, identity, owner name, evidence ids, last activity)`
    for one conversation's messages (oldest first).

    `human` marks the conversation a human asked in: there the agent that
    acknowledges posts is the owner, and its answer being last means the
    human is the one to act. `home_messages` / `homes` (requester id → its
    home's messages / its home) let an answer addressed to a requester be
    checked against that requester's `[served]` marks.

    An answer without a receipt is owed (`awaiting_delivery`) unless a
    decision recorded after it covers it (`decisions`: this conversation's
    own, plus `inherited` from the units above) — then the unit reads as its
    record does, and `facts["receipt"]` says which answer lacks a receipt and
    what settled it (failsafe p6).
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
        owed = _unserved_answer(messages, owner, homes, home_messages, here, here_ids, complete, requester_names)
        if owed is not None:
            answer, requester, home = owed
            settled = _settlement(int(answer["id"]), [*decisions(messages, owner)[0], *inherited])
            receipt = _receipt_facts(answer, requester, home, settled)
            if facts is not None:
                facts["receipt"] = receipt
            if settled is not None:
                # The requester decided about this result after it existed —
                # accepted it, or had the work cancelled: nothing is owed
                # but the bookkeeping (failsafe p6: m8519's #8557, accepted
                # four days after its receipt was lost to the ✔ race).
                return (
                    "done",
                    f"{word}; #{answer['id']} to {requester} has no receipt in {home[0]}/{home[1]} — "
                    f"settled by {settled['what']}",
                    identity, owner_name, [word_id] if word_id else [], last_activity,
                )
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
        # The agent the post names is whose queue it waits in (failsafe p5:
        # a first request to a busy agent is the common queued post).
        named = MENTION.search(str(last_other.get("content") or ""))
        return ("queued", "no agent has acknowledged it", identity, named.group("name").strip() if named else "",
                [mid(last_other)], last_activity)

    if last_answer is not None and mid(last_answer) > mid(last_other) and _is_failure(last_answer.get("content")):
        # The notice's own line, not the mention that opens the post (p3
        # trial B's incident said only "@**Omni Agent**").
        lines = [line.strip() for line in str(last_answer.get("content") or "").splitlines() if line.strip()]
        text = next((line for line in lines if any(line.startswith(p) for p in FAILURE_PREFIXES)),
                    lines[0] if lines else "")[:160]
        return "failed", text, identity, owner_name, [mid(last_answer)], last_activity

    if last_other is None:
        if human:
            return _human_state(messages, complete, identity, owner_name, last_activity,
                                "only the agent has spoken", [])
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
        if acks:
            # Its owner opened it and serves it itself — Front's `routinerun-`
            # topics, started by its listener right after the serving that
            # opened them. Nobody else ever speaks there, so "nobody has posted
            # since it was opened" was true and meant nothing (progress_panel
            # p1, G1: every routine run read `not_started` while it worked).
            facts = _serving(messages, owner)
            if facts["execution"] == "open":
                newest = max([last_ack, *progress[-1:]], key=mid)
                return (
                    "executing",
                    f"served by its owner itself since ack #{mid(last_ack)}; last sign of work "
                    f"{_age(now, int(newest.get('timestamp') or 0))} ago",
                    identity, owner_name, [mid(last_ack)], int(newest.get("timestamp") or 0),
                )
            return (
                "awaiting_requester",
                f"served by its owner itself; its last serving ended at #{facts['ended_by']} "
                f"{_age(now, int(facts['ended_at'] or 0))} ago",
                identity, owner_name, [facts["ended_by"]], last_activity,
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
        return _human_state(
            messages, complete, identity, owner_name, last_activity,
            f"{owner_name} answered #{mid(last_answer)} {_age(now, int(last_answer.get('timestamp') or 0))} ago",
            [mid(last_answer)],
        )
    named = {match.group("name").strip() for match in MENTION.finditer(str(last_answer.get("content") or ""))}
    for requester_id, home in (homes or {}).items():
        names_here = {_sender(m) for m in others if m.get("sender_id") == requester_id}
        names_here |= {name for name in [(requester_names or {}).get(requester_id)] if name}
        if not names_here or not (names_here & named):
            continue
        owed = _unserved_answer(messages, owner, {requester_id: home}, home_messages, here, here_ids, complete,
                                requester_names)
        if owed is None:
            served = _served_marks((home_messages or {}).get(requester_id), here, here_ids, complete)
            return (
                "awaiting_requester",
                f"{owner_name} answered #{mid(last_answer)}; {', '.join(sorted(names_here))} has taken it up "
                + (f"(served up to {served})" if served >= mid(last_answer) else "(receipt reconciled)"),
                identity, owner_name, [mid(last_answer)], last_activity,
            )
        answer = owed[0]
        settled = _settlement(mid(answer), [*decisions(messages, owner)[0], *inherited])
        if facts is not None:
            facts["receipt"] = _receipt_facts(answer, ", ".join(sorted(names_here)), home, settled)
        if settled is not None:
            return (
                "awaiting_requester",
                f"{owner_name} answered #{mid(answer)}; no receipt by {', '.join(sorted(names_here))} in "
                f"{home[0]}/{home[1]} — settled by {settled['what']}",
                identity, owner_name, [mid(answer)], last_activity,
            )
        return (
            "awaiting_delivery",
            f"{owner_name} answered #{mid(answer)} naming {', '.join(sorted(names_here))}; "
            f"not marked served in {home[0]}/{home[1]} after {_age(now, int(answer.get('timestamp') or 0))}",
            identity, owner_name, [mid(answer)], last_activity,
        )
    return (
        "awaiting_requester",
        f"{owner_name} answered #{mid(last_answer)} {_age(now, int(last_answer.get('timestamp') or 0))} ago",
        identity, owner_name, [mid(last_answer)], last_activity,
    )


def _human_state(messages, complete, identity, owner_name, last_activity, answered_detail, evidence):
    """The state of a human's conversation whose agent spoke last, from the
    explicit requests in it (`agag.outstanding`) rather than from who spoke
    last: until `clearer_chat_ui` step 2 every report read as
    `awaiting_human`, which is true of every conversation ever answered."""
    from .outstanding import OVERTAKEN, read_requests

    found = read_requests(messages, complete=complete, is_ack=is_ack)
    waiting = found.pending
    if waiting:
        asks = "; ".join(f"#{r.id} {r.sender_name or r.sender_id} asks {r.to_name or r.to}"
                         + (f" ({r.ask})" if r.ask else "") for r in waiting)
        return "awaiting_human", asks, identity, owner_name, [r.id for r in waiting], last_activity
    overtaken = [r for r in found.requests if r.state == OVERTAKEN]
    if overtaken:
        request = overtaken[-1]
        return (
            "queued",
            f"#{request.id} was asked before reading #{', #'.join(map(str, request.overtaken_by))}; "
            f"that input is owed a serving",
            identity, owner_name, [request.id, *request.overtaken_by], last_activity,
        )
    return "answered", f"{answered_detail}; nothing is asked of anybody", identity, owner_name, evidence, last_activity


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
    relation: Relation | None = None

    @property
    def adopts(self) -> bool:
        return self.relation is not None and self.relation.adopts


def _links(notes: list[dict], book: Relations | None = None) -> list[_Link]:
    """The effective root notes, per author and conversation: the earliest
    ordinary note anchors, the newest deliberate move replaces it
    (`effective_rootchat`'s rule, per author) — each with its relation
    (`agag.relations`)."""
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
    book = book if book is not None else Relations()
    links = [
        _Link(int(message.get("id") or 0), author, _sender(message), channel, str(message.get("subject") or ""),
              home, message, book.of(message))
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


def _records(messages: list[dict] | None, owner: int | None) -> list[dict]:
    found = []
    for message in messages or ():
        content = message.get("content")
        base = {"id": int(message.get("id") or 0), "at": int(message.get("timestamp") or 0),
                "by": int(message.get("sender_id") or 0)}
        for tag in RECORD_TAGS:
            value = parse_note(content, tag)
            if value is not None:
                found.append({"tag": tag, "value": value, **base})
                break
        else:
            if owner is not None and message.get("sender_id") == owner and is_speech(message):
                finish = finish_record(content)
                if finish is not None:
                    found.append({"tag": "finish", "value": "achieved" if finish["achieved"] else "not achieved",
                                  **base})
    return found


def _open_claims(messages: list[dict] | None) -> list[dict]:
    from .claims import open_claims

    keep = ("id", "reply", "attempt", "state", "at", "by", "missing")
    return [{key: row.get(key) for key in keep} for row in open_claims(messages or ())]


def claim_line(claim: dict) -> str:
    """One open claim, as the trace and the panel say it."""
    said = ", ".join(f"{m.get('act')}" + (f" #{m.get('target')}" if m.get("target") else "")
                     for m in claim.get("missing") or [])
    how = ("its owner is served once with the mismatch" if claim.get("state") == "repairing"
           else "said again after the notice; the owners are told")
    return f"claim #{claim.get('id')}: reply #{claim.get('reply')} says {said} — no record ({how})"


def _owed_names(messages, owner, homes, home_messages, here, names) -> list[str]:
    if not messages:
        return []
    owed = _unserved_answer(messages, owner, homes, home_messages, here, frozenset(int(m.get("id") or 0) for m in messages),
                            len(messages) < HISTORY, names)
    return owed[1].split(", ") if owed else []


def _with_receipts(home: list[dict] | None, receipts: list[dict] | None) -> list[dict] | None:
    """The requester's home, plus its served marks written elsewhere in the
    tree (deduplicated, oldest first)."""
    if not receipts:
        return home
    seen = {int(m.get("id") or 0) for m in home or ()}
    extra = [m for m in receipts if int(m.get("id") or 0) not in seen]
    return sorted([*(home or []), *extra], key=lambda m: int(m.get("id") or 0))


def _identity_id(messages: list[dict]) -> int:
    for message in messages:
        for tag in IDENTITY_TAGS:
            if parse_note(message.get("content"), tag) is not None:
                return int(message.get("id") or 0)
    return 0


def trace(client, message_id: int, *, now: int | None = None, max_depth: int = MAX_DEPTH) -> Trace:
    """The progress tree below the conversation holding `message_id`.

    Whatever message it starts from, a unit's requesters are every author of
    a root note in it — a task traced from its mission still owes its answer
    to the requester whose home is outside the tree (failsafe p6: `trace
    8519` said DONE while `trace 8512` said AWAITING_DELIVERY)."""
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
    records = reader.notes(RELATION_TAG)
    index_problem = ""
    if notes is None:
        notes, index_problem = [], "the root-note search got no answer; delegations are not listed"
    if records is None:
        # Without the records a note that states no relation is unknown —
        # never adopted on a guess (failsafe p6 ex1).
        index_problem = "; ".join(p for p in (index_problem, "the relation-record search got no answer; "
                                                             "notes without a stated relation read unknown") if p)
    links = _links(list(notes) + list(moved or []), Relations(records or []))

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

    below: dict[tuple[str, str], list[_Link]] = {}
    for link in links:
        home = home_of[link.note_id]
        if home is not None and _key(link.child_channel, link.child_topic) != home:
            below.setdefault(home, []).append(link)

    # The tree, from the root down. Only a work relation adopts (failsafe p6
    # ex1): a citation — Front cleaning up o11711 posted into m8519's
    # request, #15357 — and a relation nothing records are listed on the
    # home they name, and hang nothing under it.
    children_of: dict[tuple[str, str], list[_Link]] = {}
    cited: dict[tuple[str, str], list[_Link]] = {}
    depth_of: dict[tuple[str, str], int] = {root_key: 0}
    frontier = [root_key]
    while frontier:
        following = []
        for home in frontier:
            for link in below.get(home, []):
                key = _key(link.child_channel, link.child_topic)
                if not link.adopts:
                    cited.setdefault(home, []).append(link)
                    continue
                children_of.setdefault(home, []).append(link)
                # Where each anchored conversation is shown: under the **most
                # specific** conversation anchoring it that the tree reaches —
                # a task Front started and autolab's planner opened sits under
                # the mission, not beside it (`placement`, below).
                if key not in depth_of:
                    depth_of[key] = depth_of[home] + 1
                    following.append(key)
        frontier = following
    #: Every root note in a conversation of the tree, whatever home it names:
    #: who asked for the work there, wherever the trace started (failsafe p6:
    #: `trace 8519` said DONE and `trace 8512` AWAITING_DELIVERY, because
    #: Front's note in m8519's task names a home outside the mission's tree).
    notes_of: dict[tuple[str, str], list[_Link]] = {}
    for link in links:
        key = _key(link.child_channel, link.child_topic)
        home = home_of[link.note_id]
        if key in depth_of and home is not None and key != home and link.adopts:
            notes_of.setdefault(key, []).append(link)
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
              requested: list[tuple[int, str, tuple[str, str]]], inherited: list[dict]) -> Node:
        messages = read(key)
        if not messages and anchors_of.get(key):
            # Its own root notes are in it — that is how it was found — so a
            # read that returns nothing is a read that failed (a mirror that
            # does not hold it, a lost coverage), never an empty conversation
            # and never "not started" (robust_workflow p2 step 1, R1).
            messages = None
        homes = {requester: home for requester, _, home in requested}
        home_messages = {requester: _with_receipts(read(home), receipts.get(requester))
                         for requester, _, home in requested}
        found: dict = {}
        state, detail, identity, owner, evidence, last = classify(
            messages, human=human, now=now, home_messages=home_messages, homes=homes, here=key,
            requester_names={requester: name for requester, name, _ in requested},
            inherited=inherited, facts=found,
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
        owner_id = owner_id or _owner_by_ack(messages or [])
        if state == "queued" and not owner and hasattr(client, "roster_owner"):
            # Nobody has acknowledged anything here and the post names nobody:
            # whose listener serves this topic is what the agents publish
            # (their `#agents` roster; failsafe p5 trial G, a first request
            # without a mention).
            owner = client.roster_owner(key[0], key[1]) or ""
        facts = _serving(messages, owner_id, {name for _, name, _ in requested})
        ending = facts.pop("ending")
        node = Node(
            channel=key[0], topic=live, state=state, detail=detail, identity=identity, anchor=anchor,
            owner=owner, note_state=word, evidence=evidence, last_activity=last,
            failures=[
                f"#{m.get('id')} {_sender(m)}: {value}"
                for m in messages or ()
                if (value := parse_note(m.get("content"), OPFAIL_TAG)) is not None
            ],
            records=_records(messages, owner_id),
            owed_to=_owed_names(messages, owner_id, homes, home_messages, key,
                                {requester: name for requester, name, _ in requested}) if state == "awaiting_delivery" else [],
            taken_up=state == "awaiting_requester" and (
                (found.get("receipt") or {}).get("state") == "settled" or _answer_taken_up(
                    messages, owner_id, homes, home_messages, key,
                    {requester: name for requester, name, _ in requested})),
            receipt=found.get("receipt") or {},
            claims=_open_claims(messages),
            relations=[_relation_row(link) for link in cited.get(key, [])],
            last_substantive=max((int(m.get("id") or 0) for m in messages or ()
                                  if is_speech(m) and not is_ack(str(m.get("content") or ""))), default=0),
            activity=[[int(m.get("id") or 0), int(m.get("sender_id") or 0),
                       int((parse_post(m.get("content")).meta or _NO_META).end or 0)]
                      for m in messages or () if is_speech(m) and not is_ack(str(m.get("content") or ""))],
            **facts,
        )
        endings[int(node.anchor)] = ending
        if depth >= max_depth:
            node.holder = _holder(node, ending)
            return node
        seen = seen | {key}
        order: list[tuple[str, str]] = []
        for link in children_of.get(key, []):
            child = _key(link.child_channel, link.child_topic)
            if child in seen or placement.get(child) != key or child in order:
                continue
            order.append(child)
        # Decisions recorded here that settle answers below it: a mission's
        # acceptance, a cancellation (`decisions`).
        handed = [*inherited, *decisions(messages, owner_id)[1]]
        for child in order:
            notes_for = notes_of.get(child, [])
            requesters = []
            for link in notes_for:
                home = home_of[link.note_id]
                if home is not None:
                    requesters.append((link.author, link.author_name, home))
            # …and whoever asked *here*, one hop down: an owner may open and
            # start work for this conversation without its requester ever
            # posting in it (autolab's tasks since robust_workflow p1), and
            # the answer then reaches that requester through this
            # conversation's root note (`agag.zulip.parent_rootchat`). The
            # trace follows the same hop, or an answer lost on that route is
            # invisible to it (p2 step 5, trial B).
            known = {requester for requester, _, _ in requesters}
            requesters += [row for row in requested if row[0] not in known]
            built = build(child, depth + 1, seen, False, requesters, handed)
            built.requested_by = [f"{link.author_name} #{link.note_id}" for link in notes_for]
            built.requesters = list(dict.fromkeys(name for _, name, _ in requesters))
            node.children.append(built)
        _order_tasks(node)
        node.holder = _holder(node, ending)
        return node

    # A receipt is the requester's `[served]` mark, and it is written in the
    # home of the serving that took the answer up — which need not be the
    # conversation the root note names: Front served a routine run's task
    # result from the request's own conversation (progress_panel p1 step 5),
    # and the mark there was never read, so the answer stayed "undelivered"
    # and Observer asked, again and again. Marks are matched by the post they
    # name, so a requester's mark anywhere in this request's tree is its
    # receipt.
    endings: dict[int, dict | None] = {}
    receipts: dict[int, list[dict]] = {}
    for messages in histories.values():
        for message in messages or ():
            content = message.get("content")
            if parse_served(content) is not None or parse_receipt(content) is not None:
                receipts.setdefault(int(message.get("sender_id") or 0), []).append(message)

    result.root = build(root_key, 0, set(), True, [], [])
    result.calls = reader.calls
    result.problem = index_problem
    # A decision recorded on the request — monitoring suppressed, or the
    # request ended (`agag.dispositions`) — read the same by every reader;
    # then who holds what is decided again for what is still open.
    from .dispositions import apply as apply_dispositions

    result.dispositions = apply_dispositions(result)
    if result.dispositions:
        _rehold(result.root, endings)
    result.unknown_relations = [row for node in result.nodes() for row in node.relations
                                if row["relation"] == UNKNOWN_RELATION]
    return result


UNKNOWN_RELATION = "unknown"


def _relation_row(link: _Link) -> dict:
    relation = link.relation or Relation(link.note_id, link.author, UNKNOWN_RELATION)
    row = {"note": link.note_id, "channel": link.child_channel, "topic": link.child_topic,
           "author": link.author_name, "relation": relation.kind, "decided_by": relation.describe()}
    if relation.kind == UNKNOWN_RELATION:
        row["resolve"] = (f"{link.author_name}: `agentchat relation {link.child_channel} {_bare(link.child_topic)} "
                          f"work|reference --because <post>`")
    return row


def _rehold(node: Node, endings: dict[int, dict | None]) -> None:
    for child in node.children:
        _rehold(child, endings)
    if node.holder != "done":
        node.holder = _holder(node, endings.get(int(node.anchor)))


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
        for claim in node.claims:
            lines.append(f"{node.channel}/{node.topic}: {claim_line(claim)}")
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
        if node.holder not in ("done",):
            serving = {"open": f"serving open since ack #{node.ack}",
                       "ended": f"last serving ended at #{node.ended_by}",
                       "unknown": "no serving on record"}[node.execution]
            held = {"none": "NOBODY holds the next move", "delegate": "held by a conversation opened from it",
                    "owner": "held by its owner", "requester": "held by its requester",
                    "human": "held by a person", "unknown": "holder unknown"}[node.holder]
            detail = f"{detail}; {serving}; {held}" if detail else f"{serving}; {held}"
        if detail:
            lines.append(f"{pad}{'  ' if depth else ''}  {detail}")
        if node.requested_by:
            lines.append(f"{pad}{'  ' if depth else ''}  anchored by {', '.join(node.requested_by)}")
        for failure in node.failures:
            lines.append(f"{pad}{'  ' if depth else ''}  ! operation failed: {failure}")
        for claim in node.claims:
            lines.append(f"{pad}{'  ' if depth else ''}  ! {claim_line(claim)}")
        if node.disposition and node.disposition.get("kind") == "suppressed":
            lines.append(f"{pad}{'  ' if depth else ''}  " + (
                f"monitoring suppressed: {node.disposition['says']}" if node.disposition.get("covered") else
                f"new activity since #{node.disposition['id']} ({node.disposition['says']}): not covered"))
        elif node.disposition and not node.disposition.get("covered"):
            lines.append(f"{pad}{'  ' if depth else ''}  new activity since #{node.disposition['id']} "
                         f"({node.disposition['says']}): not covered")
        for row in node.relations:
            if row["relation"] == UNKNOWN_RELATION:
                lines.append(f"{pad}{'  ' if depth else ''}  ? relation unknown: {row['channel']}/{row['topic']} "
                             f"(note #{row['note']} by {row['author']}) adopts nothing until recorded — "
                             f"{row['resolve']}")
            else:
                lines.append(f"{pad}{'  ' if depth else ''}  cites {row['channel']}/{row['topic']} "
                             f"(note #{row['note']} by {row['author']}, {row['decided_by']}): its work stays its own")
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

    def roster_owner(self, channel: str, topic: str) -> str:
        """The agent whose published roster serves this topic (its own
        channel, or a prefix it sweeps), read off the mirror's `#agents`
        board; "" when none or more than one does."""
        from .intro import AGENTS_CHANNEL, INTRO_TOPIC_PREFIX, parse_roster, roster_owner

        rosters = getattr(self, "_rosters", None)
        if rosters is None:
            rosters = []
            try:
                for index in self.mirror.topics(AGENTS_CHANNEL):
                    if not index.name.startswith(INTRO_TOPIC_PREFIX) or index.resolved:
                        continue
                    history = self.mirror.history(AGENTS_CHANNEL, index.live_name, num_before=1,
                                                  across_resolve=False)
                    roster = parse_roster(str(history[-1].get("content") or "")) if history else None
                    if roster is not None:
                        rosters.append(roster)
            except Exception:  # noqa: BLE001 - an unreadable board resolves nobody
                rosters = []
            self._rosters = rosters
        return roster_owner(rosters, channel, topic)


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
    # failsafe p1: the last serving ended saying work goes on, and nobody
    # holds it. A listener re-serves input that arrived during a run within
    # seconds; failsafe p2 cut p1's five-minute grace to one look interval:
    # a confirmed end with unfinished work enters recovery on the next cycle.
    "unheld": 60,
    # failsafe p3: the request's own conversation ends in its agent's failure
    # notice after the listener's own second serving of the input.
    "unanswered": 60,
    # failsafe p7: a reply said an act was done that no record shows, and
    # said it again after its owner was told (`agag.claims`); an attempt-1
    # claim nothing answered within `claims.REPAIR_SECONDS` is the same.
    "claim": 60,
    # Unfinished work whose holder cannot be established (or is an agent
    # that took it up), with nothing new in the request for this long.
    "quiet": 1800,
}
#: Kinds that exist since failsafe p1: a consumer that tracks requests from
#: before its deployment may hold them to the older rules.
FAILSAFE_KINDS = ("unheld", "quiet", "unanswered")


def stall_candidates(result: Trace, now: int | None = None, thresholds: dict | None = None) -> list[Candidate]:
    """Everything in the tree that is owed and overdue."""
    now = int(now if now is not None else time.time())
    limits = {**THRESHOLDS, **(thresholds or {})}
    found: list[Candidate] = []

    def overdue(kind: str, since: int) -> bool:
        return bool(since) and now - int(since) >= limits[kind]

    root = result.root
    #: The newest explicit question to a person anywhere in the request: once
    #: somebody asked a person about the work, the next move is theirs.
    asked_person = max((max(n.evidence, default=0) for n in result.nodes() if n.state == "awaiting_human"),
                       default=0)
    for node in result.nodes():
        is_root = node is root
        resolved = node.topic.startswith(RESOLVED_TOPIC_PREFIX)
        for claim in node.claims:
            from .claims import REPAIR_SECONDS

            due = overdue("claim", int(claim.get("at") or 0)) if claim.get("state") == "escalated" else \
                bool(claim.get("at")) and now - int(claim["at"]) >= max(REPAIR_SECONDS, limits["claim"])
            if due:
                found.append(Candidate(
                    "claim", node.channel, node.topic, node.identity, claim_line(claim),
                    node.owner or "the agent that owns this conversation",
                    "a person reads the reply against the records and has the act recorded or the reply "
                    f"corrected (`python -m agag.claims --settle {claim.get('id')} <why>` closes it as dismissed)",
                    int(claim.get("at") or 0), (int(claim.get("id") or 0), int(claim.get("reply") or 0)),
                    anchor=node.anchor,
                ))
        if node.state == "queued" and overdue("unacknowledged", node.last_activity):
            found.append(Candidate(
                "unacknowledged", node.channel, node.topic, node.identity, node.detail,
                node.owner or "the agent that owns this conversation",
                f"{node.owner or 'its owner'}'s listener picks up #{node.evidence[0] if node.evidence else '?'} and serves it",
                node.last_activity, tuple(node.evidence), anchor=node.anchor,
            ))
        if is_root:
            if node.state == "failed" and overdue("unanswered", node.last_activity):
                # The request's own conversation ends in a failure notice: the
                # requester's input is unanswered, and the agent that failed is
                # the one that would be asked (failsafe p3, p2's #12509). Its
                # listener has already served the input twice.
                found.append(Candidate(
                    "unanswered", node.channel, node.topic, node.identity, node.detail,
                    node.owner or "the agent that owns this conversation",
                    "a person looks at why the agent could not answer, and answers or re-asks",
                    node.last_activity, tuple(node.evidence), anchor=node.anchor,
                ))
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
        closed_exchange = (not node.identity and node.state == "awaiting_requester" and node.taken_up
                           and node.execution != "open")
        if resolved and node.state in ("queued", "executing", "awaiting_delivery", "awaiting_requester") \
                and not closed_exchange \
                and node.note_state not in DONE_WORDS and overdue("resolved_live", node.last_activity):
            # A ✔ on work its record calls finished is no question: a close-out
            # answer still owed to its requester is `undelivered`'s, above
            # (progress_panel p1 step 5: every autolab task resolves itself
            # right after its close-out post, and a busy requester's listener
            # turned each one into a judged incident).
            found.append(Candidate(
                "resolved_live", node.channel, node.topic, node.identity,
                f"✔ while {node.state.replace('_', ' ')}: {node.detail}",
                "whoever resolved it",
                f"`agentchat unresolve {node.channel} {_bare(node.topic)}` if the ✔ was a mistake; nothing if it was a deliberate close",
                node.last_activity, tuple(node.evidence) or (0,), judgment=True, anchor=node.anchor,
            ))
        if node.state == "executing" and overdue("silent", node.last_activity) and not _open_unit_below(node):
            # A conversation whose own serving is open while a unit of work
            # opened from it is unfinished is waiting on that unit: the
            # deeper one is the one to ask about, once (progress_panel p1:
            # Front's routine runs read `executing` since they are served
            # as themselves, and a healthy long task below one must not make
            # the run a stall).
            found.append(Candidate(
                "silent", node.channel, node.topic, node.identity, node.detail,
                node.owner or "the owner",
                f"{node.owner or 'the owner'} answers, or says the work is still running",
                node.last_activity, tuple(node.evidence), judgment=True, anchor=node.anchor,
            ))
        if node.holder == "none" and overdue("unheld", node.ended_at) and asked_person < node.ended_by:
            found.append(Candidate(
                "unheld", node.channel, node.topic, node.identity,
                f"the last serving ended at #{node.ended_by} saying the work goes on, and nothing holds it: no "
                f"serving is open, nothing opened from it is unfinished, nobody was asked anything",
                _asker(node),
                (f"its owner, {node.owner}, decides: have it served again with a start of its own (its own post "
                 f"there serves nothing; Front: `agrun continue {node.channel} {_bare(node.topic)} --because <post>`), "
                 f"end it on the record, or ask the Developer")
                if node.owner and _asker(node) == node.owner else
                f"whoever asked for it decides: resume it (a post in {node.channel}/{_bare(node.topic)} starts a "
                f"new serving of the same work), report what blocks it, or ask the Developer",
                node.ended_at, (node.ended_by,), anchor=node.anchor,
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
    # A request somebody is explicitly asked about is waiting on them; a
    # silence there is theirs to break, not a stall.
    human_wait = any(node.state == "awaiting_human" for node in result.nodes())
    below = [node for node in result.nodes() if node is not root]
    newest = max((node.last_activity for node in below), default=0)
    if not human_wait and overdue("quiet", newest):
        for node in below:
            if any(child.identity and child.state not in ("done", "cancelled") for child in node.children):
                continue  # the unfinished unit below it is the one to ask about, once
            if node.identity and node.holder in ("unknown", "requester") \
                    and not node.topic.startswith(RESOLVED_TOPIC_PREFIX):
                found.append(Candidate(
                    "quiet", node.channel, node.topic, node.identity,
                    f"{node.identity} is unfinished ({node.state.replace('_', ' ')}; {node.detail}) and nothing "
                    f"has moved anywhere in the request for a while; whether anybody is still at it cannot be "
                    f"read from the records",
                    _asker(node),
                    f"whoever asked for it checks it and resumes it, reports what blocks it, or says why it waits",
                    newest, tuple(node.evidence) or (node.anchor,), judgment=True, anchor=node.anchor,
                ))
    return found


def _asker(node: Node) -> str:
    return node.requested_by[0].split(" #")[0] if node.requested_by else "whoever asked for it"


def _open_unit_below(node: Node) -> bool:
    """Whether any conversation below `node` is a unit of work (an identity
    note) that its record does not call finished."""
    stack = list(node.children)
    while stack:
        child = stack.pop()
        if child.identity and child.state not in ("done", "cancelled"):
            return True
        stack.extend(child.children)
    return False


# --- re-checking one stopped unit of work (failsafe p4) ----------------------


@dataclass
class Recheck:
    """Whether the work a stop was reported on has resumed, read from that
    one conversation now.

    Only its owner's posts there after the stopped serving count as the
    work moving: an acknowledgement elsewhere, the requester's own reply in
    their conversation, or a promise, is not (failsafe p3 trial A read
    Front's own ack as the task resuming). `verdict` is one of:

    - `finished`: its record says it is closed (`completed`, `done`,
      `accepted`, `cancelled`) — nothing to resume;
    - `resumed`: a serving acknowledged after the stopped one has worked
      (posted progress, a result or a change note) or ended with a reply;
    - `resuming`: a serving acknowledged after the stopped one has not
      shown work yet — it is starting; look again, do not ask again;
    - `asked`: somebody posted there after the stop and the owner has not
      acknowledged it yet — the resume is already asked for;
    - `stopped`: nothing has happened there since the stopped serving —
      the work has not resumed;
    - `unreadable`: the conversation could not be read; nothing follows.
    """

    verdict: str
    channel: str = ""
    topic: str = ""
    owner: str = ""
    after: int = 0
    ack: int = 0
    ack_at: int = 0
    work: int = 0
    work_at: int = 0
    execution: str = "unknown"
    state: str = ""
    pending: list[int] = field(default_factory=list)
    observed_at: int = 0
    detail: str = ""

    def as_dict(self) -> dict:
        return {"schema": "agag.recheck.v1", **asdict(self)}


def recheck(client, message_id: int, after: int, *, now: int | None = None) -> Recheck:
    """Re-read the conversation that holds `message_id` (the stalled work's
    anchor, or any post in it) and say whether its owner resumed the work
    after the serving acknowledged at `after` (the one reported stopped)."""
    now = int(now if now is not None else time.time())
    reader = _Reader(client)
    found = reader.message(int(message_id))
    if not found:
        return Recheck("unreadable", after=int(after), observed_at=now,
                       detail=f"message {message_id} " + ("is gone" if found is None else "could not be read"))
    channel = channel_name(found)
    topic = str(found.get("subject") or "")
    messages = reader.history(channel, topic)
    if messages is None:
        return Recheck("unreadable", channel, topic, after=int(after), observed_at=now,
                       detail="its history could not be read")
    owner = _owner_by_ack(messages)
    owner_name = next((_sender(m) for m in messages if m.get("sender_id") == owner), "") if owner else ""
    facts = _serving(messages, owner)
    state, _ = _note_state(messages, owner)
    result = Recheck("stopped", channel, topic, owner_name, int(after), facts["ack"], facts["ack_at"],
                     facts["work"], facts["work_at"], facts["execution"], state, observed_at=now)
    if owner is None and not state:
        # No agent has ever served this conversation (failsafe p5 trial E: a
        # post went to a topic nobody listens to, and "asked" told the
        # requester to wait for an owner that does not exist).
        result.verdict = "unowned"
        result.detail = ("no agent has acknowledged anything in this conversation: whatever was posted here "
                         "has nobody to serve it")
        return result
    if state in ("completed", "done", "accepted", "cancelled", "finished", "ended"):
        result.verdict, result.detail = "finished", f"its record says `{state}`"
        return result
    since = int(after)
    newer_ack = facts["ack"] > since
    # A post waiting for its owner counts from `after` itself: Observer names
    # an unacknowledged post as the `after` of its re-check, and that post is
    # exactly the one waiting (failsafe p5, #13702: a queued post read
    # STOPPED, and Front "resumed" into the same queue).
    result.pending = [int(m.get("id") or 0) for m in messages
                      if m.get("sender_id") != owner and is_speech(m) and int(m.get("id") or 0) >= since
                      and int(m.get("id") or 0) > facts["ack"]]
    if newer_ack and (facts["work"] > facts["ack"] or facts["execution"] == "ended"):
        result.verdict = "resumed"
        result.detail = (f"a serving acknowledged at #{facts['ack']} after the stopped #{since} has "
                         + (f"worked (#{facts['work']})" if facts["work"] > facts["ack"] else f"ended (#{facts['ended_by']})"))
    elif newer_ack:
        result.verdict = "resuming"
        result.detail = f"a serving was acknowledged at #{facts['ack']} after the stopped #{since}; no work from it yet"
    elif result.pending:
        result.verdict = "asked"
        result.detail = (f"#{result.pending[0]} was posted there after the stop and {owner_name or 'its owner'} "
                         "has not acknowledged it yet")
    else:
        result.detail = f"nothing from {owner_name or 'its owner'} there since the serving acknowledged at #{since}"
    return result


#: What each verdict asks of whoever re-checked.
RECHECK_NEXT = {
    "finished": "nothing to resume",
    "resumed": "it is moving again: do not post there, and say so",
    "resuming": "a new serving is starting: do not post there; look again in a minute",
    "asked": "a resume is already waiting for its owner: do not ask again",
    "stopped": "not resumed: post in that conversation to resume it (once)",
    "unreadable": "nothing can be concluded; ask the developer if it stays so",
    "unowned": "check that this is the conversation the work is in (its owner's topic); post there instead, "
               "or ask the developer",
}


def recheck_lines(result: Recheck) -> list[str]:
    where = f"{result.channel}/{result.topic}" if result.channel else "?"
    lines = [f"recheck of {where} after #{result.after}, observed "
             f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(result.observed_at))}: {result.verdict.upper()}",
             f"  {result.detail}"]
    if result.channel:
        lines.append(f"  owner {result.owner or '?'}; newest ack #{result.ack or '-'}; newest work #{result.work or '-'}; "
                     f"execution {result.execution}; record {result.state or '-'}"
                     + (f"; posts not yet acknowledged: {', '.join(f'#{i}' for i in result.pending)}"
                        if result.pending else ""))
    lines.append(f"  next: {RECHECK_NEXT[result.verdict]}")
    return lines
