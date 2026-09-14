"""Listening, split from serving: intake feeds a durable queue, an executor
drains it, and neither waits for the other.

`better_zulip_call` p1 step 5. Until now `agag.zulip.sweep_serve` was one
loop: poll the event queue, re-read every hinted topic's history, run the
handler synchronously, poll again. A long run therefore stopped the polling,
a burst was coalesced only while nothing ran, and downtime was recovered by
a full sweep of every subscribed channel (69–84 calls per restart, and a
paid run whenever the sweep's recovery disagreed with the served marks).

What replaces it:

- **Intake** follows the process's `agag.mirror` change feed. A `message`
  change whose conversation this bot *owns* (`topic_filter`) becomes an
  `owner` entry in the queue; one that names this bot in a conversation it
  does not own becomes a `mention` entry. Repeats for one conversation
  collapse into one entry; an event arriving while that conversation is
  being served marks the entry `again`, so the executor looks once more
  when the serving ends. No topic is read on intake: the mirror already
  holds it.
- **The executor** is one thread. It takes the oldest entry (owners before
  mentions), **evaluates the conversation as it stands now** from the
  mirror — who really spoke last, whether the topic is resolved, whether a
  served mark already covers the mention — and only then calls the handler.
  A serving that made this bot the last speaker leaves nothing to do; a
  human's post during the run is served next.
- **The queue is a file** (`listener.sqlite` beside the mirror), so a crash
  leaves its entries where they were. On start the entries that were
  `running` are judged by conversation evidence: a reply of ours after the
  ack means the run finished and the entry is dropped; anything else is
  served again. At-least-once, with duplicate suppression by evidence;
  nothing here promises exactly-once external effects.
- **Recovery** reads the mirror's index, not Zulip: every open topic this
  bot owns whose last real speaker is somebody else, every open topic that
  names this bot past its served mark. It runs at start and after every
  resync the mirror makes (a queue expiry is downtime by another name). The
  `on_recover` hook runs after it, for the obligations no last-speaker check
  can see — Front's unstarted runs and delivered reports.

The DM route is untouched: `agag.zulip.serve` on its own thread and its own
client, as before, because a direct message is account-specific and the
mirror is public conversations only.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .mirror import Change, Mirror, Message, bare_topic
from .selfnote import SELFNOTE_MARKER, is_selfnote, is_speech, note as selfnote_line, parse_served
from .status import StatusWriter, default_status_path
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, TopicFilter, log as default_log, topic_matches

#: The two routes an entry can be on. Owners first, always: a topic this bot
#: owns is never also a mention to answer somewhere else.
OWNER, MENTION = "owner", "mention"
#: How long the intake waits for the mirror to move before it looks at its
#: health again (and rewrites the status file).
IDLE_SECONDS = 30.0
#: The queue file, beside the mirror's store.
QUEUE_NAME = "listener.sqlite"

__all__ = [
    "IDLE_SECONDS",
    "MENTION",
    "OWNER",
    "QUEUE_NAME",
    "Entry",
    "Listener",
    "Queue",
    "current_mirror",
    "mentions_bot",
]

_current: dict[str, Mirror] = {}


def current_mirror() -> Mirror | None:
    """The mirror the running listener reads, for a handler that wants the
    same copy of the realm (Front's run recovery reads it instead of Zulip).
    None outside a listener."""
    return _current.get("mirror")


def mentions_bot(content: str, bot_name: str) -> bool:
    return bool(bot_name) and f"@**{bot_name}**" in str(content or "")


@dataclass(frozen=True)
class Entry:
    channel: str
    topic: str
    route: str
    state: str
    revision: int
    message_id: int
    attempts: int = 0
    again: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.channel, self.topic, self.route)


class Queue:
    """The durable pending-work queue: one row per conversation and route."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS pending (
        channel TEXT NOT NULL, topic TEXT NOT NULL, route TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
        message_id INTEGER NOT NULL DEFAULT 0, enqueued_at REAL NOT NULL,
        started_at REAL, attempts INTEGER NOT NULL DEFAULT 0, again INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (channel, topic, route)
    );
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(self.SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- the checkpoint of the change feed ----------------------------------------

    def revision(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
        return int(row["value"]) if row else 0

    def set_revision(self, revision: int) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('revision', ?)", (str(int(revision)),))

    # -- entries ------------------------------------------------------------------

    @staticmethod
    def _entry(row) -> Entry:
        return Entry(row["channel"], row["topic"], row["route"], row["state"], int(row["revision"]),
                     int(row["message_id"]), int(row["attempts"]), bool(row["again"]))

    def enqueue(self, channel: str, topic: str, route: str, *, revision: int, message_id: int,
                at: float | None = None) -> bool:
        """Add or coalesce. Returns True when the entry is new or was
        re-armed while running — the executor should look."""
        at = time.time() if at is None else at
        with self._lock:
            row = self._db.execute("SELECT * FROM pending WHERE channel = ? AND topic = ? AND route = ?",
                                   (channel, topic, route)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO pending (channel, topic, route, state, revision, message_id, enqueued_at)"
                    " VALUES (?, ?, ?, 'pending', ?, ?, ?)", (channel, topic, route, int(revision), int(message_id), at))
                return True
            self._db.execute(
                "UPDATE pending SET revision = MAX(revision, ?), message_id = MAX(message_id, ?),"
                " again = CASE WHEN state = 'running' THEN 1 ELSE again END"
                " WHERE channel = ? AND topic = ? AND route = ?",
                (int(revision), int(message_id), channel, topic, route))
            return row["state"] == "running"

    def take(self, at: float | None = None) -> Entry | None:
        """The oldest pending entry, owners first, marked running."""
        at = time.time() if at is None else at
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pending WHERE state = 'pending'"
                " ORDER BY CASE route WHEN 'owner' THEN 0 ELSE 1 END, enqueued_at, channel, topic LIMIT 1").fetchone()
            if row is None:
                return None
            self._db.execute(
                "UPDATE pending SET state = 'running', started_at = ?, attempts = attempts + 1, again = 0"
                " WHERE channel = ? AND topic = ? AND route = ?", (at, row["channel"], row["topic"], row["route"]))
            return self._entry(self._db.execute(
                "SELECT * FROM pending WHERE channel = ? AND topic = ? AND route = ?",
                (row["channel"], row["topic"], row["route"])).fetchone())

    def finish(self, entry: Entry) -> bool:
        """Done with an entry. It stays, pending again, when an event arrived
        for it during the serving; returns whether it did."""
        with self._lock:
            row = self._db.execute("SELECT again FROM pending WHERE channel = ? AND topic = ? AND route = ?",
                                   entry.key).fetchone()
            if row is None:
                return False
            if row["again"]:
                self._db.execute(
                    "UPDATE pending SET state = 'pending', again = 0, started_at = NULL, enqueued_at = ?"
                    " WHERE channel = ? AND topic = ? AND route = ?", (time.time(), *entry.key))
                return True
            self._db.execute("DELETE FROM pending WHERE channel = ? AND topic = ? AND route = ?", entry.key)
            return False

    def drop(self, entry: Entry) -> None:
        with self._lock:
            self._db.execute("DELETE FROM pending WHERE channel = ? AND topic = ? AND route = ?", entry.key)

    def requeue(self, entry: Entry) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE pending SET state = 'pending', again = 0, started_at = NULL"
                " WHERE channel = ? AND topic = ? AND route = ?", entry.key)

    def entries(self, state: str | None = None) -> list[Entry]:
        with self._lock:
            if state is None:
                rows = self._db.execute("SELECT * FROM pending ORDER BY enqueued_at").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM pending WHERE state = ? ORDER BY enqueued_at", (state,)).fetchall()
        return [self._entry(row) for row in rows]

    def __len__(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) AS n FROM pending").fetchone()["n"])


