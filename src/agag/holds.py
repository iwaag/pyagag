"""A person's hold on work, kept where the request is (failsafe p6).

A hold says: *this* decision about *this* work is a person's, and nobody —
no agent, no monitor — should act on the work until it is made. Until p6 a
hold was a line in Observer's private `held.json`: a key, a free-text why
and a time. Nothing said which decision it protected or what would settle
it, so m11741's hold (o11711, "how to resume it is the Developer's
question") outlived the resume, the acceptance and the integration, and
kept the panel saying "needs you" until somebody removed the line by hand —
which left no trace of who, when or why.

Now a hold is a record in the request's own conversation (its origin, which
is never archived), written by whoever records it on the holder's words:

    [selfnote][hold] <purpose> a<unit> by <user id> (<name>) #<evidence> — <why>

- **purpose** — what decision it protects:
  - `acceptance`: accepting the unit's result (a mission, an asset);
  - `resume`: how, or whether, stopped work goes on;
  - `decision`: anything else a person must decide;
  - `indefinite`: kept on purpose until the person lets it go.
- **unit** — the anchor of the work it covers (`Node.anchor`); the
  request's own anchor covers the whole request.
- **by** — the person who holds it; **evidence** — their post saying so (0
  when they record it in person).

**A hold is released** by one of two things, both on record:

- **the path that settles its purpose** — read from the records, so no
  tool has to remember to release anything and a retry or a restart
  reaches the same answer:
  - `acceptance`: the unit's `[acceptance]` record, or its cancellation;
  - `resume`: the unit's owner serving it again after the hold (a later
    acknowledgement that worked or answered), or the unit finished or
    cancelled by record;
  - `decision` and `indefinite`: never by themselves;
- **an explicit release**, `[selfnote][hold-release] #<hold> by <user id>
  (<name>) [for <holder> (<name>)] #<evidence> — <why>`, on the holder's
  words or those of whoever carries the holder's full authority
  (`agag.people`, failsafe p6 ex2: the Omni Agent for the Developer, either
  way round; `by` is who actually spoke) (`agentchat release`,
  `agobserver.hold --release`).

Unrelated activity releases nothing: a new post, a ✔, another unit's
acceptance, work that started before the hold. A hold whose settlement
cannot be established stays `held`, and says what it waits for — never an
unexplained "needs you". Its history (placed, settled or released, by
whom, on which post) is the conversation's.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .people import acts_for, for_suffix, principal_of
from .selfnote import note, parse_note

__all__ = [
    "HOLD_TAG",
    "RELEASE_TAG",
    "PURPOSES",
    "Hold",
    "hold_note",
    "release_note",
    "parse_hold",
    "parse_release",
    "holds_of",
    "active",
]

HOLD_TAG = "hold"
RELEASE_TAG = "hold-release"
PURPOSES = ("acceptance", "resume", "decision", "indefinite")
_HOLD = re.compile(r"^(?P<purpose>[a-z]+) a(?P<unit>\d+) by (?P<by>\d+)(?: \((?P<name>[^)]*)\))? #(?P<evidence>\d+)"
                   r"(?: — (?P<why>.*))?$", re.S)
_RELEASE = re.compile(r"^#(?P<hold>\d+) by (?P<by>\d+)(?: \((?P<name>[^)]*)\))?"
                      r"(?: for (?P<for>\d+)(?: \((?P<for_name>[^)]*)\))?)? #(?P<evidence>\d+)"
                      r"(?: — (?P<why>.*))?$", re.S)
#: What each purpose waits for, said to whoever reads a hold still in force.
WAITS_FOR = {
    "acceptance": "{name}'s acceptance of {label} (or its cancellation)",
    "resume": "{name}'s decision on how {label} goes on (its owner serving it again, or its end on record)",
    "decision": "{name}'s decision: {why}",
    "indefinite": "{name}, who keeps it on purpose until they release it",
}


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def hold_note(purpose: str, unit: int, by_id: int, by_name: str = "", evidence: int = 0, why: str = "") -> str:
    if purpose not in PURPOSES:
        raise ValueError(f"a hold protects one of {', '.join(PURPOSES)}, not {purpose!r}")
    name = f" ({_one_line(by_name).replace('(', '').replace(')', '')})" if by_name else ""
    reason = f" — {_one_line(why)}" if why.strip() else ""
    return note(HOLD_TAG, f"{purpose} a{int(unit)} by {int(by_id)}{name} #{int(evidence)}{reason}")


def release_note(hold_id: int, by_id: int, by_name: str = "", evidence: int = 0, why: str = "",
                 on_behalf: str = "") -> str:
    name = f" ({_one_line(by_name).replace('(', '').replace(')', '')})" if by_name else ""
    reason = f" — {_one_line(why)}" if why.strip() else ""
    return note(RELEASE_TAG, f"#{int(hold_id)} by {int(by_id)}{name}{on_behalf} #{int(evidence)}{reason}")


def parse_hold(content) -> dict | None:
    value = parse_note(content, HOLD_TAG)
    match = _HOLD.match(value.strip()) if value is not None else None
    if match is None or match.group("purpose") not in PURPOSES:
        return None
    return {"purpose": match.group("purpose"), "unit": int(match.group("unit")), "by": int(match.group("by")),
            "name": match.group("name") or "", "evidence": int(match.group("evidence")),
            "why": (match.group("why") or "").strip()}


def parse_release(content) -> dict | None:
    value = parse_note(content, RELEASE_TAG)
    match = _RELEASE.match(value.strip()) if value is not None else None
    if match is None:
        return None
    return {"hold": int(match.group("hold")), "by": int(match.group("by")), "name": match.group("name") or "",
            "for": int(match.group("for") or 0), "for_name": match.group("for_name") or "",
            "evidence": int(match.group("evidence")), "why": (match.group("why") or "").strip()}


@dataclass
class Hold:
    id: int
    purpose: str
    unit: int
    by: int
    name: str
    evidence: int
    why: str
    at: int = 0
    written_by: str = ""
    label: str = ""
    #: `held`, `settled` (its purpose's path is on record) or `released`
    #: (explicitly, on the holder's words).
    state: str = "held"
    #: `{id, how, what}` of whatever ended it.
    ended_by: dict | None = None
    #: What it still waits for, while `held`.
    waits_for: str = ""
    history: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _nodes(result) -> list:
    return list(result.nodes()) if result is not None and result.root is not None else []


def _find(result, anchor: int):
    return next((n for n in _nodes(result) if int(n.anchor or 0) == int(anchor)), None)


def _settled(hold: Hold, unit) -> dict | None:
    """What on record settles this hold's purpose, or None."""
    if unit is None or hold.purpose in ("decision", "indefinite"):
        return None
    records = list(getattr(unit, "records", []) or [])
    for record in records:
        if record.get("tag") == "state" and (record.get("value") or "").split()[:1] in (
                ["cancelled"], ["replaced"], ["retired"]):
            return {"id": record["id"], "how": "cancelled", "what": f"{unit.identity or unit.topic} cancelled at #{record['id']}"}
    if hold.purpose == "acceptance":
        found = next((r for r in records if r.get("tag") == "acceptance"), None)
        if found is not None:
            return {"id": found["id"], "how": "accepted",
                    "what": f"{unit.identity or unit.topic} accepted: {found.get('value')} (#{found['id']})"}
        return None
    # resume: the owner served it again after the hold, or it ended by record.
    if unit.state in ("done",) and unit.note_state:
        last = next((r for r in reversed(records) if r.get("tag") == "state"), None)
        if last is not None:
            return {"id": last["id"], "how": "finished",
                    "what": f"{unit.identity or unit.topic} {last.get('value')} at #{last['id']}"}
    for node in [unit, *_descendants(unit)]:
        if int(node.ack or 0) > hold.id and (int(node.work or 0) > int(node.ack) or node.execution == "ended"):
            return {"id": int(node.ack), "how": "resumed",
                    "what": f"{node.owner or 'its owner'} served {node.identity or node.topic} again at #{node.ack}"}
    return None


