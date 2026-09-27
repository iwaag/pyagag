"""Accepting a mission: the requester's decision, recorded where the work is.

`robust_workflow` p3 step 3. A mission's tasks are closed one by one, each by
the run that did it once the requester agreed; the mission itself had no
close on the normal path. autolab's last close-out said "the mission waits
for your acceptance", and then one of two things happened: nobody posted in
the plan's conversation (every p2 trial), or the requester relayed the
acceptance there and bought a planning run whose only output was "noted"
(p1 #8921, p3 #8462) — which named the requester and bought it a run too.
Neither wrote anything, so every mission read `started` for ever and every
request that ever reached one stayed tracked by Observer.

`accept_mission` is the one operation, for whoever holds the decision:

- **the requester's side**, with `agentchat accept <mission> --evidence
  <the post where it was accepted>` — Front on the developer's words;
- **autolab**, when the requester says it in the plan's conversation itself
  (the `accept.flag` a planning run writes), with that post as evidence;
- **the operation room's completion door**, as the human pressing it.

What it writes, all selfnotes (nobody is served by a record): `[state]
accepted` in each finished task that does not say so yet, then
`[selfnote][acceptance] #<evidence> by <user id> (<name>)` and `[state]
done` in the mission's conversation, which is then resolved. The trace reads
`done` from anybody (a requester's word, `agag.trace._note_state`), so the
mission is finished for every reader at once, and Observer releases the
request on its next look.

**Whose decision it is** (failsafe p5). The acceptance rests on a post by
somebody who **holds** the decision, whoever records it:

- the mission's **requester** — the one who asked for it in the plan's
  conversation (its root note there, else the first person to speak) — and
  everybody that requester was asking *for*, up its root notes to the
  request's origin (Front asking for a routine run asking for the
  Developer's request: Front and the Developer). A requester entrusted with
  the work may accept it, and its own agreement is evidence like anybody's;
- unless the approval was **reserved**: `[selfnote][approval] reserved
  <user id> (<name>) #<post>` in any of those conversations (written by
  `agentchat reserve` on that person's own words) leaves that person as the
  only holder. The mission then waits for their words.

The recorder — the holder itself, autolab's close-out, the completion
door, anybody — changes nothing: the same post gives the same record. The
evidence must be a holder's post (never the mission's own agent: a
worker's completion claim is not acceptance), in the mission's own
conversations or the requester's chain when an agent acting for somebody
said it (a person's own words count wherever they said them), and
**later than the last result
shown** for review (each task's `[change] accepted … +shown=<id>`, else its
newest report): the initial request, or an agreement given before the
result existed, accepts nothing. The note says whose decision it was, on
which post, and which shown result it followed (`after=#<shown>`).

What it refuses, before writing anything: a conversation that is not a
mission; a mission cancelled, replaced or retired; a task not finished yet
(accepting the last task and the mission may coincide in the requester's
words, but the record waits until the task is closed); an acceptance
without the post it rests on, unless a holder is recording their own
decision in person; and evidence that fails the rules above.

**Repeating it is safe.** Every write is skipped when it is already there, and
an acceptance note already on record is the one that counts: a retry after
an interruption finishes the record the first attempt began, with the first
attempt's evidence, and a repeat after `done` changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .selfnote import Conversation, effective_rootchat, is_speech, note, parse_note, parse_rootchat
from .trace import CANCELLED_WORDS, trace
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, ZulipError, locate, log as default_log

ACCEPTANCE_TAG = "acceptance"
STATE_TAG = "state"
MISSION_DONE = "done"
TASK_ACCEPTED = "accepted"
#: A task the requester may be said to have accepted: its run closed it.
FINISHED_TASK = ("completed", "accepted")
HISTORY = 1000
_ACCEPTANCE = re.compile(r"^#(?P<evidence>\d+) by (?P<by>\d+)(?: \((?P<name>[^)]*)\))?")
APPROVAL_TAG = "approval"
_RESERVED = re.compile(r"^reserved (?P<user>\d+)(?: \((?P<name>[^)]*)\))?(?: #(?P<post>\d+))?")
_SHOWN = re.compile(r"\+shown=(\d+)")
#: How far up the requester's root notes the chain is followed.
CHAIN_DEPTH = 4
_MISSION = re.compile(r"^mission m(?P<id>\d+)\b")

__all__ = [
    "ACCEPTANCE_TAG",
    "APPROVAL_TAG",
    "Decision",
    "decision",
    "parse_reservation",
    "reservation_note",
    "Acceptance",
    "AcceptanceRefused",
    "accept_mission",
    "acceptance_note",
    "parse_acceptance",
]


class AcceptanceRefused(RuntimeError):
    """The mission cannot be accepted now, and nothing was written."""


def acceptance_note(evidence: int, by_id: int, by_name: str = "", after: int = 0) -> str:
    """`[selfnote][acceptance] #<evidence> by <user id> (<name>) [after=#<shown>]`.
    Evidence 0 means a holder recorded their own decision with no post to
    point at (the completion door); `after` is the shown result the decision
    followed."""
    name = f" ({by_name})" if by_name else ""
    shown = f" after=#{int(after)}" if after else ""
    return note(ACCEPTANCE_TAG, f"#{int(evidence)} by {int(by_id)}{name}{shown}")


def reservation_note(user_id: int, name: str = "", post: int = 0) -> str:
    """`[selfnote][approval] reserved <user id> (<name>) #<post>`: the final
    approval of the work opened from here is that person's own (failsafe p5).
    `post` is where they said so."""
    who = f" ({name})" if name else ""
    return note(APPROVAL_TAG, f"reserved {int(user_id)}{who}" + (f" #{int(post)}" if post else ""))


def parse_reservation(content) -> tuple[int, str, int] | None:
    """`(user id, name, post)` of a reservation note, or None."""
    value = parse_note(content, APPROVAL_TAG)
    match = _RESERVED.match(value.strip()) if value else None
    if match is None:
        return None
    return int(match.group("user")), match.group("name") or "", int(match.group("post") or 0)


@dataclass
class Decision:
    """Who holds a mission's acceptance, and where it may be given."""

    requester: int = 0
    #: `(user id, name)`, the requester first and the origin's asker last.
    holders: list[tuple[int, str]] = field(default_factory=list)
    #: `(user id, name, post)` when a person reserved the approval.
    reserved: tuple[int, str, int] | None = None
    #: `(channel, bare topic)` of every conversation the decision may be
    #: given in: the mission's, its tasks', and the requester chain's.
    conversations: set[tuple[str, str]] = field(default_factory=set)
    #: Holders that act for somebody else (reached through their root
    #: note): an agent carries many requests at once, so its words count only
    #: in `conversations`. A person's own decision counts wherever they said it.
    delegated: set[int] = field(default_factory=set)

    def may_decide(self, user_id: int) -> bool:
        if self.reserved is not None:
            return int(user_id) == self.reserved[0]
        return any(int(user_id) == holder for holder, _ in self.holders)

    def describe(self) -> str:
        if self.reserved is not None:
            return f"{self.reserved[1] or self.reserved[0]} (who reserved the approval)"
        return " or ".join(str(name or uid) for uid, name in self.holders) or "nobody known"


