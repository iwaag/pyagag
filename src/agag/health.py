"""Is this serving's work still being done? A bounded look, with its unknowns.

`failsafe` p2 step 2. The health interface an owner exposes to a monitor.
The monitor names a serving the way the conversation shows it — the ack
that opened it, and its conversation — and gets back **facts with their
observation times and sources**, a verdict derived from them, and what
could not be established (`agag.health.v1`):

| fact | from |
|---|---|
| `process` — `alive`, `exited` or `unknown` | the live execution record's pid (`agag.execution`), checked in the process table, and the end the runner recorded |
| `progress` — the last harness event, its age and what it was | the live record |
| `wait` — `tool` (a call that has not returned), `children` (processes under the harness), `none` or `unknown` | the live record's open tool calls; the process table under the pid |
| `serving` — the listener's journal stage for that ack, and whether the conversation is queued or running again | the listener's queue file, read-only |
| `queue` — for a post not yet acknowledged: the conversation's entry, what the executor runs meanwhile (and that serving's own health), how many servings began since | the listener's queue file, read-only; the live records of the serving ahead |

The verdict:

- `running`: the process is alive and **work** arrived within `window` — a
  tool call starting or returning, the model's text or partial output, a
  subagent's events (failsafe p3). Housekeeping (`system` events, pings)
  is reported with its age and never makes a run `running`;
- `waiting`: alive, no recent work, and a named tool call or child process
  explains the wait — a tool call only **inside its own declared bound**
  (Claude Code's Bash `timeout`, `TOOL_GRACE` added) when it declares one,
  and inside the run's own deadline. The wait reports the CPU time of the
  processes under the run (`cpu_seconds`), so a monitor comparing two
  looks can tell a wait that advances from one that is only named;
- `stopped`: the work is not being done and nothing is queued to do it —
  the harness process is gone with no end recorded, or it ended and its
  serving delivered nothing; confirmed, not guessed;
- `ended`: the run ended and its serving delivered (or is delivering) a
  reply: nothing is running, which is the conversation's business;
- `unknown`: anything else, with `unknowns` saying why.

**A post nobody acknowledged yet** (failsafe p5) is asked about with
`queued=True` (`--queued`, no ack): the listener serves one conversation at
a time, so a post can wait behind another request's healthy task for as
long as that task runs. `probe_queue` reads the conversation's entry in the
listener's queue and what the executor runs meanwhile, and answers:

- `queued`: the entry waits and the serving ahead of it is healthy
  (`running`/`waiting`, checked the same way, `queue.ahead`);
- `stopped`: the entry waits while the executor runs **nothing** for
  `PICKUP_SECONDS` (an idle listener that has not picked it up), it was
  passed over by `PASSED_OVER` servings begun after it, or the listener
  gave up on it (`failed`);
- `running`/`waiting`/…: it was picked up — the verdict of that serving;
- `unknown`: the serving ahead cannot be confirmed healthy (it is
  `stopped` or `unknown` itself, said in `queue.ahead`), the entry is not
  in the queue at all, or the file cannot be read.

The original queued time (`queue.enqueued_at`) and the serving ahead are
facts of each look; a reader that compares looks sees the work ahead change.

**What this does not claim.** A live listener or a heartbeat is not task
health: an alive process that has sent nothing for longer than `window` and
has no named wait is `unknown`, not `running`. A lack of output is not a
failure. Evidence from another serving is never applied: a record for a
different ack is reported as such and concludes nothing about this one.

**Bounded.** One process-table read (`ps`, `PS_TIMEOUT` seconds) and one
read-only query of the queue file (`QUEUE_TIMEOUT`). Anything that fails
or times out becomes an unknown; the probe itself never raises.

Harness specifics stay behind this interface: the live record is written by
`run_harness` from each harness's own events, and only `claude_code` and
`agcode` streams say when a tool call returns (`execution.TOOL_RESULTS`).

`python -m agag.health --dir <executions> [--queue <listener.sqlite>] --ack
<id> [--channel … --topic …] [--window 120]` prints one JSON document —
the form a monitor in another process calls with its own timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .execution import records as execution_records

SCHEMA = "agag.health.v1"
#: The default: no harness event for this long is not progress any more.
WINDOW_SECONDS = 120.0
PS_TIMEOUT = 5.0
QUEUE_TIMEOUT = 2.0
#: A pid whose process started this much after the record's start is
#: another process that reused the number.
PID_REUSE_SLACK = 10.0
MAX_CHILDREN = 8
#: Past a tool call's own declared bound, this long before the wait is no
#: longer explained: the harness kills the call at its bound, so a call
#: still open after it is not the call doing its work.
TOOL_GRACE = 60.0

#: A queued conversation while the executor runs nothing, this long after
#: it became eligible: the listener is not picking it up (failsafe p5). A
#: healthy executor takes an eligible entry within a second.
PICKUP_SECONDS = 90.0
#: Servings of other conversations begun after this one was queued: past
#: this many it has been passed over while the listener served others.
PASSED_OVER = 3

RUNNING, WAITING, STOPPED, ENDED, UNKNOWN, QUEUED = "running", "waiting", "stopped", "ended", "unknown", "queued"
VERDICTS = (RUNNING, WAITING, STOPPED, ENDED, UNKNOWN, QUEUED)
HEALTHY = (RUNNING, WAITING)

__all__ = ["SCHEMA", "VERDICTS", "HEALTHY", "probe", "probe_queue", "queue_state", "main"]


def _bare(topic: str) -> str:
    return topic[2:] if topic.startswith("✔ ") else topic


def _etime_seconds(text: str) -> float | None:
    """`ps -o etime` ([[dd-]hh:]mm:ss) as seconds."""
    try:
        days, _, rest = text.strip().rpartition("-")
        parts = [int(p) for p in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def _cpu_seconds(text: str) -> float | None:
    """`ps -o time` ([[dd-]hh:]mm:ss[.cc]) as seconds."""
    try:
        days, _, rest = text.strip().rpartition("-")
        parts = [float(p) for p in rest.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0.0)
        return int(days or 0) * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
    except ValueError:
        return None


def process_table(timeout: float = PS_TIMEOUT) -> dict[int, dict[str, Any]] | None:
    """`pid → {ppid, age, cpu, command}` for every process, or None."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,time=,command="], capture_output=True,
                             text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table: dict[int, dict[str, Any]] = {}
    for line in out.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        table[pid] = {"ppid": ppid, "age": _etime_seconds(fields[2]), "cpu": _cpu_seconds(fields[3]),
                      "command": fields[4] if len(fields) > 4 else ""}
    return table or None