def _descendants(node) -> list:
    found, stack = [], list(node.children)
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(child.children)
    return found


def holds_of(result, messages: Iterable[dict] | None = None) -> list[Hold]:
    """Every hold in the request traced as `result` — read from the notes in
    its origin conversation (`messages`, else the root's records) — with its
    state decided (module doc)."""
    rows = list(messages) if messages is not None else []
    if messages is None and result is not None and result.root is not None:
        rows = [{"id": r["id"], "timestamp": r["at"], "sender_id": r["by"], "sender_full_name": "",
                 "content": note(r["tag"], r["value"])} for r in result.root.records if r.get("tag") in
                (HOLD_TAG, RELEASE_TAG)]
    found: list[Hold] = []
    releases: dict[int, dict] = {}
    for message in rows:
        content = message.get("content")
        parsed = parse_hold(content)
        if parsed is not None:
            found.append(Hold(id=int(message.get("id") or 0), at=int(message.get("timestamp") or 0),
                              written_by=str(message.get("sender_full_name") or message.get("sender_id") or ""),
                              **parsed))
            continue
        released = parse_release(content)
        if released is not None:
            releases.setdefault(released["hold"], {**released, "id": int(message.get("id") or 0),
                                                   "at": int(message.get("timestamp") or 0)})
    for hold in found:
        unit = _find(result, hold.unit)
        hold.label = (unit.identity or unit.topic) if unit is not None else f"a{hold.unit}"
        who = hold.name or str(hold.by)
        hold.history.append({"id": hold.id, "at": hold.at, "event": "held",
                             "what": f"{hold.purpose} of {hold.label}, held by {who} on #{hold.evidence}"
                                     + (f": {hold.why}" if hold.why else "")})
        release = releases.get(hold.id)
        if release is not None:
            hold.state = "released"
            hold.ended_by = {"id": release["id"], "how": "released",
                             "what": f"released by {release['name'] or release['by']}"
                                     + (f" for {release['for_name'] or release['for']}" if release["for"] else "")
                                     + f" on #{release['evidence']}"
                                     + (f": {release['why']}" if release["why"] else "")}
        else:
            settled = _settled(hold, unit)
            if settled is not None:
                hold.state, hold.ended_by = "settled", settled
        if hold.ended_by is not None:
            hold.history.append({"id": hold.ended_by["id"], "event": hold.state, "what": hold.ended_by["what"]})
        else:
            hold.waits_for = WAITS_FOR[hold.purpose].format(name=who, label=hold.label, why=hold.why or "see #"
                                                            + str(hold.evidence))
            if unit is None and hold.unit != (result.root.anchor if result is not None and result.root else 0):
                hold.waits_for += f" — a{hold.unit} is not in this request's trace"
    return found