class Listener:
    """Intake and execution over one mirror and one serving client."""

    def __init__(
        self,
        mirror: Mirror,
        client: ZulipClient,
        *,
        topic_filter: TopicFilter,
        handler: Callable[[str, str], None],
        on_mention: Callable[[str, str], None] | None = None,
        on_recover: Callable[[], None] | None = None,
        is_ack: Callable[[str], bool] | None = None,
        queue_path: Path | None = None,
        log=default_log,
        status: StatusWriter | None = None,
        idle_seconds: float = IDLE_SECONDS,
    ):
        self.mirror = mirror
        self.client = client
        self.topic_filter = topic_filter
        self.handler = handler
        self.on_mention = on_mention
        self.on_recover = on_recover
        self.is_ack = is_ack or (lambda content: False)
        self.log = log
        self.status = status if status is not None else StatusWriter(default_status_path(), log=log)
        self.idle_seconds = float(idle_seconds)
        self.queue = Queue(queue_path or (mirror.store.path.parent / QUEUE_NAME))
        self.self_id: int | None = None
        self.bot_name = ""
        self._stop = threading.Event()
        self._wake = threading.Condition()
        self._executor: threading.Thread | None = None
        self.served: int = 0
        self.recoveries: int = 0

    # -- identity ----------------------------------------------------------------------

    def _identify(self) -> None:
        while self.self_id is None and not self._stop.is_set():
            try:
                profile = self.client.whoami()
                self.self_id = int(profile["user_id"])
                self.bot_name = str(profile.get("full_name") or "") or ""
                self.log(f"listening as user_id={self.self_id} ({getattr(self.client, 'email', '')})")
            except Exception as error:  # noqa: BLE001 - Zulip restarts; a listener outlives that
                self.status.record_error(str(error))
                self.log(f"whoami failed: {error!r}; retrying")
                self._stop.wait(5.0)

    # -- evaluation: the conversation as it stands now ------------------------------------

    def _live(self, channel: str, topic: str):
        """The open topic of that bare name, or None when it is resolved or gone."""
        found = [t for t in self.mirror.topic(channel, topic) if not t.resolved]
        return found[0] if found else None

    def _last_real(self, channel: str, live_name: str) -> Message | None:
        for message in reversed(self.mirror.messages(channel, live_name, across_resolve=False, limit=60)):
            if is_speech(message.as_zulip()):
                return message
        return None

    def served_marks(self) -> dict[tuple[str, str], int]:
        """`{(channel, bare topic): newest served id}` from this bot's own
        served notes, out of the mirror's index."""
        marks: dict[tuple[str, str], int] = {}
        if self.self_id is None:
            return marks
        for note in self.mirror.notes(tag="served", sender_id=self.self_id):
            parsed = parse_served(selfnote_line("served", note.value))
            if parsed is None:
                continue
            remote, message_id = parsed
            key = (remote.channel, bare_topic(remote.topic))
            if message_id > marks.get(key, 0):
                marks[key] = message_id
        return marks

    def owed(self, entry: Entry, marks: dict[tuple[str, str], int] | None = None) -> str | None:
        """The live topic name to serve, or None when nothing is owed here."""
        index = self._live(entry.channel, entry.topic)
        if index is None:
            return None
        last = self._last_real(entry.channel, index.live_name)
        if last is None:
            # A listed topic holds messages; if none is speech, nobody spoke.
            return None
        if last.sender_id == self.self_id:
            return None
        if entry.route == MENTION:
            marks = self.served_marks() if marks is None else marks
            if last.id <= marks.get((entry.channel, entry.topic), 0):
                return None
        return index.live_name

    # -- intake ----------------------------------------------------------------------------

    def _intake(self, change: Change) -> None:
        if change.kind == "message" and change.message_id is not None:
            message = self.mirror.message(change.message_id)
            if message is None or message.sender_id == self.self_id or is_selfnote(message.content):
                return
            if message.resolved:
                return
            self._consider(message.channel, message.topic, message.content, change.revision, message.id,
                           flagged=bool(change.detail.get("mentioned")))
        elif change.kind == "move" and change.topic and not change.topic.startswith(RESOLVED_TOPIC_PREFIX):
            # A topic un-resolved or renamed into a name this bot owns: what
            # it now awaits is judged when the executor gets to it.
            if topic_matches(change.channel, change.topic, self.topic_filter):
                self._enqueue(change.channel, bare_topic(change.topic), OWNER, change.revision, change.message_id or 0)
        elif change.kind == "resync":
            self.recover(reason="the mirror re-read the realm")

    def _consider(self, channel: str, topic: str, content: str, revision: int, message_id: int, *,
                  flagged: bool = False) -> None:
        if topic_matches(channel, topic, self.topic_filter):
            self._enqueue(channel, bare_topic(topic), OWNER, revision, message_id)
        elif self.on_mention is not None and (flagged or mentions_bot(content, self.bot_name)):
            self._enqueue(channel, bare_topic(topic), MENTION, revision, message_id)

    def _enqueue(self, channel: str, topic: str, route: str, revision: int, message_id: int) -> None:
        if self.queue.enqueue(channel, topic, route, revision=revision, message_id=message_id):
            with self._wake:
                self._wake.notify_all()

    def recover(self, *, reason: str) -> int:
        """Rebuild the pending set from the mirror's index — no Zulip call —
        and run the `on_recover` hook. Returns how many entries were added."""
        if self.self_id is None:
            return 0
        added = 0
        marks = self.served_marks() if self.on_mention is not None else {}
        revision = self.mirror.revision()
        for index in self.mirror.topics(include_resolved=False):
            key = (index.channel, index.name)
            last = index.last_real
            if last is None or last.sender_id == self.self_id:
                continue
            if topic_matches(index.channel, index.live_name, self.topic_filter):
                route = OWNER
            elif self.on_mention is not None and mentions_bot(last.content, self.bot_name) \
                    and last.id > marks.get(key, 0):
                route = MENTION
            else:
                continue
            if self.queue.enqueue(index.channel, index.name, route, revision=revision, message_id=last.id):
                added += 1
        self.recoveries += 1
        self.log(f"recovery ({reason}): {added} conversation(s) queued from the index, "
                 f"{len(self.queue)} pending")
        if self.on_recover is not None:
            try:
                self.on_recover()
            except Exception as error:  # noqa: BLE001 - recovery must not end the listener
                self.log(f"post-recovery hook failed: {error!r}")
        with self._wake:
            self._wake.notify_all()
        return added

    def _resume_running(self) -> None:
        """Entries a crash left `running`: judged by what the conversation
        shows. A reply of ours that is not the ack means the run finished;
        anything else is owed again."""
        for entry in self.queue.entries("running"):
            index = self._live(entry.channel, entry.topic)
            history = self.mirror.messages(entry.channel, index.live_name, across_resolve=False) if index else []
            ours = [m for m in history if m.sender_id == self.self_id and is_speech(m.as_zulip())]
            if index is None or (ours and not self.is_ack(ours[-1].content.strip())
                                 and history and history[-1].sender_id == self.self_id):
                self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r} was served before the restart; dropped")
                self.queue.drop(entry)
            else:
                self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r} was running at the restart; queued again")
                self.queue.requeue(entry)

    # -- execution -----------------------------------------------------------------------------

    def _execute(self) -> None:
        while not self._stop.is_set():
            entry = self.queue.take()
            if entry is None:
                with self._wake:
                    self._wake.wait(1.0)
                continue
            try:
                live = self.owed(entry)
                if live is None:
                    self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r}: nothing owed now; skipped")
                else:
                    self.log(f"{'serving' if entry.route == OWNER else 'serving mention in'} {entry.channel!r}/{live!r}")
                    serve = self.handler if entry.route == OWNER else self.on_mention
                    try:
                        serve(entry.channel, live)  # type: ignore[misc]
                        self.served += 1
                    except Exception as error:  # noqa: BLE001 - one bad topic must not end the loop
                        self.log(f"handler failed on {entry.channel!r}/{live!r}: {error!r}")
            finally:
                if self.queue.finish(entry):
                    self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r}: an event arrived during the "
                             f"serving; looking again")

    # -- lifecycle -----------------------------------------------------------------------------

    def start_executor(self) -> None:
        if self._executor is None:
            self._executor = threading.Thread(target=self._execute, name="listener-executor", daemon=True)
            self._executor.start()

    def run(self) -> None:
        """Block: identify, wait for the mirror's first fill, recover, then
        follow the change feed until stopped."""
        _current["mirror"] = self.mirror
        self._identify()
        while not self.mirror.live and not self._stop.is_set():
            self.status.record_error(self.mirror.health()["reason"])
            self.mirror.wait(self.mirror.revision(), timeout=2.0)
        if self._stop.is_set():
            return
        self._resume_running()
        # The index says what is owed *now*, which covers everything the feed
        # could replay from an older checkpoint — so the checkpoint moves to
        # the revision the recovery read at, and only what lands after it is
        # taken from the feed. (A change arriving during the recovery pass is
        # both in the index and replayed; the queue coalesces it.)
        revision = self.mirror.revision()
        self.recover(reason="startup")
        self.queue.set_revision(revision)
        self.start_executor()
        while not self._stop.is_set():
            changes = self.mirror.changes(revision)
            if changes is None:
                self.log("the change feed forgot the checkpoint; recovering from the index")
                revision = self.mirror.revision()
                self.queue.set_revision(revision)
                self.recover(reason="the change feed was truncated")
                continue
            for change in changes:
                try:
                    self._intake(change)
                except Exception as error:  # noqa: BLE001 - one bad change must not end intake
                    self.log(f"intake failed on change {change.revision} ({change.kind}): {error!r}")
                revision = change.revision
            if changes:
                self.queue.set_revision(revision)
            health = self.mirror.health()
            if health["state"] == "live":
                self.status.record_poll_ok(health["queue"])
            else:
                self.status.record_error(str(health["reason"]))
            self.mirror.wait(revision, timeout=self.idle_seconds)

    def stop(self) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        _current.pop("mirror", None)