def descendants(table: dict[int, dict[str, Any]], pid: int) -> list[dict[str, Any]]:
    children: dict[int, list[int]] = {}
    for child, row in table.items():
        children.setdefault(int(row["ppid"]), []).append(child)
    found, frontier = [], [pid]
    while frontier:
        following = []
        for parent in frontier:
            for child in sorted(children.get(parent, [])):
                found.append({"pid": child, "age_seconds": table[child]["age"], "cpu_seconds": table[child].get("cpu"),
                              "command": " ".join(str(table[child]["command"]).split())[:160]})
                following.append(child)
        frontier = following
    return found


def serving_stage(queue: Path, ack: int, channel: str, topic: str,
                  timeout: float = QUEUE_TIMEOUT) -> dict[str, Any] | None:
    """The journal row of the serving acked by `ack`, and whether its
    conversation is queued or running now — or None when it cannot be read."""
    try:
        db = sqlite3.connect(f"file:{queue}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    try:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT id, state, ack_id, delivered_id, failure, updated_at, channel, topic, route "
                         "FROM servings WHERE ack_id = ? ORDER BY id DESC LIMIT 1", (int(ack),)).fetchone() if ack else None
        where = (row["channel"], row["topic"]) if row is not None else (channel, topic)
        pending = [dict(r) for r in db.execute("SELECT state, route FROM pending WHERE channel = ? AND topic IN (?, ?)",
                                               (where[0], _bare(where[1]), f"✔ {_bare(where[1])}")).fetchall()]
        later = db.execute("SELECT COUNT(*) AS n FROM servings WHERE channel = ? AND topic = ? AND id > ?",
                           (where[0], where[1], int(row["id"]))).fetchone()["n"] if row is not None else 0
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return {
        "found": row is not None,
        "id": row["id"] if row is not None else None,
        "state": row["state"] if row is not None else None,
        "delivered_id": row["delivered_id"] if row is not None else None,
        "failure": (row["failure"] or "")[:200] if row is not None else "",
        "updated_at": row["updated_at"] if row is not None else None,
        "queued": [p["state"] for p in pending],
        "later_servings": int(later),
    }


