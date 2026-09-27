"""What a post nobody acknowledged is waiting behind — one reading, shared.

`failsafe` p5 step 2. An agent's listener serves one conversation at a
time (`agag.listen`: one executor), so a post to an agent that is busy
elsewhere waits for that serving to end. progress_panel p1 showed it on the
panel (`agag.progress.queue_behind`) while Observer read the same wait as
a stall — five minutes of a healthy queue became `unacknowledged` (#13702)
and a false recovery request. This module is the reading both use.

A **wait** is one queued unit: the post (`post`), when it was made
(`queued_at` — the original time, kept whatever moves ahead of it), what it
waits behind (`ahead`), where that fact comes from (`evidence`) and when it
was observed, and a `state`:

| state | meaning | evidence |
|---|---|---|
| `behind` | the owner is busy with a healthy serving elsewhere: a legitimate wait | `confirmed` (the listener's journal and the serving's own health check, `agag.health.probe_queue`) or `conversation` (the owner has an open serving elsewhere with work within `WORK_QUIET`) |
| `blocked` | the serving ahead is not confirmed healthy — stopped, or its check establishes nothing | `confirmed` |
| `unserved` | the listener is not serving it: idle while it waits, passed over by later servings, or gave up on it | `confirmed` |
| `unknown` | nothing establishes why it waits: no check, a failed or stale check, no open serving anywhere, or a conversation-only excuse past `CONVERSATION_MAX` | — |

Only `behind` excuses a wait, and only while its evidence is current: a
confirmed check older than `CONFIRMED_FRESH` is `unknown`, and a
conversation-only excuse lapses after `CONVERSATION_MAX` from the post — an
open conversation alone is not confirmed process health.

Pure: the caller supplies the open servings (`open_servings` over traces,
or rows it built itself) and, for an owner that exposes its health, the
`probe_queue` report.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

__all__ = ["BEHIND", "BLOCKED", "UNSERVED", "UNKNOWN", "CONFIRMED_FRESH", "CONVERSATION_MAX", "WORK_QUIET",
           "QueueWait", "from_conversation", "from_probe", "open_servings"]

BEHIND, BLOCKED, UNSERVED, UNKNOWN = "behind", "blocked", "unserved", "unknown"
#: A queue check older than this excuses nothing (the panel's HEALTH_FRESH).
CONFIRMED_FRESH = 120
#: An open serving elsewhere with no sign of work for this long is not
#: evidence that the owner is busy (the panel's WORK_QUIET).
WORK_QUIET = 1800
#: A wait excused only by conversations (no check of the owner's listener)
#: stops being excused this long after the post.
CONVERSATION_MAX = 1800


@dataclass
class QueueWait:
    unit: int
    owner: str
    post: int
    queued_at: int
    state: str
    evidence: str
    observed_at: float
    why: str
    ahead: list[dict[str, Any]] = field(default_factory=list)

    @property
    def excused(self) -> bool:
        """A legitimate wait: the owner is busy with healthy work elsewhere."""
        return self.state == BEHIND

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bare(topic: str) -> str:
    return topic[2:] if topic.startswith("✔ ") else topic


def open_servings(results: Iterable, now: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Owner → the servings the conversations show open, across traces
    (`agag.trace.Trace`), with the newest sign of work in each."""
    rows: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, int]] = set()
    for result in results:
        root = getattr(result, "root", None)
        if root is None:
            continue
        for node in result.nodes():
            if node.execution != "open" or not node.owner or (node.owner, node.anchor) in seen:
                continue
            seen.add((node.owner, node.anchor))
            rows.setdefault(node.owner, []).append({
                "anchor": node.anchor, "channel": node.channel, "topic": _bare(node.topic),
                "label": node.identity or _bare(node.topic), "origin": getattr(result, "origin", ""),
                "since": node.ack_at or None, "work_at": max(int(node.work_at or 0), int(node.ack_at or 0)) or None,
                "evidence": "conversation"})
    return rows


def _post(node) -> tuple[int, int]:
    """The queued post's id and time, as the trace says it."""
    post = int(node.evidence[0]) if getattr(node, "evidence", None) else 0
    return post, int(node.last_activity or 0)


def from_conversation(node, open_by_owner: dict[str, list[dict[str, Any]]], now: int | None = None) -> QueueWait:
    """The wait as the conversations show it: behind an open serving of the
    same owner elsewhere that showed work within `WORK_QUIET`, for at most
    `CONVERSATION_MAX` after the post."""
    now = int(now if now is not None else time.time())
    post, queued_at = _post(node)
    ahead = [dict(row) for row in open_by_owner.get(node.owner, []) if row.get("anchor") != node.anchor]
    live = [row for row in ahead if row.get("work_at") and now - int(row["work_at"]) <= WORK_QUIET]
    wait = QueueWait(node.anchor, node.owner, post, queued_at, UNKNOWN, "conversation", now, "", ahead)
    if not live:
        wait.why = (f"{node.owner} shows no open serving elsewhere with recent work" if not ahead else
                    f"{node.owner}'s open serving elsewhere shows no work for {WORK_QUIET // 60} min")
        return wait
    first = max(live, key=lambda row: int(row["work_at"]))
    where = f"{first['channel']}/{first['topic']}"
    if queued_at and now - queued_at > CONVERSATION_MAX:
        wait.why = (f"queued {now - queued_at} s; {node.owner} is still serving {where}, but a wait excused only "
                    f"by conversations lapses after {CONVERSATION_MAX // 60} min")
        return wait
    wait.state = BEHIND
    wait.ahead = live
    wait.why = (f"{node.owner} serves one conversation at a time and is serving {where} ({first['label']}), with "
                f"work {now - int(first['work_at'])} s ago (conversation only)")
    return wait


def from_probe(node, report: dict[str, Any] | None, now: float | None = None) -> QueueWait:
    """The wait as the owner's own listener says it (`agag.health.probe_queue`)."""
    now = float(now if now is not None else time.time())
    post, queued_at = _post(node)
    wait = QueueWait(node.anchor, node.owner, post, queued_at, UNKNOWN, "confirmed", now, "")
    if not report:
        wait.why = "no queue check"
        return wait
    observed = float(report.get("observed_at") or 0)
    wait.observed_at = observed or now
    queue = report.get("queue") or {}
    wait.ahead = [dict(row) for row in queue.get("ahead") or ()]
    verdict = str(report.get("verdict") or "unknown")
    why = str(report.get("why") or "")
    if not observed or now - observed > CONFIRMED_FRESH:
        wait.why = f"the queue check is {int(now - observed) if observed else '?'} s old: {why}"
        return wait
    if verdict == "queued":
        wait.state, wait.why = BEHIND, why
    elif verdict == "stopped":
        wait.state, wait.why = UNSERVED, why
    elif verdict == "unknown" and wait.ahead and any(row.get("verdict") not in ("running", "waiting")
                                                     for row in wait.ahead):
        wait.state, wait.why = BLOCKED, why
    elif verdict in ("running", "waiting", "ended"):
        # Picked up: nothing waits any more; the conversation will show it.
        wait.state, wait.why = BEHIND, f"picked up: {why}"
        wait.ahead = []
    else:
        wait.why = why or "the queue check established nothing"
    return wait