def active(holds: Iterable[Hold]) -> list[Hold]:
    return [h for h in holds if h.state == "held"]


def covers(hold: Hold, result, anchor: int) -> bool:
    """Whether a hold in force covers the conversation `anchor`: the unit it
    names, or anything opened below it (the request's root covers all)."""
    unit = _find(result, hold.unit)
    if unit is None:
        return result is not None and result.root is not None and int(hold.unit) == int(result.root.anchor)
    return int(anchor) == int(unit.anchor) or any(int(n.anchor) == int(anchor) for n in _descendants(unit))


# --- the tools (`agentchat hold` / `release`) ---------------------------------------------


class HoldRefused(RuntimeError):
    """Nothing was written, and why."""


def _origin_of(client, message_id: int) -> tuple[str, str, int]:
    """`(channel, live topic, origin id)` of the conversation holding the
    message: a hold lives in the request's own conversation."""
    from .zulip import channel_name, topic_history_across_resolve

    found = client.message(int(message_id))
    if not found:
        raise HoldRefused(f"message {message_id} does not exist or could not be read")
    channel, topic = channel_name(found), str(found.get("subject") or "")
    history = topic_history_across_resolve(client, channel, topic, 1000)
    origin = min((int(m.get("id") or 0) for m in history), default=int(message_id))
    return channel, topic, origin


def read_holds(client, message_id: int):
    """`(holds, result, where)` for the request holding the message."""
    from .trace import trace
    from .zulip import topic_history_across_resolve

    channel, topic, origin = _origin_of(client, message_id)
    result = trace(client, origin)
    history = topic_history_across_resolve(client, channel, topic, 1000)
    return holds_of(result, history), result, (channel, topic, origin)


def hold_lines(holds: list[Hold], where: tuple[str, str, int]) -> list[str]:
    lines = [f"holds on the request #{where[2]} ({where[0]}/{where[1]}): {len(active(holds))} in force"]
    for hold in holds:
        lines.append(f"  #{hold.id} {hold.state.upper()} — {hold.purpose} of {hold.label} (a{hold.unit}), "
                     f"held by {hold.name or hold.by} on #{hold.evidence}" + (f": {hold.why}" if hold.why else ""))
        if hold.state == "held":
            lines.append(f"      waits for {hold.waits_for}")
        for event in hold.history[1:]:
            lines.append(f"      {event['event']} at #{event['id']}: {event['what']}")
    if not holds:
        lines.append("  none")
    return lines


def holds_command(client, args, out, *, home=None) -> int:
    import json

    message_id = args.message_id
    if message_id is None and home is not None:
        message_id = home.anchor or client.topic_last_id(home.channel, home.topic)
    if message_id is None:
        print("hold needs a message id: none was given and this run serves no conversation", file=out)
        return 2
    try:
        holds, result, where = read_holds(client, int(message_id))
        if args.purpose is None:
            if args.json:
                print(json.dumps([h.as_dict() for h in holds], ensure_ascii=False, indent=1), file=out)
            else:
                print("\n".join(hold_lines(holds, where)), file=out)
            return 0
        written = place(client, where, result, args.purpose, int(args.unit or where[2]), int(args.evidence or 0),
                        " ".join(args.why or []), holds)
    except HoldRefused as refused:
        print(f"refused: {refused}", file=out)
        return 1
    print(f"held: #{written} in {where[0]}/{where[1]}", file=out)
    return 0