def _bare(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def _read(client, conversation: Conversation) -> tuple[Conversation, list[dict]]:
    found = locate(client, conversation) or conversation
    try:
        return found, client.topic_history(found.channel, found.topic, num_before=HISTORY)
    except ZulipError:
        return found, []


def decision(client, history: list[dict], owner: int | None, channel: str, topic: str) -> Decision:
    """Who holds the acceptance of the mission whose conversation is
    `history`: its requester and everybody up the requester's root notes to
    the request's origin — or the one person who reserved it."""
    found = Decision(conversations={(channel, _bare(topic))})
    speakers = [m for m in history if is_speech(m) and m.get("sender_id") != owner]
    notes = [m for m in history if m.get("sender_id") != owner and parse_rootchat(m.get("content"))]
    requester = int((notes or speakers or [{}])[0].get("sender_id") or 0)
    found.requester = requester
    if not requester:
        return found
    names = {int(m.get("sender_id") or 0): str(m.get("sender_full_name") or "") for m in history}
    found.holders.append((requester, names.get(requester, "")))
    if notes:
        found.delegated.add(requester)
    reservations = [r for m in history if (r := parse_reservation(m.get("content")))]
    asker, messages, depth = requester, history, 0
    while depth < CHAIN_DEPTH:
        depth += 1
        home = effective_rootchat(messages, asker)
        if home is None:
            break
        where, messages = _read(client, home)
        found.conversations.add((where.channel, _bare(where.topic)))
        reservations += [r for m in messages if (r := parse_reservation(m.get("content")))]
        names.update({int(m.get("sender_id") or 0): str(m.get("sender_full_name") or "") for m in messages})
        # Whoever asked there: a root note of another agent, else the first
        # person who spoke that is not the asker.
        above = effective_rootchat(messages, asker)
        first = next((m for m in messages if is_speech(m) and int(m.get("sender_id") or 0) != asker), None)
        if above is not None and (above.channel, _bare(above.topic)) != (where.channel, _bare(where.topic)):
            continue  # the same asker, one conversation further up (a run opened from a desk)
        if first is None:
            break
        asker = int(first.get("sender_id") or 0)
        if asker and all(asker != h for h, _ in found.holders):
            found.holders.append((asker, names.get(asker, "")))
    if reservations:
        found.reserved = reservations[-1]
    return found


def _shown(tasks) -> int:
    """The newest result shown for review among the mission's tasks, as
    their close-outs bound it (`[change] accepted … +shown=<id>`, failsafe
    p4). A task closed before that binding existed says nothing here."""
    newest = 0
    for task in tasks:
        for record in getattr(task, "records", []) or []:
            match = _SHOWN.search(str(record.get("value") or "")) if record.get("tag") == "change" else None
            if match:
                newest = max(newest, int(match.group(1)))
    return newest


def parse_acceptance(content) -> tuple[int, int, str] | None:
    """`(evidence id, user id, name)` of an acceptance note, or None."""
    value = parse_note(content, ACCEPTANCE_TAG)
    if value is None:
        return None
    match = _ACCEPTANCE.match(value.strip())
    if match is None:
        return None
    return int(match.group("evidence")), int(match.group("by")), match.group("name") or ""


@dataclass
class Acceptance:
    """What `accept_mission` found and did."""

    mission: int
    channel: str
    topic: str
    evidence: int
    by_id: int
    by_name: str
    already: bool = False
    tasks: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    resolved: bool = False
    after: int = 0

    @property
    def label(self) -> str:
        return f"m{self.mission}"

    def summary(self) -> str:
        whose = f"{self.by_name or self.by_id}" + (f" (#{self.evidence})" if self.evidence else "")
        if self.already and not self.written:
            if not self.by_id:
                return f"{self.label} is already done (closed before acceptances were recorded); nothing was written"
            return f"{self.label} is already done: accepted by {whose}; nothing was written"
        what = f"{self.label} is done: accepted by {whose}"
        if self.tasks:
            what += f"; task(s) {', '.join(self.tasks)} recorded accepted"
        if self.resolved:
            what += f"; #{self.channel} > {self.topic} resolved"
        return what


def _state(messages: list[dict], owner: int | None) -> str:
    """The newest `[state]` word by the owner, or `accepted`/`done` by anyone
    (the same rule the trace reads)."""
    for message in reversed(messages):
        value = parse_note(message.get("content"), STATE_TAG)
        if not value or not value.split():
            continue
        word = value.split()[0].lower()
        if owner is None or message.get("sender_id") == owner or word in (TASK_ACCEPTED, MISSION_DONE):
            return word
    return ""


def _mission_owner(messages: list[dict]) -> int | None:
    for message in messages:
        if parse_note(message.get("content"), "mission") is not None:
            return int(message.get("sender_id") or 0) or None
    return None


def accept_mission(
    client: ZulipClient,
    message_id: int,
    *,
    evidence: int | None = None,
    writer: dict | None = None,
    resolve: bool = True,
    log=default_log,
) -> Acceptance:
    """Record that the mission holding `message_id` is accepted, on the post
    `evidence`, and that it is done. See the module for what is refused.
    Raises `AcceptanceRefused` (nothing written) or `ZulipError` (a write
    failed part-way; repeating the call finishes it)."""
    me = writer or client.whoami()
    me_id = int(me.get("user_id") or 0)
    anchor = client.message(int(message_id), strict=True)
    if anchor is None:
        raise AcceptanceRefused(f"message {message_id} does not exist, so there is no mission to accept")
    result = trace(client, int(message_id))
    root = result.root
    if root is None:
        raise AcceptanceRefused(f"the conversation holding #{message_id} could not be read; nothing is written")
    match = _MISSION.match(root.identity or "")
    if match is None:
        # Said with how to name the mission: in p3 step 5 (trial G) a run passed
        # the accepting post itself as the first argument, read this refusal as
        # "cannot be recorded", and relayed the acceptance by post instead.
        raise AcceptanceRefused(
            f"#{message_id} is in #{root.channel} > {root.topic}, which is not a mission. Name the mission "
            "first — the number in its label m<number>, as its agent's close-out gives it — and the post "
            f"that accepted it second: `agentchat accept <number> --evidence {message_id}`")
    mission_id = int(match.group("id"))
    history = client.topic_history(root.channel, root.topic, num_before=HISTORY)
    owner = _mission_owner(history)
    word = _state(history, owner)
    recorded = [parsed for m in history if (parsed := parse_acceptance(m.get("content"))) is not None]
    done = Acceptance(mission_id, root.channel, root.topic, 0, me_id, str(me.get("full_name") or ""))
    if recorded:
        done.evidence, done.by_id, done.by_name = recorded[0]
    if word == MISSION_DONE:
        done.already = True
        if not recorded:
            done.by_id, done.by_name = 0, ""
        return done
    if word in CANCELLED_WORDS:
        raise AcceptanceRefused(f"m{mission_id} is {word}; a mission that was called off is not accepted")

    tasks = [child for child in root.children if (child.identity or "").startswith(f"task {mission_id}#")]
    if not tasks:
        raise AcceptanceRefused(f"m{mission_id} has no task, so there is no finished work to accept")
    serials = sorted(int(t.identity.split("#")[-1]) for t in tasks if t.identity.split("#")[-1].isdigit())
    if serials != list(range(1, len(serials) + 1)):
        # The tasks are found through their root notes, read from the newest
        # of the realm's notes; a gap means one was not seen, and accepting
        # over work nobody looked at is the one mistake this must not make.
        raise AcceptanceRefused(
            f"m{mission_id}'s tasks could not all be seen (found {', '.join(map(str, serials))}); "
            "nothing is written")
    unfinished = [t for t in tasks if t.note_state not in FINISHED_TASK and t.state != "cancelled"
                  and t.note_state not in CANCELLED_WORDS]
    if unfinished:
        listed = ", ".join(f"{t.identity.split('#')[-1]} ({t.state.replace('_', ' ')})" for t in unfinished)
        raise AcceptanceRefused(
            f"m{mission_id} still has unfinished task(s): {listed}. A task is closed by its run once you "
            "agree it is done; accept the mission after that")

    held = decision(client, history, owner, root.channel, root.topic)
    for task in tasks:
        held.conversations.add((task.channel, _bare(task.topic)))
    shown = _shown(tasks)
    if not recorded:
        if evidence is None:
            if me.get("is_bot", True) or not held.may_decide(me_id):
                raise AcceptanceRefused(
                    "an acceptance is recorded with the post where it was given: pass the message id of the "
                    f"words of {held.describe()} (--evidence). Only a holder recording their own decision in "
                    "person needs none")
            done.evidence, done.by_id, done.by_name = 0, me_id, str(me.get("full_name") or "")
        else:
            said = client.message(int(evidence), strict=True)
            if said is None:
                raise AcceptanceRefused(f"the evidence #{evidence} does not exist")
            speaker = int(said.get("sender_id") or 0)
            if owner is not None and speaker == owner:
                raise AcceptanceRefused(f"#{evidence} was written by the mission's own agent: a worker's own "
                                        "completion is not its requester's acceptance")
            if int(evidence) < mission_id:
                raise AcceptanceRefused(f"#{evidence} is older than m{mission_id} itself, so it cannot accept it")
            if not held.may_decide(speaker):
                raise AcceptanceRefused(
                    f"#{evidence} was written by {said.get('sender_full_name') or speaker}, who does not hold "
                    f"m{mission_id}'s acceptance; it is {held.describe()}'s to give")
            where = (str(said.get("display_recipient") or ""), _bare(str(said.get("subject") or "")))
            if speaker in held.delegated and where not in held.conversations:
                raise AcceptanceRefused(
                    f"#{evidence} is in #{where[0]} > {where[1]}, which is not m{mission_id}'s conversation, one of "
                    "its tasks', or a conversation it was requested from")
            if shown and int(evidence) < shown:
                raise AcceptanceRefused(
                    f"#{evidence} was said before the result it would accept was shown (#{shown}): an acceptance "
                    "follows the reviewed result")
            done.evidence, done.by_id = int(evidence), speaker
            done.by_name = str(said.get("sender_full_name") or "")
        done.after = shown

    # The record, in an order a repeat can finish: the tasks, the note that
    # says whose decision it is, then `done`, then the resolve.
    for task in tasks:
        if task.note_state in (TASK_ACCEPTED, *CANCELLED_WORDS) or task.state == "cancelled":
            continue
        client.send_to_channel(task.channel, task.topic, note(STATE_TAG, TASK_ACCEPTED))
        done.tasks.append(task.identity.split("#")[-1])
        done.written.append(f"{task.channel}/{task.topic}: accepted")
    if not recorded:
        client.send_to_channel(root.channel, root.topic,
                               acceptance_note(done.evidence, done.by_id, done.by_name, done.after))
        done.written.append(f"{root.channel}/{root.topic}: acceptance #{done.evidence}")
    client.send_to_channel(root.channel, root.topic, note(STATE_TAG, MISSION_DONE))
    done.written.append(f"{root.channel}/{root.topic}: done")
    if resolve and not root.topic.startswith(RESOLVED_TOPIC_PREFIX):
        try:
            latest = client.topic_history(root.channel, root.topic, num_before=1)
            if latest:
                client.resolve_topic(int(latest[-1]["id"]), root.topic)
                done.resolved = True
                done.topic = f"{RESOLVED_TOPIC_PREFIX}{root.topic}"
        except ZulipError as error:
            # The record is complete; a ✔ is display. Said, not raised.
            log(f"m{mission_id} is done, but its conversation could not be resolved: {error!r}")
    log(f"acceptance: {done.summary()}")
    return done
