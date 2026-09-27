"""What a harness run is doing right now, kept where another process can read it.

`failsafe` p2 step 2. Until now a serving's health was read from its
conversation: an ack with no `end=` said "open", and whether anything was
still running behind it was in no record at all. m11741's harness had ended
an hour before anybody noticed; p2's silent-exit case is a harness that dies
with nothing posted. Both look exactly like a long job.

A **live execution record** is one small JSON file per harness run, written
by `run_harness` while the run lasts (`agag.execution.v1`):

- **who**: the serving it belongs to (the listener's journal id, the ack
  that opened it, the conversation), the harness and its process id;
- **progress**: when the last event arrived from the harness, how many,
  and what the last one was (a tool call, a text block, a partial message);
- **waits**: the tool calls that were started and have not returned yet,
  each with its name, its one telling argument and when it began — a test
  suite, a subagent (`Task`), a `sleep` — for harnesses whose stream carries
  tool results (`claude_code`, `agcode`); the others say they cannot tell;
- **deadline**: when `run_harness` will kill the run;
- **end**: when it ended and how (exit code, outcome), written by the
  process that ran it — so a record with no end and a dead pid is a run that
  died without its runner noticing.

Nothing here decides anything. `agag.health` reads the record, the process
table and the serving journal, and says what can and cannot be concluded.

Writes are atomic (a temporary file and a rename) and throttled: a tool
starting or finishing is written at once; any other event at most every
`WRITE_EVERY` seconds, which is what bounds the cost of a partial-message
stream. A failed write never touches the run.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = "agag.execution.v1"
#: Plain events (a text block, a partial message) are written at most this
#: often; tool starts and ends and the run's own start and end at once.
WRITE_EVERY = 5.0
#: Harnesses whose event stream says when a tool call *returns*. For the
#: others an open tool call cannot be told from a finished one.
TOOL_RESULTS = ("claude_code", "agcode")
DETAIL_KEYS = ("command", "description", "file_path", "path", "pattern", "url", "subagent_type")
DETAIL_CHARS = 200

__all__ = ["SCHEMA", "LiveExecution", "read", "records"]


def _detail(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    for key in DETAIL_KEYS:
        value = arguments.get(key)
        if value:
            return " ".join(str(value).split())[:DETAIL_CHARS]
    return ""


class LiveExecution:
    """The record of one harness run, written as it goes.

    `serving` is whatever identifies the serving the run belongs to — the
    listener fills it from its journal (`agag.serving.current()`): `id`,
    `ack`, `channel`, `topic`, `home_anchor`. `begin`, `event` and `end` are
    called by `run_harness`; `event` from the stdout reader thread."""

    def __init__(self, path: Path, *, serving: dict[str, Any] | None = None, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.Lock()
        self._written = 0.0
        self.doc: dict[str, Any] = {
            "schema": SCHEMA,
            "serving": dict(serving or {}),
            "harness": "",
            "pid": None,
            "started_at": None,
            "deadline_at": None,
            "last_event_at": None,
            "last_event": "",
            "events": 0,
            "tool_results": False,
            "open_tools": {},
            "tools_done": 0,
            "ended_at": None,
            "exit_code": None,
            "outcome": None,
            "written_at": None,
        }

    # --- what run_harness tells it ------------------------------------------------------

    def begin(self, *, harness: str, pid: int | None, timeout: float) -> None:
        now = self.clock()
        with self._lock:
            self.doc.update(harness=harness, pid=pid, started_at=now, deadline_at=now + float(timeout),
                            tool_results=harness in TOOL_RESULTS, last_event_at=now, last_event="started")
            self._write(force=True)

    def event(self, event: dict) -> None:
        """One harness event (claude_code's stream-json shape, which agcode
        and the adapters for agy and codex also speak)."""
        now = self.clock()
        kind = str(event.get("type") or "")
        force = False
        with self._lock:
            self.doc["events"] += 1
            self.doc["last_event_at"] = now
            open_tools: dict[str, Any] = self.doc["open_tools"]
            if kind == "tool_use":  # the agy/codex adapters' flat form
                self.doc["last_event"] = f"tool {event.get('name', '?')}"
                force = True
            message = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    self.doc["last_event"] = f"tool {name}"
                    if self.doc["tool_results"] and block.get("id"):
                        open_tools[str(block["id"])] = {
                            "name": name, "detail": _detail(block.get("input")), "since": now,
                            # A subagent's own tool calls are progress of the call that started it.
                            "parent": event.get("parent_tool_use_id") or None,
                        }
                    force = True
                elif block.get("type") == "tool_result":
                    if open_tools.pop(str(block.get("tool_use_id") or ""), None) is not None:
                        self.doc["tools_done"] += 1
                    self.doc["last_event"] = "tool result"
                    force = True
                elif block.get("type") == "text" and kind == "assistant":
                    self.doc["last_event"] = "text"
            if kind == "stream_event":
                self.doc["last_event"] = "generating"
            elif kind == "system" and not content:
                self.doc["last_event"] = f"system {event.get('subtype', '')}".strip()
            self._write(force=force)

    def end(self, *, exit_code: int | None, outcome: str) -> None:
        with self._lock:
            self.doc.update(ended_at=self.clock(), exit_code=exit_code, outcome=outcome)
            self._write(force=True)

    # --- the file --------------------------------------------------------------------------

    def _write(self, *, force: bool) -> None:
        now = self.clock()
        if not force and now - self._written < WRITE_EVERY:
            return
        self._written = now
        self.doc["written_at"] = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(self.doc, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            pass  # a record that cannot be written must never touch the run

    def flush(self) -> None:
        with self._lock:
            self._write(force=True)


def read(path: Path) -> dict[str, Any] | None:
    """One record, or None when it is missing or unreadable."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == SCHEMA else None


def records(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Every readable record in `directory`, newest start first."""
    found = []
    try:
        paths = list(Path(directory).glob("*.json"))
    except OSError:
        return []
    for path in paths:
        doc = read(path)
        if doc is not None:
            found.append((path, doc))
    found.sort(key=lambda item: -(item[1].get("started_at") or 0))
    return found
