"""A decision about a request's standing, kept where the request is (failsafe p6 ex1).

Until p6 ex1, Observer's private `retired.json` said "a person checked this
and found nothing owed": a key, a free-text why and a time, read by Observer
alone and lapsing the moment *anything* was posted in the request — a
receipt repair, a bookkeeping note. The panel showed the same requests as
active work, and nothing said whether a retired request had been finished,
abandoned, or was merely not to be nagged about.

Those are different decisions, and each is now a record in the request's
origin conversation (which is never archived), beside its holds:

    [selfnote][disposition] <kind> a<unit> upto=#<id> by <user id> (<name>) #<evidence> — <why>
    [selfnote][disposition-reversed] #<disposition> by <user id> (<name>) #<evidence> — <why>

- **kind**:
  - `suppressed` — **monitoring suppressed**: the work stays open and is
    shown as such, with the reason; Observer does not act on it;
  - `completed` — **request ended**, its requested outcome reached;
  - `cancelled` / `withdrawn` — **request ended** without it: stopped on a
    decision, or taken back by whoever asked (a stray post, a trial nobody
    continues). Never read as success.
- **unit** — the anchor of the work it covers (`Node.anchor`) and everything
  below it; the request's own anchor covers the whole request.
- **upto** — the newest **substantive** post (speech that is not an ack) in
  that scope when the decision was made. A later substantive post is new
  activity the decision never saw: the part of the scope it happened in is
  no longer covered, so a new request, question or result is visible and
  monitored as ever. Bookkeeping — selfnotes (receipts, served marks, holds,
  these records), ✔ and other system notices, a restart — moves nothing.
  Neither does the recorder's own reply closing the serving it recorded the
  decision in (`end=` an ack older than the record): Front saying
  "recorded" is the decision being reported, not something new.
- **by** / **evidence** — the decision maker and their post (0 when an
  operator records their own decision in person, `agobserver.disposition`).

What an ended disposition does to the tree (`apply`, called by the trace, so
the panel, Observer and `agentchat trace` read the same thing): the unit it
names reads `done` (`completed`) or `cancelled`; unfinished work below it
reads `cancelled` — ended with it, not completed — and is listed as the
disposition's `remaining` obligations with what its owner's own record still
says, so the owner's existing path (`agrun finish`, a cancellation) can
reconcile it. A missing receipt under it is settled bookkeeping. Work
finished by its own record is untouched.

A repeat of the same decision writes nothing; a reversal ends it on record;
a restart reads the same records.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from .selfnote import note, parse_note

__all__ = [
    "DISPOSITION_TAG",
    "REVERSED_TAG",
    "KINDS",
    "ENDED",
    "Disposition",
    "DispositionRefused",
    "apply",
    "disposition_note",
    "parse_disposition",
    "parse_reversal",
    "reversal_note",
    "suppressed_anchors",
]

DISPOSITION_TAG = "disposition"
REVERSED_TAG = "disposition-reversed"
KINDS = ("suppressed", "completed", "cancelled", "withdrawn")
ENDED = ("completed", "cancelled", "withdrawn")
_DISPOSITION = re.compile(r"^(?P<kind>[a-z]+) a(?P<unit>\d+) upto=#(?P<upto>\d+) by (?P<by>\d+)"
                          r"(?: \((?P<name>[^)]*)\))? #(?P<evidence>\d+)(?: — (?P<why>.*))?$", re.S)
_REVERSAL = re.compile(r"^#(?P<disposition>\d+) by (?P<by>\d+)(?: \((?P<name>[^)]*)\))? #(?P<evidence>\d+)"
                       r"(?: — (?P<why>.*))?$", re.S)
#: What each kind says on the card and in the trace.
SAYS = {
    "suppressed": "monitoring suppressed",
    "completed": "ended: completed",
    "cancelled": "ended: cancelled",
    "withdrawn": "ended: withdrawn",
}


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _name(name: str) -> str:
    return f" ({_one_line(name).replace('(', '').replace(')', '')})" if name else ""


def disposition_note(kind: str, unit: int, upto: int, by_id: int, by_name: str = "", evidence: int = 0,
                     why: str = "") -> str:
    if kind not in KINDS:
        raise ValueError(f"a disposition is one of {', '.join(KINDS)}, not {kind!r}")
    reason = f" — {_one_line(why)}" if str(why).strip() else ""
    return note(DISPOSITION_TAG, f"{kind} a{int(unit)} upto=#{int(upto)} by {int(by_id)}{_name(by_name)} "
                                 f"#{int(evidence)}{reason}")


def reversal_note(disposition_id: int, by_id: int, by_name: str = "", evidence: int = 0, why: str = "") -> str:
    reason = f" — {_one_line(why)}" if str(why).strip() else ""
    return note(REVERSED_TAG, f"#{int(disposition_id)} by {int(by_id)}{_name(by_name)} #{int(evidence)}{reason}")


def parse_disposition(content) -> dict | None:
    value = parse_note(content, DISPOSITION_TAG)
    match = _DISPOSITION.match(value.strip()) if value is not None else None
    if match is None or match.group("kind") not in KINDS:
        return None
    return {"kind": match.group("kind"), "unit": int(match.group("unit")), "upto": int(match.group("upto")),
            "by": int(match.group("by")), "name": match.group("name") or "",
            "evidence": int(match.group("evidence")), "why": (match.group("why") or "").strip()}


def parse_reversal(content) -> dict | None:
    value = parse_note(content, REVERSED_TAG)
    match = _REVERSAL.match(value.strip()) if value is not None else None
    if match is None:
        return None
    return {"disposition": int(match.group("disposition")), "by": int(match.group("by")),
            "name": match.group("name") or "", "evidence": int(match.group("evidence")),
            "why": (match.group("why") or "").strip()}


@dataclass
class Disposition:
    id: int
    kind: str
    unit: int
    upto: int
    by: int
    name: str
    evidence: int
    why: str
    at: int = 0
    #: The account that wrote the record.
    written_by: int = 0
    label: str = ""
    #: `in_force`, `reversed`, or `lapsed` (every part of its scope has new
    #: activity since `upto`, so it covers nothing any more).
    state: str = "in_force"
    reversed_by: dict | None = None
    #: Anchors it covers now; those with new activity since `upto`.
    covers: list[int] = field(default_factory=list)
    new_activity: list[dict] = field(default_factory=list)
    #: Unfinished work below an ended unit: what its owner's record still
    #: says (`{anchor, label, owner, state, detail}`).
    remaining: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def ended(self) -> bool:
        return self.kind in ENDED

    def says(self) -> str:
        who = self.name or str(self.by)
        return f"{SAYS[self.kind]} by {who} (#{self.id}" + (f", on #{self.evidence}" if self.evidence else "") \
            + ")" + (f": {self.why}" if self.why else "")


def _nodes(result) -> list:
    return list(result.nodes()) if result is not None and result.root is not None else []


def _below(node) -> list:
    found, stack = [], list(node.children)
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(child.children)
    return found


def read(records: Iterable[dict]) -> list[Disposition]:
    """The dispositions in a request's record notes (`Node.records` of its
    root, `{tag, value, id, at, by}`), reversals applied, oldest first."""
    found: list[Disposition] = []
    reversals: dict[int, dict] = {}
    for record in records:
        if record.get("tag") == DISPOSITION_TAG:
            parsed = parse_disposition(note(DISPOSITION_TAG, record.get("value") or ""))
            if parsed is not None:
                found.append(Disposition(id=int(record["id"]), at=int(record.get("at") or 0),
                                         written_by=int(record.get("by") or 0), **parsed))
        elif record.get("tag") == REVERSED_TAG:
            parsed = parse_reversal(note(REVERSED_TAG, record.get("value") or ""))
            if parsed is not None:
                reversals.setdefault(parsed["disposition"], {**parsed, "id": int(record["id"])})
    for disposition in found:
        reversal = reversals.get(disposition.id)
        if reversal is not None:
            disposition.state = "reversed"
            disposition.reversed_by = {"id": reversal["id"], "by": reversal["by"], "name": reversal["name"],
                                       "evidence": reversal["evidence"], "why": reversal["why"]}
    return found


def apply(result) -> list[Disposition]:
    """Read the request's dispositions and apply those in force to its tree
    (module doc). Each node covered gets `Node.disposition`; a node whose
    scope saw new activity gets it with `covered: False`. Returns them all."""
    if result is None or result.root is None:
        return []
    found = read(result.root.records)
    nodes = {int(n.anchor): n for n in _nodes(result)}
    for disposition in found:
        unit = nodes.get(int(disposition.unit))
        disposition.label = (unit.identity or unit.topic) if unit is not None else f"a{disposition.unit}"
    # The newest decision in force decides for a node; a node is covered
    # while nothing substantive happened in it after the decision's `upto`.
    in_force = sorted((d for d in found if d.state == "in_force"), key=lambda d: d.id, reverse=True)
    for disposition in in_force:
        unit = nodes.get(int(disposition.unit))
        if unit is None:
            continue
        for node in [unit, *_below(unit)]:
            if getattr(node, "disposition", None):
                continue  # a newer decision already spoke for it
            fresh = _new_activity(node, disposition)
            node.disposition = {"id": disposition.id, "kind": disposition.kind, "covered": not fresh,
                                "unit": disposition.unit, "says": disposition.says()}
            if fresh:
                disposition.new_activity.append({"anchor": int(node.anchor), "label": node.identity or node.topic,
                                                 "since": fresh})
                continue
            disposition.covers.append(int(node.anchor))
            if disposition.ended:
                _end(node, disposition, named=node is unit)
    for disposition in in_force:
        if not disposition.covers and disposition.new_activity:
            disposition.state = "lapsed"
    return found


def _new_activity(node, disposition: Disposition) -> int:
    """The first substantive post in `node` the decision did not see, or 0."""
    rows = getattr(node, "activity", None)
    if rows is None:
        return int(node.last_substantive) if int(getattr(node, "last_substantive", 0) or 0) > disposition.upto else 0
    for mid, sender, ends in sorted(rows):
        if mid <= disposition.upto:
            continue
        if sender == disposition.written_by and ends and ends < disposition.id:
            continue  # the recorder reporting the decision it just recorded
        return mid
    return 0


def _end(node, disposition: Disposition, *, named: bool) -> None:
    if node.state in ("done", "cancelled"):
        return
    if not named and not node.identity and node.state in ("awaiting_requester", "answered") \
            and getattr(node, "taken_up", False) and node.execution != "open":
        return  # a plain exchange already complete: nothing of it is left to end
    before = {"anchor": int(node.anchor), "label": node.identity or node.topic, "owner": node.owner,
              "state": node.state, "detail": node.detail}
    if not named:
        disposition.remaining.append(before)
    word = "done" if (named and disposition.kind == "completed") else "cancelled"
    head = disposition.says() if named else f"ended with {disposition.label} ({SAYS[disposition.kind]}, #{disposition.id})"
    node.detail = f"{head}; its own record: {node.state.replace('_', ' ')} — {node.detail}" if node.detail else head
    node.state = word
    node.holder = "done"
    node.owed_to = []
    if node.receipt and node.receipt.get("state") == "missing":
        node.receipt = {**node.receipt, "state": "settled",
                        "settled_by": {"kind": disposition.kind, "id": disposition.id, "what": disposition.says()}}


def suppressed_anchors(result) -> set[int]:
    """The anchors whose monitoring an in-force decision suppresses now."""
    return {int(n.anchor) for n in _nodes(result)
            if (getattr(n, "disposition", None) or {}).get("kind") == "suppressed"
            and (n.disposition or {}).get("covered")}


def of(result) -> list[Disposition]:
    """The dispositions `apply` found on this trace (`Trace.dispositions`)."""
    return list(getattr(result, "dispositions", None) or [])


# --- the tools (`agentchat disposition`, `agobserver.disposition`) -------------------------


class DispositionRefused(RuntimeError):
    """Nothing was written, and why."""


def lines(found: list[Disposition], where: tuple[str, str, int]) -> list[str]:
    out = [f"dispositions of the request #{where[2]} ({where[0]}/{where[1]}): "
           f"{sum(1 for d in found if d.state == 'in_force')} in force"]
    for d in found:
        out.append(f"  #{d.id} {d.state.upper()} — {SAYS[d.kind]} for {d.label} (a{d.unit}) up to #{d.upto}, "
                   f"by {d.name or d.by} on #{d.evidence}" + (f": {d.why}" if d.why else ""))
        if d.reversed_by:
            out.append(f"      reversed at #{d.reversed_by['id']} by {d.reversed_by['name'] or d.reversed_by['by']}"
                       + (f": {d.reversed_by['why']}" if d.reversed_by["why"] else ""))
        for row in d.new_activity:
            out.append(f"      new activity since: {row['label']} (#{row['since']}) — not covered")
        for row in d.remaining:
            out.append(f"      ended with it: {row['label']} [{row['owner'] or '?'}] — its own record: "
                       f"{row['state'].replace('_', ' ')}; {_reconcile(row)}")
    if not found:
        out.append("  none")
    return out


def _reconcile(row: dict) -> str:
    label = str(row.get("label") or "")
    if "routinerun-" in label or row.get("owner") == "Front" and "routine" in label:
        return "its owner ends it with `agrun finish`"
    if label.startswith("mission ") or label.startswith("task "):
        return "autolab's own record says otherwise until it is cancelled or accepted there"
    return "its owner's record keeps saying so; nothing more is owed for this request"


def substantive_upto(result, unit: int) -> int:
    """The newest substantive post in the scope of `unit`, as the trace read it."""
    nodes = {int(n.anchor): n for n in _nodes(result)}
    node = nodes.get(int(unit))
    if node is None:
        return 0
    return max(int(getattr(n, "last_substantive", 0) or 0) for n in [node, *_below(node)])


def _origin_of(client, message_id: int) -> tuple[str, str, int]:
    from .holds import HoldRefused, _origin_of as origin

    try:
        return origin(client, message_id)
    except HoldRefused as refused:
        raise DispositionRefused(str(refused)) from refused


def read_dispositions(client, message_id: int):
    """`(dispositions, result, where)` for the request holding the message."""
    from .trace import trace

    channel, topic, origin = _origin_of(client, message_id)
    result = trace(client, origin)
    return of(result), result, (channel, topic, origin)


def record(client, message_id: int, kind: str, *, unit: int = 0, evidence: int = 0, why: str = "",
           in_person: tuple[int, str] | None = None) -> tuple[int, Disposition | None, object, tuple]:
    """Record a disposition on the decision maker's post `evidence` (or, from
    an operator's terminal, in person). Returns `(written id, the same
    decision already in force or None, trace, where)`; 0 written when the
    same decision is already in force (a repeat converges)."""
    if kind not in KINDS:
        raise DispositionRefused(f"a disposition is one of {', '.join(KINDS)}")
    found, result, where = read_dispositions(client, int(message_id))
    if in_person is not None and not evidence:
        by, name = int(in_person[0]), str(in_person[1] or "")
    else:
        if not evidence:
            raise DispositionRefused("--evidence names the post where the decision was made; its author is who "
                                     "made it")
        post = client.message(int(evidence))
        if not post:
            raise DispositionRefused(f"#{evidence} does not exist or could not be read")
        by, name = int(post.get("sender_id") or 0), str(post.get("sender_full_name") or "")
    nodes = {int(n.anchor): n for n in result.nodes()} if result.root is not None else {}
    unit = int(unit or where[2])
    if unit not in nodes:
        raise DispositionRefused(f"#{unit} is not a unit of this request (`agentchat trace {where[2]}` lists the "
                                 "anchors)")
    same = [d for d in found if d.state == "in_force" and d.kind == kind and d.unit == unit and d.by == by
            and unit in d.covers]
    if same:
        return 0, same[0], result, where
    upto = substantive_upto(result, unit)
    written = int(client.send_to_channel(where[0], where[1],
                                         disposition_note(kind, unit, upto, by, name, evidence, why)) or 0)
    return written, None, result, where


def reverse(client, disposition_id: int, *, evidence: int = 0, why: str = "",
            in_person: tuple[int, str] | None = None) -> tuple[int, Disposition]:
    found, _, where = read_dispositions(client, int(disposition_id))
    target = next((d for d in found if d.id == int(disposition_id)), None)
    if target is None:
        raise DispositionRefused(f"#{disposition_id} is not a disposition")
    if target.state == "reversed":
        return 0, target
    if in_person is not None and not evidence:
        by, name = int(in_person[0]), str(in_person[1] or "")
    else:
        if not evidence:
            raise DispositionRefused("--evidence names the post saying the decision is undone")
        post = client.message(int(evidence))
        if not post:
            raise DispositionRefused(f"#{evidence} does not exist or could not be read")
        by, name = int(post.get("sender_id") or 0), str(post.get("sender_full_name") or "")
    written = int(client.send_to_channel(where[0], where[1], reversal_note(target.id, by, name, evidence, why)) or 0)
    return written, target


def command(client, args, out, *, home=None) -> int:
    """`agentchat disposition [<message id>] [<kind> --evidence <post> [--unit <anchor>] <why…>]`
    and `agentchat disposition <disposition id> reversed --evidence <post> <why…>`."""
    import json

    message_id = args.message_id
    if message_id is None and home is not None:
        message_id = home.anchor or client.topic_last_id(home.channel, home.topic)
    if message_id is None:
        print("disposition needs a message id: none was given and this run serves no conversation", file=out)
        return 2
    why = " ".join(args.why or []).strip()
    try:
        if args.kind is None:
            found, _, where = read_dispositions(client, int(message_id))
            if args.json:
                print(json.dumps([d.as_dict() for d in found], ensure_ascii=False, indent=1), file=out)
            else:
                print("\n".join(lines(found, where)), file=out)
            return 0
        if args.kind == "reversed":
            written, target = reverse(client, int(message_id), evidence=int(args.evidence or 0), why=why)
            print(f"#{target.id} is already reversed; nothing written" if not written
                  else f"reversed #{target.id}: #{written}", file=out)
            return 0
        written, same, result, where = record(client, int(message_id), args.kind, unit=int(args.unit or 0),
                                              evidence=int(args.evidence or 0), why=why)
    except DispositionRefused as refused:
        print(f"refused: {refused}", file=out)
        return 1
    if not written:
        print(f"already in force: #{same.id} ({same.says()}); nothing written", file=out)
        return 0
    found, _, where = read_dispositions(client, int(where[2]))
    print(f"recorded #{written} in {where[0]}/{where[1]}", file=out)
    print("\n".join(lines([d for d in found if d.id == written], where)[1:]), file=out)
    return 0
