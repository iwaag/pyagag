"""What a person needs to see of a request in flight, read off its trace.

`progress_panel` p1 step 2. `agag.trace` says, per conversation, what the
records show: a state, the owner's serving (`execution`), who holds the
next move (`holder`) and the record notes. A person looking at several
requests at once needs something smaller — is it advancing, waiting, whose
move is it, do *I* have to answer — **without** those facts being merged
into one word that hides where each came from. This module is that
interpretation, shared by every reader that shows progress (the agentroom
relay's panel today; `agentchat` and Observer can read the same answer).

A **card** is one request: its origin conversation (`o<first post id>`)
and every conversation opened for it. Each conversation is a **unit**
carrying three facts kept apart:

- **work** — the trace state and the owner's record word;
- **execution** — the serving the conversation shows (`open`/`ended`/
  `unknown`), and, only where the owner exposes it (`agag.health.v1`), the
  health check of *that* serving (matched by its ack), with its observation
  time. `evidence` says which one a reader is looking at: `confirmed` (a
  fresh check), `stale` (a check older than `HEALTH_FRESH`) or
  `conversation` (no check at all — an open serving is then a claim);
- **recovery** — what the monitor (Observer) holds about it: an incident,
  a hold or a retirement, supplied by the caller.

From them one **display state** per unit, for people, from this list:

| state | when |
|---|---|
| `planning` | a plan with no task known yet, or a request served with nothing opened below |
| `queued` | a post waits for its owner's ack; a task whose turn has not come; opened and not started |
| `working` | a serving is open and — `confirmed` — a fresh check says `running`, or — `conversation` — the owner showed work within `WORK_QUIET` |
| `waiting` | on another agent or conversation, a named tool call, a notifier, a result's agreement, or an answer not yet taken up |
| `awaiting_you` | a response request to the viewer is pending, or a person holds the request |
| `answered` | the agent answered and nothing is open or asked (a plain conversation has no other end) |
| `completed` | done by record |
| `cancelled` | cancelled, replaced or retired by record |
| `stopped` | a check says `stopped`, a failure notice, or an open stop/unheld/unanswered incident |
| `unknown` | unreadable, a check that establishes nothing, nothing holding the work, or an open serving with no sign of work for `WORK_QUIET` |

Rules the whole shape rests on (step 1):

- an open serving is never "working" on its own age: without a check it is
  `conversation` evidence, and past `WORK_QUIET` with no sign of work it is
  `unknown`, not a moving meter;
- housekeeping is not work: only the check's `last_work` is shown as the
  current action;
- a delivered answer or an ended run completes nothing: completion is the
  record (`done`), and a card keeps its **stages** — task agreements, plan
  acceptance, the routine run's end, its report delivered home, a study's
  knowledge refreshed — pending until each one's record exists.

The **plan meter** counts tasks: completed / current total (tasks cancelled
by record leave the denominator), with the working and awaiting-agreement
tasks marked, and each plan revision (`[doc]`) with the total it left, so a
denominator that moved says why. Counts are not time. A **run** has no
determinate unit anywhere in the records; `determinate` stays None and the
activity is what the check (or the conversation) shows.

Pure: no reads. The caller supplies the trace, the checks and the monitor's
records, and says whether its source was live.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any, Iterable

from . import waits
from .trace import Node, Trace
from .zulip import RESOLVED_TOPIC_PREFIX

__all__ = [
    "SCHEMA", "STATES", "HEALTH_FRESH", "WORK_QUIET", "card", "card_state", "queue_behind", "unit_kind",
]

SCHEMA = "agag.progress.v1"
#: Display states, most urgent first: a card shows the first one any of its
#: units has.
STATES = ("awaiting_you", "stopped", "unknown", "working", "waiting", "queued", "planning", "answered",
          "completed", "cancelled")
#: A health check older than this is `stale`: shown, never animated.
HEALTH_FRESH = 120
#: An open serving with no sign of work (post, change note, ack) for this
#: long, and no check to say otherwise, is `unknown` (Observer's `quiet`).
WORK_QUIET = 1800
#: Record words a task is agreed with (autolab's close-out writes
#: `completed`; the requester's acceptance adds `accepted`).
AGREED = ("completed", "accepted", "done")
#: Units whose end is a record (a plain conversation's is not).
WORK_KINDS = ("plan", "task", "run", "routine_run")
#: Routines whose runs are studies: the guide archsage writes ends with the
#: sage refreshed (README_DEV, "archsage establishes studies").
STUDY_ROUTINE_PREFIX = "routine-study-"
_TASK = re.compile(r"^task (\d+)\s*#\s*(\d+)$")


def _bare(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def unit_kind(node: Node, *, root: bool = False) -> str:
    """`request`, `plan` (mission/asset/change), `task`, `run` (a forge
    asset run), `routine_run` or `conversation`."""
    if root:
        return "request"
    identity = node.identity or ""
    if identity.startswith("task "):
        return "task"
    if identity.startswith(("mission ", "asset ", "change ")):
        return "plan"
    if identity.startswith("run "):
        return "run"
    if node.channel.startswith("routine-") and _bare(node.topic).startswith("routinerun-"):
        return "routine_run"
    return "conversation"


def _serial(node: Node) -> int:
    match = _TASK.match(node.identity or "")
    return int(match.group(2)) if match else 0


def _records(node: Node, tag: str) -> list[dict]:
    return [r for r in node.records if r.get("tag") == tag]


def _latest_work(node: Node) -> int:
    return max(int(node.work_at or 0), int(node.ack_at or 0), int(node.last_activity or 0))


# --- execution -------------------------------------------------------------------


def _execution(node: Node, report: dict | None, now: int) -> dict[str, Any]:
    """The serving as the conversation shows it, and the check of that same
    serving when there is one. A report about another ack is not applied."""
    facts: dict[str, Any] = {
        "serving": node.execution, "ack": node.ack or None, "ack_at": node.ack_at or None,
        "ended_by": node.ended_by or None, "ended_at": node.ended_at or None,
        "evidence": "conversation", "health": None,
    }
    if not report:
        return facts
    subject = int(((report.get("subject") or {}).get("ack")) or 0)
    if subject and node.ack and subject != int(node.ack):
        facts["health_note"] = f"the last check was of serving #{subject}, not this one (#{node.ack})"
        return facts
    observed = float(report.get("observed_at") or 0)
    progress = report.get("progress") or {}
    wait = report.get("wait") or {}
    process = report.get("process") or {}
    age = max(0.0, now - observed) if observed else None
    facts["evidence"] = "confirmed" if age is not None and age <= HEALTH_FRESH else "stale"
    facts["health"] = {
        "verdict": str(report.get("verdict") or "unknown"),
        "why": str(report.get("why") or ""),
        "observed_at": observed or None,
        "age": round(age) if age is not None else None,
        "process": process.get("state"),
        "last_work": progress.get("last_work"),
        "last_work_at": progress.get("last_work_at"),
        "wait": ({k: wait.get(k) for k in ("kind", "name", "detail", "since", "bound_seconds", "over_bound")}
                 if wait.get("kind") not in (None, "none", "unknown") else None),
        "unknowns": list(report.get("unknowns") or []),
        "source": (report.get("source") or {}).get("host"),
        "injected": (report.get("run") or {}).get("injected"),
    }
    return facts


def _run_meter(node: Node, execution: dict, now: int) -> dict[str, Any] | None:
    """The small meter under a unit whose owner serves it: never determinate
    (no record counts units of a run), an activity band only on fresh
    evidence of work."""
    if node.execution != "open" and not execution.get("health"):
        return None
    health = execution.get("health")
    if health and execution["evidence"] == "confirmed":
        verdict = health["verdict"]
        wait = health.get("wait")
        if wait:
            action = f"{wait.get('kind')}: {wait.get('name') or ''}".strip(": ")
            if wait.get("detail"):
                action = f"{action} — {wait['detail']}"
            since = wait.get("since")
        else:
            action, since = health.get("last_work"), health.get("last_work_at")
        activity = {"running": "active", "waiting": "waiting", "stopped": "stopped",
                    "ended": "ended"}.get(verdict, "unknown")
        return {"determinate": None, "activity": activity, "action": action, "action_at": since,
                "action_age": round(now - float(since)) if since else None,
                "evidence": "confirmed", "observed_at": health.get("observed_at")}
    at = _latest_work(node)
    if node.execution == "open":
        quiet = now - at >= WORK_QUIET if at else True
        activity = "unknown" if quiet or execution["evidence"] == "stale" else "claimed"
    else:
        activity = "ended"
    return {"determinate": None, "activity": activity,
            "action": ("last sign of work" if node.execution == "open" else "last serving ended"),
            "action_at": at or None, "action_age": (now - at) if at else None,
            "evidence": execution["evidence"],
            "observed_at": (health or {}).get("observed_at")}


# --- one unit ----------------------------------------------------------------------


def _display(node: Node, kind: str, execution: dict, recovery: dict | None, *, now: int, viewer_id: int | None,
             pending: list[dict], turn_blocked_by: int, awaiting_agreement: bool,
             child_states: list[str], asked: list[dict] = ()) -> tuple[str, str, str]:
    """`(state, reason, next actor)` for one unit (module table)."""
    state = node.state
    word = node.note_state or ""
    owner = node.owner or "its owner"
    health = execution.get("health") or {}
    fresh = execution.get("evidence") == "confirmed"
    verdict = health.get("verdict") if fresh else None
    # Whoever asked for it: a task's owner writes a root note of its own (the
    # parent hop), which names itself, never its requester.
    askers = [entry.split(" #")[0] for entry in node.requested_by]
    requester = next((name for name in askers if name != node.owner), askers[0] if askers else "whoever asked")

    if state == "done":
        reason = {"finished": "the run finished and reached its goal",
                  "ended": "the run ended without reaching its goal"}.get(word, f"its record says {word or 'done'}")
        return "completed", reason, ""
    if state == "cancelled":
        return "cancelled", f"its record says {word or 'cancelled'}", ""
    if recovery and recovery.get("held"):
        return "awaiting_you", f"held by a person: {recovery.get('held_why') or 'taken over'}", "you"
    if state == "unobservable":
        return "unknown", node.detail or "the conversation could not be read", ""
    if kind == "routine_run" and node.topic.startswith(RESOLVED_TOPIC_PREFIX) and node.execution != "open":
        # A ✔ closes the conversation; only the finish block ends the run and
        # hands its report home (step 5: Front resolved the growbox run by
        # hand, `agentchat resolve --anyway`, after writing "Run complete.").
        return "unknown", ("closed with ✔ but without its end record, so whether its report reached the "
                           "request is not on record (`agrunfinish` records the end)"), owner
    if state == "failed":
        return "stopped", node.detail or "the newest word is a failure notice", requester
    if verdict == "stopped":
        return "stopped", f"health check: {health.get('why')}", requester
    if recovery and (recovery.get("open") or recovery.get("unrecovered")) \
            and recovery.get("kind") in ("stopped", "unheld", "unanswered", "failed") \
            and not (recovery.get("kind") == "unheld" and node.holder != "none"):
        # An `unheld` incident whose unit is held again reads as its work
        # does; the incident stays on the unit as its recovery record.
        return "stopped", f"Observer: {recovery.get('fact') or recovery.get('kind')}", recovery.get("responsible", "")
    if recovery and recovery.get("open") and recovery.get("kind") in ("uncertain", "quiet", "silent"):
        return "unknown", f"Observer: {recovery.get('fact') or recovery.get('kind')}", recovery.get("responsible", "")
    closed = node.topic.startswith(RESOLVED_TOPIC_PREFIX)
    if kind == "request" and pending and not closed:
        mine = [p for p in pending if viewer_id is not None and p.get("to") == viewer_id]
        if mine:
            return "awaiting_you", f"#{mine[0]['id']} asks you" + (f" ({mine[0]['ask']})" if mine[0].get("ask") else ""), "you"
        who = pending[0].get("to_name") or pending[0].get("to") or "a person"
        return "waiting", f"#{pending[0]['id']} asks {who}", str(who)
    if state == "awaiting_human" and kind != "request":
        return "awaiting_you", node.detail, "you"
    if verdict == "running":
        return "working", f"health check: {health.get('why')}", owner
    if verdict == "waiting":
        wait = health.get("wait") or {}
        what = wait.get("name") or wait.get("kind") or "a wait"
        return "waiting", f"on {what}" + (f" — {wait['detail']}" if wait.get("detail") else ""), owner
    if fresh and verdict == "unknown":
        return "unknown", f"health check: {health.get('why')}", owner
    if fresh and verdict == "ended" and node.execution == "open" and state not in ("queued",):
        # The run is over (its journal says delivered, or it was served
        # again), yet no post ended the serving here: whatever it did, its
        # conversation does not show an answer (m11741's task, 2026-09-27).
        return "unknown", (f"health check: {health.get('why')} — but no post here ended the serving acked at "
                           f"#{node.ack}"), requester
    if kind == "plan" and not child_states and node.identity.startswith("mission "):
        serving = " (its planner's serving is open)" if node.execution == "open" else ""
        return "planning", f"no task is known yet{serving}", owner
    if state == "queued":
        return "queued", node.detail, owner
    if turn_blocked_by:
        return "queued", f"after task {turn_blocked_by}", owner
    if state == "not_started":
        return "queued", node.detail or "opened, not started", owner
    if node.execution == "open":
        at = _latest_work(node)
        if not at or now - at >= WORK_QUIET:
            return "unknown", (f"a serving is open since #{node.ack} but nothing has shown work for "
                               f"{_age(now - at) if at else 'ever'}; the conversation cannot say whether it runs"), owner
        if execution.get("evidence") == "stale":
            return "unknown", f"the last health check is {_age(health.get('age') or 0)} old", owner
        return "working", "a serving is open (conversation only: not a health check)", owner
    if node.waiting_on:
        return "waiting", f"on {', '.join(node.waiting_on)}", ", ".join(node.waiting_on)
    if state == "awaiting_delivery":
        return "waiting", "the answer is not yet taken up by " + requester, requester
    if awaiting_agreement:
        return "waiting", f"a result was shown; waiting for {requester}'s agreement", requester
    if node.holder == "delegate":
        return "waiting", "on work opened from it", ""
    if node.holder == "none":
        if asked:
            # Nothing below holds it, but the request's own conversation has
            # a question to a person still open: the request waits for that
            # answer (step 5: growbox's run after Front asked for acceptance).
            who = asked[0].get("to_name") or asked[0].get("to") or "a person"
            if viewer_id is not None and asked[0].get("to") == viewer_id:
                return "awaiting_you", f"the request waits for your answer to #{asked[0]['id']}", "you"
            return "waiting", f"the request waits for {who}'s answer to #{asked[0]['id']}", str(who)
        return "unknown", "its last serving ended saying the work goes on, and nothing holds it", requester
    if kind in ("request", "conversation") and state in ("awaiting_requester", "answered", "awaiting_human"):
        # A plain conversation has no record of its own end: its agent
        # answered and nothing in it is open, asked or owed (a named answer
        # not yet taken up is `awaiting_delivery`, above). The next move is
        # whoever asked; nothing is running (Observer's `satisfied`).
        why = "closed with ✔; " if closed else ""
        return "answered", f"{why}answered; nothing is asked of anybody", ("you" if kind == "request" else requester)
    if node.holder == "owner":
        return "queued", node.detail or "its owner holds the next move", owner
    if node.holder == "requester" or state in ("awaiting_requester", "answered"):
        return "waiting", f"answered; the move is with {requester}", requester
    return "unknown", node.detail or f"holder {node.holder}", ""


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {(seconds % 3600) // 60:02d} min"


def _awaiting_agreement(node: Node) -> bool:
    """A task whose serving ended showing a result that is not agreed yet."""
    return (unit_kind(node) == "task" and node.note_state not in AGREED and node.state not in ("done", "cancelled")
            and node.execution == "ended" and node.ending_intent in ("report", "response_request"))


def _meter(plan: Node, tasks: list[dict]) -> dict[str, Any] | None:
    """completed / current total, the working and awaiting-agreement tasks,
    and each plan revision with the total it left."""
    if unit_kind(plan) != "plan" or not plan.identity.startswith("mission "):
        return None
    live = [t for t in tasks if t["display"]["state"] != "cancelled"]
    cancelled = [t for t in tasks if t["display"]["state"] == "cancelled"]
    docs = _records(plan, "doc")
    revisions = []
    for index, doc in enumerate(docs):
        until = docs[index + 1]["id"] if index + 1 < len(docs) else None
        opened = [t for t in tasks if until is None or t["anchor"] < until]
        gone = [t for t in cancelled if until is not None and (t.get("cancelled_at_id") or 0) < until]
        revisions.append({"doc": doc["id"], "at": doc["at"], "total": len(opened) - len(gone)})
    if not tasks:
        return {"known": False, "total": None, "completed": 0, "working": 0, "awaiting_agreement": 0,
                "stopped": 0, "unknown": 0, "cancelled": 0, "revisions": revisions,
                "note": "planning: no task is known yet"}
    completed = [t for t in live if t["work"]["record"] in AGREED or t["work"]["state"] == "done"]
    working = [t for t in live if t["display"]["state"] in ("working", "waiting")
               and not t["awaiting_agreement"] and t not in completed]
    stopped = [t for t in live if t["display"]["state"] == "stopped"]
    unclear = [t for t in live if t["display"]["state"] == "unknown"]
    awaiting = [t for t in live if t["awaiting_agreement"]]
    note = ""
    totals = [r["total"] for r in revisions]
    if len(set(totals + [len(live)])) > 1 and totals:
        note = f"plan revised {len(revisions) - 1}×: " + " → ".join(str(t) for t in [*totals[:-1], len(live)]) + " tasks"
    elif cancelled:
        note = f"{len(cancelled)} task(s) cancelled and left out of the total"
    elif len(revisions) > 1:
        note = f"plan revised {len(revisions) - 1}× (the total did not change)"
    return {"known": True, "total": len(live), "completed": len(completed), "working": len(working),
            "awaiting_agreement": len(awaiting), "stopped": len(stopped), "unknown": len(unclear),
            "cancelled": len(cancelled), "revisions": revisions,
            "note": note, "segments": [t["display"]["state"] if not t["awaiting_agreement"] else "awaiting_agreement"
                                       for t in sorted(live, key=lambda t: t["serial"])]}


def _unit(node: Node, *, root: bool, now: int, health: dict[int, dict], recovery: dict[int, dict],
          viewer_id: int | None, pending: list[dict], turn_blocked_by: int = 0,
          asked: list[dict] | None = None) -> dict[str, Any]:
    kind = unit_kind(node, root=root)
    execution = _execution(node, health.get(int(node.anchor)), now)
    # Children first: a plan's display depends on whether any task exists.
    children: list[dict] = []
    finished_before = True
    tasks_open = 0
    if root:
        asked = [] if node.topic.startswith(RESOLVED_TOPIC_PREFIX) else list(pending)
    ordered = sorted(node.children, key=lambda c: (_serial(c) == 0, _serial(c)))
    for child in ordered:
        blocked = 0
        if unit_kind(child) == "task":
            if not finished_before and child.state == "not_started":
                blocked = tasks_open
            if child.state not in ("done", "cancelled") and child.note_state not in AGREED:
                if finished_before:
                    tasks_open = _serial(child)
                finished_before = False
        children.append(_unit(child, root=False, now=now, health=health, recovery=recovery, viewer_id=viewer_id,
                              pending=[], turn_blocked_by=blocked, asked=asked))
    rec = recovery.get(int(node.anchor))
    awaiting = _awaiting_agreement(node)
    state, reason, next_actor = _display(
        node, kind, execution, rec, now=now, viewer_id=viewer_id, pending=pending if root else [],
        turn_blocked_by=turn_blocked_by, awaiting_agreement=awaiting,
        child_states=[c["display"]["state"] for c in children if c["kind"] == "task"], asked=asked or [])
    cancelled_at = next((r["id"] for r in reversed(_records(node, "state"))
                         if (r.get("value") or "").split()[:1] == ["cancelled"]), None)
    unit = {
        # Waiting only because something opened from it is at work: the card
        # is about that deeper unit, not this pass-through.
        "passthrough": state == "waiting" and node.holder == "delegate" and bool(children)
        and reason == "on work opened from it",
        "anchor": int(node.anchor), "kind": kind, "channel": node.channel, "topic": node.topic,
        "resolved": node.topic.startswith(RESOLVED_TOPIC_PREFIX), "label": node.identity or _bare(node.topic),
        "owner": node.owner, "serial": _serial(node),
        "work": {"state": node.state, "record": node.note_state or None, "detail": node.detail},
        "execution": execution,
        "holder": node.holder, "waiting_on": list(node.waiting_on),
        "recovery": rec,
        "display": {"state": state, "reason": reason, "next": next_actor},
        "latest_work_at": _latest_work(node) or None,
        "evidence_id": (node.evidence[-1] if node.evidence else node.anchor) or None,
        "posted_at": int(node.last_activity or 0) or None,
        "post_id": (node.evidence[0] if node.evidence else 0) or None,
        "failures": list(node.failures),
        "awaiting_agreement": awaiting,
        "cancelled_at_id": cancelled_at,
        "records": [r for r in node.records if r.get("tag") != "state"],
        "run": _run_meter(node, execution, now),
        "children": children,
    }
    report = health.get(int(node.anchor)) or {}
    if node.state == "queued" and (report.get("subject") or {}).get("queued"):
        # The owner's own listener says where the post is (failsafe p5):
        # confirmed, and never replaced by the conversation-only reading.
        wait = waits.from_probe(node, report, now)
        unit["queue"] = wait.as_dict()
        unit["display"]["reason"] = _queue_reason(unit["display"]["reason"], wait, "")
    if kind == "plan":
        unit["meter"] = _meter(node, [c for c in children if c["kind"] == "task"])
    return unit


def _queue_reason(base: str, wait: waits.QueueWait, elsewhere: str) -> str:
    what = {"behind": "", "blocked": "blocked: ", "unserved": "not being served: ",
            "unknown": "why it waits is not established: "}[wait.state]
    return f"{base}; {what}{wait.why}{elsewhere}" if base else f"{what}{wait.why}{elsewhere}"


# --- the card ----------------------------------------------------------------------


def _walk(unit: dict) -> Iterable[dict]:
    yield unit
    for child in unit["children"]:
        yield from _walk(child)


def _named(value: str) -> tuple[str, str]:
    """`(channel, bare topic)` of a note value `<channel>/<topic>[ #<anchor>]`."""
    text = re.sub(r"\s+#\d+\s*$", "", str(value or "").strip())
    channel, _, topic = text.partition("/")
    return channel.strip(), _bare(topic.strip())


def card_state(units: Iterable[dict]) -> str:
    """The most urgent display state any unit has (`STATES` order), with
    `completed` only when every unit is finished and the request answered."""
    found = {u["display"]["state"] for u in units}
    for state in STATES:
        if state in found:
            return state
    return "unknown"


def _field(value: str, name: str) -> str:
    match = re.search(rf"(?:^|\s){name}=(\S+)", value or "")
    return match.group(1) if match else ""


_ACCEPTED_MAIN = re.compile(r"^accepted\b.*?\bmain=([0-9a-f]{7,40})")


def _integrated(plans: list[dict]) -> set[str]:
    """The `main` commits the plans' tasks integrated (each close-out's
    `[change] accepted main=<commit>`): the result a refresh must hold."""
    found: set[str] = set()
    for plan in plans:
        for task in _walk(plan):
            for record in task["records"]:
                match = _ACCEPTED_MAIN.match(str(record.get("value") or "")) if record.get("tag") == "change" else None
                if match:
                    found.add(match.group(1))
    return found


def _same_commit(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a)) and min(len(a), len(b)) >= 7


def _refresh_holds(sync: dict, required: set[str], mine: set[tuple[str, str]], here: bool) -> bool:
    """Whether a `sagesync` record establishes this request's refresh:

    - `missing=` one of the required commits: no;
    - `for=` one of this request's conversations: yes if it `includes=` the
      required commit (or is that revision), or when no commit is known;
    - elsewhere (another request's refresh): only if its `includes=` or its
      revision *is* a required commit — the recorded revision establishes
      the fact, the time does not;
    - an older record with neither relation: its revision being a required
      commit, or — when no commit is known — being recorded in this
      request's own conversations."""
    value = str(sync.get("value") or "")
    parts = value.split()
    revision = parts[1] if len(parts) > 1 else ""
    included, missing = _field(value, "includes"), _field(value, "missing")
    if missing and any(_same_commit(missing, c) for c in required):
        return False
    holds = any(_same_commit(included, c) or _same_commit(revision, c) for c in required)
    target = _field(value, "for")
    if target:
        channel, _, rest = target.partition("/")
        topic = rest.split("#", 1)[0]
        if (channel, _bare(topic)) in mine:
            return holds or not required
    if included or target:
        return holds
    return holds if required else here


def _stages(root: dict, now: int, syncs_elsewhere: list[dict] | None = None) -> list[dict]:
    """What must still be recorded before the request is complete, one
    line each, in the order it happens: every plan's agreements and its
    acceptance, every routine run's end and its report delivered home, and
    — for a study routine — the sage refreshed after the research."""
    stages: list[dict] = []
    units = list(_walk(root))
    delivered_runs = {r["value"] for r in root["records"] if r["tag"] == "delivered"}
    syncs = [r for u in units for r in u["records"] if r["tag"] == "sagesync"]
    # A refresh is a fact about the study's sage, wherever it was recorded:
    # a routine guide that sends every run to the same fixed topic has its
    # answer land in the first request that used it (step 5, growbox).
    seen = {r["id"] for r in syncs}
    syncs += [r for r in syncs_elsewhere or () if r.get("id") not in seen]
    for unit in units:
        if unit["kind"] == "plan" and unit.get("meter") and unit["work"]["state"] != "cancelled":
            # A cancelled plan owes no agreement and no acceptance: its
            # record ends it.
            meter = unit["meter"]
            if meter["known"] and meter["total"]:
                stages.append({"stage": "tasks_agreed", "unit": unit["anchor"], "label": unit["label"],
                               "status": "done" if meter["completed"] >= meter["total"] else "pending",
                               "detail": f"{meter['completed']}/{meter['total']} task(s) agreed"})
            accepted = next((r for r in unit["records"] if r["tag"] == "acceptance"), None)
            # The record decides, not the conversation's state: a done mission
            # whose owner's last word still waits for delivery reads
            # `awaiting_delivery` (step 5's repeat, m13665).
            recorded = unit["work"]["state"] == "done" or (unit["work"]["record"] or "") in ("done", "accepted")
            stages.append({"stage": "plan_accepted", "unit": unit["anchor"], "label": unit["label"],
                           "status": "done" if recorded else "pending",
                           "evidence": accepted["id"] if accepted else None,
                           "detail": (f"accepted: {accepted['value']}" if accepted else
                                      "done" if recorded else "not accepted yet")})
        if unit["kind"] == "routine_run":
            finished = unit["work"]["state"] == "done"
            stages.append({"stage": "run_ended", "unit": unit["anchor"], "label": _bare(unit["topic"]),
                           "status": "done" if finished else "pending",
                           "detail": unit["display"]["reason"] if finished else "no finish record yet"})
            name = (unit["channel"], _bare(unit["topic"]))
            home = any(_named(value) == name for value in delivered_runs)
            stages.append({"stage": "report_delivered", "unit": unit["anchor"], "label": _bare(unit["topic"]),
                           "status": "done" if home else "pending",
                           "detail": "the run's report is in the request's conversation" if home
                           else "the run's report has not been delivered home"})
            if unit["channel"].startswith(STUDY_ROUTINE_PREFIX):
                # The run's own plans; a plan opened for the request beside the
                # run instead of from it (step 5's repeat: Front's desk serving
                # opened the workplan itself) is still this study's research.
                plans = [u for u in _walk(unit) if u["kind"] == "plan"] \
                    or [u for u in units if u["kind"] == "plan"]
                research_done = max((r["at"] for u in plans for r in u["records"] if r["tag"] == "acceptance"),
                                    default=0)
                required = _integrated(plans)
                mine = {(u["channel"], _bare(u["topic"])) for u in units}
                in_tree = {r["id"] for u in units for r in u["records"] if r["tag"] == "sagesync"}
                # failsafe p5: a refresh counts by what it records — whom it was
                # for and which result it holds — never by the study's project
                # and a time alone, which let another run's refresh complete
                # this one (and the first trial's refresh the second's).
                after = [s for s in syncs if research_done and s["at"] >= research_done
                         and _refresh_holds(s, required, mine, s["id"] in in_tree)]
                stages.append({"stage": "knowledge_refreshed", "unit": unit["anchor"],
                               "label": unit["channel"][len("routine-"):],
                               "status": "done" if after else "pending",
                               "evidence": after[-1]["id"] if after else None,
                               "required": sorted(required) or None,
                               "detail": (f"sage synced: {after[-1]['value']}" if after else
                                          ("no sage refresh holding " + ", ".join(sorted(c[:12] for c in required))
                                           + " is recorded for this request" if required else
                                           "no sage refresh is recorded for this request")
                                          if research_done else "the research is not accepted yet")})
    order = ("tasks_agreed", "plan_accepted", "run_ended", "report_delivered", "knowledge_refreshed")
    return sorted(stages, key=lambda s: order.index(s["stage"]))


def card(result: Trace, *, now: int | None = None, health: dict[int, dict] | None = None,
         recovery: dict[int, dict] | None = None, viewer_id: int | None = None, pending: list[dict] | None = None,
         source_live: bool = True, source_note: str = "", syncs: list[dict] | None = None) -> dict[str, Any]:
    """One request's card (module doc). `health` and `recovery` are keyed by
    unit anchor; `pending` are the response requests still waiting in the
    origin (`agag.outstanding`, as dicts with `id`, `to`, `to_name`, `ask`)."""
    now = int(now if now is not None else time.time())
    if result.root is None:
        return {"schema": SCHEMA, "origin": result.origin, "observed_at": result.observed_at,
                "state": "unknown", "reason": result.problem or "nothing could be traced", "root": None,
                "stages": [], "stale": not source_live}
    root = _unit(result.root, root=True, now=now, health=health or {}, recovery=recovery or {},
                 viewer_id=viewer_id, pending=list(pending or []))
    units = list(_walk(root))
    stages = _stages(root, now, syncs)
    deciding = [u for u in units if not u.get("passthrough")] or units
    state = card_state(deciding)
    if state in ("answered", "completed") and any(s["status"] == "pending" for s in stages):
        # A delivered answer or an ended run completes nothing on its own.
        pending_stage = next(s for s in stages if s["status"] == "pending")
        state, focus = "waiting", None
        reason, next_actor = f"not complete: {pending_stage['detail']} ({pending_stage['label']})", ""
    else:
        focus = _focus(root, deciding, state)
        if state == "waiting" and pending and root["display"]["state"] == "waiting" \
                and not root["topic"].startswith(RESOLVED_TOPIC_PREFIX):
            # A question to a person in the request's own conversation is what
            # the request waits for, before anything deeper (step 5: "the answer
            # is not yet taken up by Front" hid "#13477 asks Omni Agent").
            focus = root
        reason = focus["display"]["reason"] if focus else ""
        next_actor = focus["display"]["next"] if focus else ""
    work = [u for u in units if u["kind"] in WORK_KINDS]
    if state == "answered" and work and all(u["display"]["state"] in ("completed", "cancelled") for u in work) \
            and not any(s["status"] == "pending" for s in stages):
        # Every unit of work is finished by record, every stage is recorded,
        # and the conversations around them are answered.
        if all(u["display"]["state"] == "cancelled" for u in work):
            state, reason = "cancelled", "every unit of work was cancelled by its record"
        else:
            state = "completed"
            reason = "every unit of work is finished by its record"
        next_actor = ""
    latest = max((u["latest_work_at"] or 0 for u in units), default=0) or None
    if not source_live:
        reason = f"last known ({source_note or 'the source is not live'}): {reason}"
    return {
        "schema": SCHEMA, "origin": result.origin, "anchor": root["anchor"], "observed_at": result.observed_at,
        "state": state, "reason": reason, "next": next_actor,
        "focus": focus["anchor"] if focus else None,
        "latest_work_at": latest, "stale": not source_live, "problem": result.problem,
        "stages": stages, "root": root,
        "counts": {s: sum(1 for u in units if u["display"]["state"] == s) for s in STATES},
    }


def queue_behind(cards: list[dict], now: int | None = None) -> list[dict]:
    """Say what a queued post waits behind, across requests.

    An agent's listener serves one conversation at a time (`agag.listen`:
    one executor), so a post its owner has not acknowledged while that owner
    has a serving open elsewhere waits for that serving — the difference
    between two requests being *concurrent* and their work *executing*
    concurrently. The reading is `agag.waits`, the one Observer uses: a
    unit whose owner's listener was checked (`queue` already set by `card`)
    keeps that confirmed answer; every other queued unit gets the
    conversation-only reading over the open servings in all these cards.
    Its reason says so; the card's reason follows when that unit is its
    focus. Mutates and returns `cards`."""
    now = int(now if now is not None else time.time())
    open_by_owner: dict[str, list[dict]] = {}
    for found in cards:
        if not found.get("root"):
            continue
        for unit in _walk(found["root"]):
            # Only a serving with evidence of being served: a stale or dead
            # open serving (`unknown`, `stopped`) is not what the agent is doing now.
            if unit["execution"]["serving"] == "open" and unit["owner"] and unit["display"]["state"] in (
                    "working", "waiting", "planning"):
                open_by_owner.setdefault(unit["owner"], []).append({
                    "anchor": unit["anchor"], "channel": unit["channel"], "topic": _bare(unit["topic"]),
                    "label": unit["label"], "origin": found["origin"], "request": found.get("topic") or "",
                    "since": unit["execution"].get("ack_at"),
                    "work_at": unit.get("latest_work_at") or unit["execution"].get("ack_at"),
                    "evidence": unit["execution"]["evidence"]})
    for found in cards:
        if not found.get("root"):
            continue
        for unit in _walk(found["root"]):
            # Only a post waiting for its owner's ack: a task not started yet
            # waits for its start, not for the owner's executor (trial C
            # showed a task "behind" the other request's serving).
            if unit["display"]["state"] != "queued" or unit["work"]["state"] != "queued" \
                    or "queue" in unit or not unit["owner"]:
                continue
            node = SimpleNamespace(anchor=unit["anchor"], owner=unit["owner"], last_activity=unit.get("posted_at") or 0,
                                   evidence=[unit["post_id"]] if unit.get("post_id") else [])
            wait = waits.from_conversation(node, open_by_owner, now)
            if wait.state == waits.UNKNOWN and not wait.ahead:
                continue  # nothing is open anywhere: the plain "queued" stands
            unit["queue"] = wait.as_dict()
            first = wait.ahead[0] if wait.ahead else None
            elsewhere = f" of another request ({_bare(first['request'])})" \
                if first and first.get("origin") != found["origin"] and first.get("request") else ""
            unit["display"]["reason"] = _queue_reason(unit["display"]["reason"], wait, elsewhere)
            if found.get("focus") == unit["anchor"]:
                found["reason"] = unit["display"]["reason"]
    return cards


def _focus(root: dict, units: list[dict], state: str) -> dict | None:
    """The unit that makes the card's state: the deepest one in that state."""
    depth: dict[int, int] = {}

    def measure(unit: dict, level: int) -> None:
        depth[id(unit)] = level
        for child in unit["children"]:
            measure(child, level + 1)

    measure(root, 0)
    matching = [u for u in units if u["display"]["state"] == state]
    return max(matching, key=lambda u: depth.get(id(u), 0)) if matching else None
