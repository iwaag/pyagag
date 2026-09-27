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

The verdict:

- `running`: the process is alive and an event arrived within `window`;
- `waiting`: alive, quiet, and a named tool call or child process explains
  the wait, inside the run's own deadline;
- `stopped`: the work is not being done and nothing is queued to do it —
  the harness process is gone with no end recorded, or it ended and its
  serving delivered nothing; confirmed, not guessed;
- `ended`: the run ended and its serving delivered (or is delivering) a
  reply: nothing is running, which is the conversation's business;
- `unknown`: anything else, with `unknowns` saying why.

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

RUNNING, WAITING, STOPPED, ENDED, UNKNOWN = "running", "waiting", "stopped", "ended", "unknown"
VERDICTS = (RUNNING, WAITING, STOPPED, ENDED, UNKNOWN)

__all__ = ["SCHEMA", "VERDICTS", "probe", "main"]


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


def process_table(timeout: float = PS_TIMEOUT) -> dict[int, dict[str, Any]] | None:
    """`pid → {ppid, age, command}` for every process, or None."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,etime=,command="], capture_output=True, text=True,
                             timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table: dict[int, dict[str, Any]] = {}
    for line in out.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        table[pid] = {"ppid": ppid, "age": _etime_seconds(fields[2]), "command": fields[3] if len(fields) > 3 else ""}
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
                found.append({"pid": child, "age_seconds": table[child]["age"],
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


def _matching(directory: Path, ack: int, channel: str, topic: str) -> tuple[dict | None, list[dict]]:
    """The record of the serving acked by `ack`, and the records of the same
    conversation that belong to other servings (never applied)."""
    mine, others = None, []
    for _, doc in execution_records(directory):
        serving = doc.get("serving") or {}
        if ack and int(serving.get("ack") or 0) == int(ack):
            if mine is None:
                mine = doc
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
                     "record_written_at": record.get("written_at"), "role": (record.get("serving") or {}).get("role")}
    last_event = record.get("last_event_at")
    report["progress"] = {"last_event_at": last_event, "age_seconds": round(now - last_event, 1) if last_event else None,
                          "last_event": record.get("last_event"), "events": record.get("events"),
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
    if top:
        tool = max(top, key=lambda t: t["age_seconds"])
        report["wait"] = {"kind": "tool", "name": tool.get("name"), "detail": tool.get("detail"),
                          "since": tool.get("since"), "open": len(open_tools),
                          "children": children[:MAX_CHILDREN]}
    elif children:
        report["wait"] = {"kind": "children", "children": children[:MAX_CHILDREN], "count": len(children)}
    elif record.get("tool_results"):
        report["wait"] = {"kind": "none"}
    else:
        unknowns.append(f"{record.get('harness')}'s events do not say when a tool call returns")
    age = now - float(last_event) if last_event else None
    if deadline and now > float(deadline):
        report["verdict"], report["why"] = UNKNOWN, (
            f"the process is alive {int(now - float(deadline))} s past the run's own deadline")
        unknowns.append("why the run outlived its deadline")
    elif age is not None and age <= window:
        report["verdict"], report["why"] = RUNNING, f"alive; its last event ({record.get('last_event')}) was {int(age)} s ago"
    elif report["wait"]["kind"] == "tool":
        w = report["wait"]
        alive_under = " with a process under it" if children else ", with no process under it"
        report["verdict"], report["why"] = WAITING, (
            f"alive; waiting {int(now - float(w['since'] or now))} s on {w['name']}"
            + (f" ({w['detail']})" if w.get("detail") else "") + alive_under)
    elif report["wait"]["kind"] == "children":
        report["verdict"], report["why"] = WAITING, (
            f"alive; no event for {int(age or 0)} s, and {len(children)} process(es) run under it "
            f"({children[0]['command'][:80]})")
    else:
        report["verdict"], report["why"] = UNKNOWN, (
            f"alive, but no event for {int(age or 0)} s and no tool call or child process explains the wait")
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
    args = parser.parse_args(argv)
    try:
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
