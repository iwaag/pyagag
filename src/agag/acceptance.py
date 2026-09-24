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

What it refuses, before writing anything: a conversation that is not a
mission; a mission cancelled, replaced or retired; a task not finished yet
(accepting the last task and the mission may coincide in the requester's
words, but the record waits until the task is closed); an acceptance without
the post it rests on, unless a person is recording their own decision; and
evidence written by the recorder itself or by the mission's owner — an
acceptance is quoted, never performed.

**Repeating it is safe.** Every write is skipped when it is already there, and
an acceptance note already on record is the one that counts: a retry after
an interruption finishes the record the first attempt began, with the first
attempt's evidence, and a repeat after `done` changes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .selfnote import note, parse_note
from .trace import CANCELLED_WORDS, trace
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, ZulipError, log as default_log

ACCEPTANCE_TAG = "acceptance"
STATE_TAG = "state"
MISSION_DONE = "done"
TASK_ACCEPTED = "accepted"
#: A task the requester may be said to have accepted: its run closed it.
FINISHED_TASK = ("completed", "accepted")
HISTORY = 1000
_ACCEPTANCE = re.compile(r"^#(?P<evidence>\d+) by (?P<by>\d+)(?: \((?P<name>.*)\))?")
_MISSION = re.compile(r"^mission m(?P<id>\d+)\b")

__all__ = [
    "ACCEPTANCE_TAG",
    "Acceptance",
    "AcceptanceRefused",
    "accept_mission",
    "acceptance_note",
    "parse_acceptance",
]


class AcceptanceRefused(RuntimeError):
    """The mission cannot be accepted now, and nothing was written."""


def acceptance_note(evidence: int, by_id: int, by_name: str = "") -> str:
    """`[selfnote][acceptance] #<evidence> by <user id> (<name>)`. Evidence 0
    means a person recorded their own decision with no post to point at (the
    completion door)."""
    name = f" ({by_name})" if by_name else ""
    return note(ACCEPTANCE_TAG, f"#{int(evidence)} by {int(by_id)}{name}")


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
        raise AcceptanceRefused(
            f"#{root.channel} > {root.topic} is not a mission (it carries no mission note of its owner's); "
            "only a mission is accepted this way")
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

    if not recorded:
        if evidence is None:
            if me.get("is_bot", True):
                raise AcceptanceRefused(
                    "an acceptance is recorded with the post where it was given: pass the message id of the "
                    "requester's words (--evidence). Only a person recording their own decision needs none")
            done.evidence, done.by_id, done.by_name = 0, me_id, str(me.get("full_name") or "")
        else:
            said = client.message(int(evidence), strict=True)
            if said is None:
                raise AcceptanceRefused(f"the evidence #{evidence} does not exist")
            speaker = int(said.get("sender_id") or 0)
            if speaker == me_id:
                raise AcceptanceRefused(
                    f"#{evidence} is your own post; an acceptance is the requester's decision — pass the post "
                    "where they gave it")
            if owner is not None and speaker == owner:
                raise AcceptanceRefused(f"#{evidence} was written by the mission's own agent, not by its requester")
            if int(evidence) < mission_id:
                raise AcceptanceRefused(f"#{evidence} is older than m{mission_id} itself, so it cannot accept it")
            done.evidence, done.by_id = int(evidence), speaker
            done.by_name = str(said.get("sender_full_name") or "")

    # The record, in an order a repeat can finish: the tasks, the note that
    # says whose decision it is, then `done`, then the resolve.
    for task in tasks:
        if task.note_state in (TASK_ACCEPTED, *CANCELLED_WORDS) or task.state == "cancelled":
            continue
        client.send_to_channel(task.channel, task.topic, note(STATE_TAG, TASK_ACCEPTED))
        done.tasks.append(task.identity.split("#")[-1])
        done.written.append(f"{task.channel}/{task.topic}: accepted")
    if not recorded:
        client.send_to_channel(root.channel, root.topic, acceptance_note(done.evidence, done.by_id, done.by_name))
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