def place(client, where, result, purpose: str, unit: int, evidence: int, why: str, holds: list[Hold],
          *, in_person: tuple[int, str] | None = None) -> int:
    """Record a hold on `unit` on the holder's post `evidence` — or, from an
    operator's own terminal (`agobserver.hold`), for the person recording it
    in person (`in_person`, evidence 0)."""
    me = client.whoami()
    if in_person is not None and not evidence:
        by, name = int(in_person[0]), str(in_person[1] or "")
    else:
        if not evidence:
            raise HoldRefused("--evidence names the post where the person asked to keep the decision; the holder "
                              "is whoever wrote it")
        post = client.message(int(evidence))
        if not post:
            raise HoldRefused(f"#{evidence} does not exist or could not be read")
        by, name = int(post.get("sender_id") or 0), str(post.get("sender_full_name") or "")
        # An agent's own words are not a person's hold — unless it carries a
        # person's full authority, whose words are that person's (p6 ex2).
        if by == int(me["user_id"]) and principal_of(by) is None:
            raise HoldRefused(f"#{evidence} is your own post: a hold is a person's, on their words")
    nodes = {int(n.anchor): n for n in result.nodes()} if result is not None and result.root is not None else {}
    if unit not in nodes:
        # Any post of the unit's conversation names it: its node's anchor is
        # what the hold records.
        from .zulip import RESOLVED_TOPIC_PREFIX, channel_name

        found = client.message(int(unit))
        bare = lambda t: t[len(RESOLVED_TOPIC_PREFIX):] if t.startswith(RESOLVED_TOPIC_PREFIX) else t
        match = next((n for n in nodes.values() if found and n.channel == channel_name(found)
                      and bare(n.topic) == bare(str(found.get("subject") or ""))), None)
        if match is None:
            raise HoldRefused(f"#{unit} is not in this request (`agentchat trace {where[2]}` lists its anchors)")
        unit = int(match.anchor)
    if nodes[unit].owner and nodes[unit].owner == name:
        raise HoldRefused(f"#{evidence} is by {name}, the agent doing that work: a hold is its requester's side")
    same = [h for h in active(holds) if h.purpose == purpose and h.unit == unit and h.by == by]
    if same:
        return same[0].id  # already held: nothing written
    return int(client.send_to_channel(where[0], where[1], hold_note(purpose, unit, by, name, evidence, why)) or 0)


def release_command(client, args, out) -> int:
    try:
        written, hold = release(client, int(args.hold_id), int(args.evidence), " ".join(args.why or []))
    except HoldRefused as refused:
        print(f"refused: {refused}", file=out)
        return 1
    if not written:
        print(f"#{hold.id} is already {hold.state}: {hold.ended_by['what'] if hold.ended_by else ''}; nothing written",
              file=out)
        return 0
    print(f"released #{hold.id}: #{written}", file=out)
    return 0


def release(client, hold_id: int, evidence: int, why: str = "", *, in_person: int | None = None) -> tuple[int, Hold]:
    """Release a hold on its holder's own post — or, from an operator's own
    terminal, by the holder in person (`in_person` = their user id, evidence
    0). Returns `(written id, hold)`; 0 written when it was already settled
    or released."""
    holds, _, where = read_holds(client, int(hold_id))
    hold = next((h for h in holds if h.id == int(hold_id)), None)
    if hold is None:
        raise HoldRefused(f"#{hold_id} is not a hold")
    if hold.state != "held":
        return 0, hold
    if in_person is not None and not evidence:
        if isinstance(in_person, tuple):
            who, who_name = int(in_person[0]), str(in_person[1] or "")
        else:
            who, who_name = int(in_person), (hold.name if int(in_person) == hold.by else "")
        if not acts_for(who, hold.by):
            raise HoldRefused(f"#{hold_id} is {hold.name or hold.by}'s hold: only they, or whoever carries their "
                              "full authority, release it")
        written = int(client.send_to_channel(where[0], where[1], release_note(
            hold.id, who, who_name, 0, why, for_suffix(who, hold.by, hold.name))) or 0)
        return written, hold
    post = client.message(int(evidence))
    if not post:
        raise HoldRefused(f"#{evidence} does not exist or could not be read")
    speaker = int(post.get("sender_id") or 0)
    if not acts_for(speaker, hold.by):
        raise HoldRefused(f"#{evidence} is not {hold.name or hold.by}'s: a hold is released on its holder's words, "
                          "or those of whoever carries their full authority "
                          f"(or settles by itself when {hold.waits_for.split(' (')[0]} is on record)")
    # The record names who actually spoke, and whose authority it was.
    written = int(client.send_to_channel(where[0], where[1], release_note(
        hold.id, speaker, str(post.get("sender_full_name") or ""), evidence, why,
        for_suffix(speaker, hold.by, hold.name))) or 0)
    return written, hold