_OPEN_SERVING = ("received", "acked", "executed")


def queue_state(queue: Path, channel: str, topic: str, timeout: float = QUEUE_TIMEOUT) -> dict[str, Any] | None:
    """The conversation's entry in the listener's queue, what the executor
    runs meanwhile, and how many servings began since it was queued — or
    None when the file cannot be read (failsafe p5)."""
    try:
        db = sqlite3.connect(f"file:{queue}?mode=ro", uri=True, timeout=timeout)
    except sqlite3.Error:
        return None
    try:
        db.row_factory = sqlite3.Row
        names = (_bare(topic), f"✔ {_bare(topic)}")
        rows = [dict(r) for r in db.execute("SELECT channel, topic, route, state, message_id, enqueued_at, started_at,"
                                           " attempts, next_at, failure FROM pending ORDER BY enqueued_at")]
        mine = next((r for r in rows if r["channel"] == channel and r["topic"] in names), None)
        running = []
        for row in rows:
            if row["state"] != "running":
                continue
            serving = db.execute("SELECT id, state, ack_id, started_at, home_channel, home_topic FROM servings "
                                 "WHERE channel = ? AND topic = ? AND route = ? ORDER BY id DESC LIMIT 1",
                                 (row["channel"], row["topic"], row["route"])).fetchone()
            running.append({"channel": row["channel"], "topic": row["topic"], "route": row["route"],
                            "started_at": row["started_at"],
                            "serving": dict(serving) if serving is not None and serving["state"] in _OPEN_SERVING
                            else None})
        since = float(mine["enqueued_at"]) if mine is not None else None
        began = db.execute("SELECT channel, topic, started_at FROM servings WHERE started_at > ? "
                           "ORDER BY id", (since or 0.0,)).fetchall() if since is not None else []
        latest = db.execute("SELECT id, state, ack_id, started_at FROM servings WHERE channel = ? AND topic IN (?, ?) "
                            "ORDER BY id DESC LIMIT 1", (channel, *names)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    eligible = [r for r in rows if r["state"] == "pending"]
    return {
        "entry": mine,
        "position": next((i + 1 for i, r in enumerate(eligible) if r is mine), None),
        "waiting": len(eligible),
        "running": running,
        "served_since": sum(1 for r in began if not (r["channel"] == channel and r["topic"] in names)),
        "latest_serving": dict(latest) if latest is not None else None,
    }


def probe_queue(directory: Path, *, channel: str, topic: str, queue: Path | None, since: float = 0.0,
                window: float = WINDOW_SECONDS, now: float | None = None, table=None) -> dict[str, Any]:
    """Whether a post nobody acknowledged is waiting its turn (module doc):
    `queued` behind a healthy serving, `stopped` when the listener is not
    serving it, the picked-up serving's own verdict, or `unknown`. `since` is
    when the post was made. Never raises."""
    now = float(now if now is not None else time.time())
    report: dict[str, Any] = {
        "schema": SCHEMA, "observed_at": now, "subject": {"ack": 0, "channel": channel, "topic": topic,
                                                           "queued": True, "since": since or None},
        "source": {"records": str(directory), "queue": str(queue) if queue else None, "host": os.uname().nodename},
        "process": {"state": UNKNOWN}, "progress": {}, "wait": {"kind": UNKNOWN}, "serving": None,
        "verdict": UNKNOWN, "why": "", "unknowns": [],
    }
    if queue is None:
        report["why"] = "no listener queue to read"
        report["unknowns"].append("the listener's queue")
        return report
    try:
        state = queue_state(queue, channel, topic)
    except Exception as error:  # noqa: BLE001 - a probe answers, it does not raise
        state = None
        report["unknowns"].append(f"the queue could not be read: {error!r}")
    if state is None:
        report["why"] = "the listener's queue could not be read"
        report["unknowns"].append("the listener's queue")
        return report
    entry = state["entry"]
    latest = state["latest_serving"]
    facts = {"entry": entry, "enqueued_at": entry["enqueued_at"] if entry else None, "position": state["position"],
             "waiting": state["waiting"], "served_since": state["served_since"], "ahead": []}
    report["queue"] = facts
    # A serving begun after the post read it: its turn came, whatever was
    # queued for the conversation since.
    picked = latest is not None and float(latest.get("started_at") or 0) >= float(since or 0)
    if picked:
        # Its turn came: what that serving is doing is the answer.
        served = probe(directory, ack=int(latest.get("ack_id") or 0), channel=channel, topic=topic, queue=queue,
                       window=window, now=now, table=table)
        served["subject"] = {**served.get("subject", {}), "queued": True, "since": since or None}
        served["queue"] = facts
        if not latest.get("ack_id"):
            served["verdict"], served["why"] = RUNNING, (
                f"picked up {int(now - float(latest['started_at']))} s ago; its acknowledgement is being posted")
        return served
    if entry is None:
        report["why"] = ("the post is not in the listener's queue and no serving of its conversation began after "
                         "it: the listener did not take it in, or has not yet")
        report["unknowns"].append("whether the listener saw the post")
        return report
    if entry["state"] == "failed":
        report["verdict"], report["why"] = STOPPED, (
            f"the listener gave up on this conversation: {str(entry.get('failure') or 'no reason recorded')[:200]}")
        return report
    queued_for = int(now - float(entry["enqueued_at"]))
    if state["served_since"] >= PASSED_OVER:
        report["verdict"], report["why"] = STOPPED, (
            f"queued {queued_for} s and passed over: {state['served_since']} serving(s) of other conversations "
            f"began after it")
        return report
    running = state["running"]
    if not running:
        eligible_at = max(float(entry["enqueued_at"]), float(entry.get("next_at") or 0))
        idle = now - eligible_at
        if idle >= PICKUP_SECONDS:
            report["verdict"], report["why"] = STOPPED, (
                f"queued {queued_for} s while the listener's executor runs nothing: it has not picked the "
                f"conversation up for {int(idle)} s")
        else:
            report["why"] = f"queued {queued_for} s; the executor is idle and should take it within seconds"
            report["unknowns"].append("whether the executor will pick it up")
        return report
    for ahead in running:
        serving = ahead.get("serving") or {}
        row = {"channel": ahead["channel"], "topic": ahead["topic"], "route": ahead["route"],
               "started_at": ahead["started_at"], "ack": serving.get("ack_id"), "journal": serving.get("state")}
        if serving.get("ack_id"):
            checked = probe(directory, ack=int(serving["ack_id"]), channel=ahead["channel"], topic=ahead["topic"],
                            queue=None, window=window, now=now, table=table)
            row.update(verdict=checked.get("verdict"), why=str(checked.get("why") or "")[:300],
                       last_work=(checked.get("progress") or {}).get("last_work"),
                       last_work_at=(checked.get("progress") or {}).get("last_work_at"))
        elif ahead.get("started_at") and now - float(ahead["started_at"]) <= window:
            row.update(verdict=RUNNING, why="just picked up; its acknowledgement is being posted")
        else:
            row.update(verdict=UNKNOWN, why="the serving ahead has no acknowledgement and no record to check")
        facts["ahead"].append(row)
    first = facts["ahead"][0]
    where = f"{first['channel']}/{_bare(first['topic'])}"
    if all(row.get("verdict") in HEALTHY for row in facts["ahead"]):
        report["verdict"], report["why"] = QUEUED, (
            f"queued {queued_for} s (position {state['position'] or '?'} of {state['waiting']}) behind "
            f"{where}, whose serving is {first['verdict']}: {first.get('why') or ''}")
        report["wait"] = {"kind": "queue", "name": where, "since": entry["enqueued_at"]}
    else:
        report["why"] = (f"queued {queued_for} s behind {where}, whose serving is {first.get('verdict')}: "
                         f"{first.get('why') or ''}")
        report["unknowns"].append("whether the serving ahead is still being done")
    return report


#: Beside a live record, a trial fault a person injected into that run
#: (`{"fault": …, "at": …}`), written by the owner's fault hook (failsafe p3).
INJECTED_SUFFIX = ".injected"


def _injected(path: Path) -> dict | None:
    try:
        doc = json.loads(path.with_suffix(INJECTED_SUFFIX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _matching(directory: Path, ack: int, channel: str, topic: str) -> tuple[dict | None, list[dict]]:
    """The record of the serving acked by `ack`, and the records of the same
    conversation that belong to other servings (never applied)."""
    mine, others = None, []
    for path, doc in execution_records(directory):
        serving = doc.get("serving") or {}
        if ack and int(serving.get("ack") or 0) == int(ack):
            if mine is None:
                mine = {**doc, "injected": _injected(path)}
            continue
        if channel and serving.get("channel") == channel and _bare(str(serving.get("topic") or "")) == _bare(topic):
            others.append(doc)
    return mine, others


def probe(directory: Path, *, ack: int = 0, channel: str = "", topic: str = "", queue: Path | None = None,
          window: float = WINDOW_SECONDS, now: float | None = None, table=None) -> dict[str, Any]:
    """The health of the serving acked by `ack` (module doc). Never raises."""
    now = float(now if now is not None else time.time())
    report: dict[str, Any] = {
        "schema": SCHEMA, "observed_at": now, "subject": {"ack": int(ack or 0), "channel": channel, "topic": topic},
        "source": {"records": str(directory), "queue": str(queue) if queue else None, "host": os.uname().nodename},
        "process": {"state": UNKNOWN}, "progress": {}, "wait": {"kind": UNKNOWN}, "serving": None,
        "verdict": UNKNOWN, "why": "", "unknowns": [],
    }
    unknowns: list[str] = report["unknowns"]
    try:
        record, others = _matching(Path(directory), int(ack or 0), channel, topic)
    except Exception as error:  # noqa: BLE001 - a probe answers, it does not raise
        record, others = None, []
        unknowns.append(f"the execution records could not be read: {error!r}")
    stage = serving_stage(queue, int(ack or 0), channel, topic) if queue is not None else None
    report["serving"] = stage
    if queue is not None and stage is None:
        unknowns.append("the listener's serving journal could not be read")
    if record is None:
        newer = [d for d in others if (d.get("serving") or {}).get("ack")]
        if newer:
            report["other_servings"] = [{"ack": (d.get("serving") or {}).get("ack"), "started_at": d.get("started_at"),
                                         "ended_at": d.get("ended_at")} for d in newer[:3]]
            unknowns.append("the execution records of this conversation belong to other servings; none is applied "
                            "to this one")
        else:
            unknowns.append("no execution record for this serving (the run kept none, or has not started)")
        report["why"] = "nothing on record says what this serving's run is doing"
        if stage is not None and stage.get("state") == "delivered" and stage.get("delivered_id"):
            report["verdict"] = ENDED
            report["why"] = f"its serving delivered its reply #{stage['delivered_id']} (no execution record)"
        if stage is not None and stage.get("queued"):
            report["why"] += f"; its conversation is {', '.join(stage['queued'])} in the listener's queue"
        return report
    report["run"] = {"harness": record.get("harness"), "pid": record.get("pid"), "started_at": record.get("started_at"),
                     "deadline_at": record.get("deadline_at"), "ended_at": record.get("ended_at"),
                     "exit_code": record.get("exit_code"), "outcome": record.get("outcome"),
                     "record_written_at": record.get("written_at"), "role": (record.get("serving") or {}).get("role"),
                     "injected": record.get("injected")}
    last_event = record.get("last_event_at")
    # Work, not any event (failsafe p3). A record from before the split has
    # no `last_work_at`: its events are all it has.
    last_work = record.get("last_work_at", last_event)
    kept = record.get("housekeeping") or {}
    report["progress"] = {"last_work_at": last_work, "work_age_seconds": round(now - last_work, 1) if last_work else None,
                          "last_work": record.get("last_work", record.get("last_event")),
                          "last_event_at": last_event, "age_seconds": round(now - last_event, 1) if last_event else None,
                          "last_event": record.get("last_event"), "events": record.get("events"),
                          "housekeeping": {"count": kept.get("count"), "last": kept.get("last"),
                                           "age_seconds": round(now - kept["last_at"], 1) if kept.get("last_at") else None},
                          "tools_done": record.get("tools_done")}

    # --- the process -------------------------------------------------------------------
    pid = int(record.get("pid") or 0)
    table = table if table is not None else (process_table() if pid and not record.get("ended_at") else {})
    if record.get("ended_at"):
        report["process"] = {"state": "exited", "observed_at": now, "how": "its runner recorded the end",
                             "ended_at": record.get("ended_at"), "exit_code": record.get("exit_code")}
    elif not pid:
        unknowns.append("the record names no process")
    elif table is None:
        unknowns.append("the process table could not be read")
    else:
        row = table.get(pid)
        started = float(record.get("started_at") or 0)
        reused = row is not None and row.get("age") is not None and started \
            and now - float(row["age"]) > started + PID_REUSE_SLACK
        if row is None or reused:
            report["process"] = {"state": "exited", "observed_at": now, "pid": pid,
                                 "how": "not in the process table" if row is None else "its pid now belongs to a "
                                                                                          "later process",
                                 "ended_at": None}
        else:
            report["process"] = {"state": "alive", "observed_at": now, "pid": pid,
                                 "age_seconds": row.get("age"), "command": str(row.get("command"))[:120]}

    # --- the verdict -------------------------------------------------------------------
    process = report["process"]["state"]
    # `delivered` with no message id is a serving that ended posting nothing.
    delivered = stage is not None and (stage.get("state") == "prepared"
                                       or (stage.get("state") == "delivered" and stage.get("delivered_id")))
    requeued = stage is not None and (stage.get("queued") or stage.get("later_servings"))
    if process == "exited":
        report["wait"] = {"kind": "none"}
        if requeued:
            report["verdict"], report["why"] = ENDED, (
                "the run is over and its conversation is already queued or served again")
        elif delivered:
            report["verdict"], report["why"] = ENDED, (
                f"the run is over and its serving is {stage['state']}: a reply is (being) posted")
        elif record.get("ended_at") and stage is None:
            report["verdict"], report["why"] = UNKNOWN, (
                f"the run ended ({record.get('outcome')}, exit {record.get('exit_code')}); whether its serving "
                "posted a reply could not be read")
            unknowns.append("the serving's reply")
        else:
            how = "the harness process is gone and its runner never recorded an end" if not record.get("ended_at") \
                else f"the run ended ({record.get('outcome')}, exit {record.get('exit_code')})"
            state = stage.get("state") if stage else "unknown"
            if state == "delivered":
                state = "delivered, nothing posted"
            report["verdict"], report["why"] = STOPPED, (
                f"{how}, its serving delivered no reply (journal: {state}) and nothing is queued to serve it again")
        return report
    if process != "alive":
        report["why"] = "whether the run's process still exists could not be established"
        return report

    deadline = record.get("deadline_at")
    children = descendants(table, pid) if table else []
    open_tools = [{"id": key, **value, "age_seconds": round(now - float(value.get("since") or now), 1)}
                  for key, value in (record.get("open_tools") or {}).items()]
    top = [t for t in open_tools if not t.get("parent")] or open_tools
    cpu = round(sum(float(c.get("cpu_seconds") or 0) for c in children), 2) if children else 0.0
    if top:
        tool = max(top, key=lambda t: t["age_seconds"])
        bound = tool.get("bound")
        report["wait"] = {"kind": "tool", "id": tool.get("id"), "name": tool.get("name"), "detail": tool.get("detail"),
                          "since": tool.get("since"), "bound_seconds": bound, "open": len(open_tools),
                          "children": children[:MAX_CHILDREN], "cpu_seconds": cpu,
                          "over_bound": bool(bound is not None and tool["age_seconds"] > float(bound) + TOOL_GRACE)}
    elif children:
        report["wait"] = {"kind": "children", "children": children[:MAX_CHILDREN], "count": len(children),
                          "cpu_seconds": cpu}
    elif record.get("tool_results"):
        report["wait"] = {"kind": "none"}
    else:
        unknowns.append(f"{record.get('harness')}'s events do not say when a tool call returns")
    age = now - float(last_work) if last_work else None
    quiet = f"no work for {int(age or 0)} s" + (
        f" (only housekeeping since: {kept.get('last')}, {int(now - kept['last_at'])} s ago)"
        if kept.get("last_at") and last_work and kept["last_at"] > last_work else "")
    if deadline and now > float(deadline):
        report["verdict"], report["why"] = UNKNOWN, (
            f"the process is alive {int(now - float(deadline))} s past the run's own deadline")
        unknowns.append("why the run outlived its deadline")
    elif age is not None and age <= window:
        report["verdict"], report["why"] = RUNNING, (
            f"alive; its last work ({record.get('last_work', record.get('last_event'))}) was {int(age)} s ago")
    elif report["wait"]["kind"] == "tool" and report["wait"]["over_bound"]:
        w = report["wait"]
        report["verdict"], report["why"] = UNKNOWN, (
            f"alive; {quiet}; its {w['name']} call" + (f" ({w['detail']})" if w.get("detail") else "")
            + f" has been open {int(now - float(w['since'] or now))} s, past its own bound of "
              f"{int(w['bound_seconds'])} s")
        unknowns.append("why a tool call is still open past its own bound")
    elif report["wait"]["kind"] == "tool":
        w = report["wait"]
        alive_under = " with a process under it" if children else ", with no process under it"
        within = f", within its bound of {int(w['bound_seconds'])} s" if w.get("bound_seconds") is not None else ""
        report["verdict"], report["why"] = WAITING, (
            f"alive; waiting {int(now - float(w['since'] or now))} s on {w['name']}"
            + (f" ({w['detail']})" if w.get("detail") else "") + within + alive_under
            + (f" ({w['cpu_seconds']:g} s CPU)" if children else ""))
    elif report["wait"]["kind"] == "children":
        report["verdict"], report["why"] = WAITING, (
            f"alive; {quiet}, and {len(children)} process(es) run under it "
            f"({children[0]['command'][:80]}; {report['wait']['cpu_seconds']:g} s CPU)")
    else:
        report["verdict"], report["why"] = UNKNOWN, (
            f"alive, but {quiet} and no tool call or child process explains the wait")
        unknowns.append("what the process is doing")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agag.health", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", required=True, type=Path, help="the live execution records")
    parser.add_argument("--queue", type=Path, default=None, help="the listener's queue file (listener.sqlite)")
    parser.add_argument("--ack", type=int, default=0)
    parser.add_argument("--channel", default="")
    parser.add_argument("--topic", default="")
    parser.add_argument("--window", type=float, default=WINDOW_SECONDS)
    parser.add_argument("--queued", action="store_true",
                        help="the conversation holds a post nobody acknowledged: where is it in the queue")
    parser.add_argument("--since", type=float, default=0.0, help="with --queued: when the post was made")
    args = parser.parse_args(argv)
    try:
        if args.queued:
            report = probe_queue(args.dir, channel=args.channel, topic=args.topic, queue=args.queue,
                                 since=args.since, window=args.window)
        else:
            report = probe(args.dir, ack=args.ack, channel=args.channel, topic=args.topic, queue=args.queue,
                           window=args.window)
    except Exception as error:  # noqa: BLE001 - the interface answers
        report = {"schema": SCHEMA, "observed_at": time.time(), "verdict": UNKNOWN,
                  "why": f"the probe failed: {error!r}", "unknowns": ["everything"]}
    json.dump(report, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
