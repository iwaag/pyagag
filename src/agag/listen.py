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
  bot owns whose last real speaker is somebody else, every open topic
  holding a post that names this bot past its served mark (`argue` p1: the
  newest such post, not only the last one — a mention is an invitation and
  is owed until the mark passes it). It runs at start and after every
  resync the mirror makes (a queue expiry is downtime by another name). The
  `on_recover` hook runs after it, for the obligations no last-speaker check
  can see — Front's unstarted runs and delivered reports.

**A memo is never work** (`argue` p2 step 1, `agag.memo`). A conversation in
a memo channel is presentation only, and the same one rule is asked at every
point where a conversation can become a serving: intake (a post, a mention,
a rename into an owned name), recovery, the entries a crash left running,
and — because a queue file outlives the process that filled it — once more
at execution time, where an entry queued by an older listener is dropped.

**A serving is journaled** (`explicit_reply` p1 step 1, `agag.serving`).
Beside the queue the file holds one record per serving with the stages a
reply goes through — received, acked, executed, prepared, delivered — and
the executor binds it for the handler's thread, so `serve_topic` writes the
ack's id, the input boundary it processed, the reply text *before* the send
and the delivered id after it is confirmed. Three consequences:

- **"Who spoke last" is no longer the whole evidence.** An ack of ours is
  transport, not an answer: `owed` skips this bot's own acks when it asks
  who really spoke last, so a crash right after the ack leaves the request
  owed instead of looking answered. A restart marks the records it finds
  between `received` and `prepared` as `interrupted` — their evidence is
  handed to the next serving of the same conversation as `context.previous`.
- **A reply that was prepared is delivered, not re-generated.** A send
  whose answer was lost, a listener that died between the send and the
  confirmation, a `DeliveryError` out of `serve_topic` — all leave a
  `prepared` record, and the next pass redelivers *that text* after reading
  the conversation back for it (`agag.delivery`). The model does not run
  again and no external action is repeated.
- **A transport failure is retried with bounded backoff and never
  disappears.** An entry whose serving raised is scheduled again
  (`RETRY_SECONDS`, doubling, up to `MAX_ATTEMPTS`); exhausted, it is kept
  in the queue as `failed` with its reason — visible in `entries("failed")`
  and the log — and re-armed by the next post in that conversation.

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

from . import serving as serving_record
from .delivery import DeliveryError
from .memo import is_memo_channel
from .mirror import Change, Mirror, Message, bare_topic
from .selfnote import (
    SELFNOTE_MARKER, Conversation, is_selfnote, is_speech, note as selfnote_line, parse_served, served_note,
)
from .serving import ACKED, DELIVERED, EXECUTED, FAILED, INTERRUPTED, PREPARED, RECEIVED, Serving
from .status import StatusWriter, default_status_path
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, TopicFilter, live_topic_name, log as default_log, topic_matches

#: The two routes an entry can be on. Owners first, always: a topic this bot
#: owns is never also a mention to answer somewhere else.
OWNER, MENTION = "owner", "mention"
#: How long the intake waits for the mirror to move before it looks at its
#: health again (and rewrites the status file).
IDLE_SECONDS = 30.0
#: The queue file, beside the mirror's store.
QUEUE_NAME = "listener.sqlite"
#: The queue file's layout. Bumped when the tables change: the file is
#: disposable state and is rebuilt rather than migrated.
QUEUE_SCHEMA = "2"
#: How many times one entry is served before it is left `failed`, and the
#: first delay between attempts (doubling each time, capped).
MAX_ATTEMPTS = 5
RETRY_SECONDS = 5.0
RETRY_CAP_SECONDS = 300.0

__all__ = [
    "IDLE_SECONDS",
    "MAX_ATTEMPTS",
    "MENTION",
    "OWNER",
    "QUEUE_NAME",
    "RETRY_SECONDS",
    "Entry",
    "Listener",
    "Queue",
    "QueueJournal",
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
    """Whether `content` names this bot with `@**<name>**`.

    Case-insensitive on the name (`argue` p1 step 4): Front wrote
    `@**cagent**` for the bot whose display name is `Cagent`, Zulip rendered
    it as plain text, and the invitation reached nobody. The mirror sees
    every post whether or not Zulip made a pill of it, and a name that
    differs only in case is not a different agent.
    """
    return bool(bot_name) and f"@**{bot_name.lower()}**" in str(content or "").lower()


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
    next_at: float | None = None
    failure: str = ""

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
        next_at REAL, failure TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (channel, topic, route)
    );
    CREATE TABLE IF NOT EXISTS servings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL, topic TEXT NOT NULL, route TEXT NOT NULL,
        trigger_id INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'received',
        attempt INTEGER NOT NULL DEFAULT 1,
        home_channel TEXT NOT NULL DEFAULT '', home_topic TEXT NOT NULL DEFAULT '',
        ack_id INTEGER, input_up_to INTEGER, requester_id INTEGER, requester_name TEXT NOT NULL DEFAULT '',
        reply_channel TEXT NOT NULL DEFAULT '', reply_topic TEXT NOT NULL DEFAULT '', reply_text TEXT,
        reply_after INTEGER NOT NULL DEFAULT 0, resolve_after INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0, delivered_id INTEGER,
        reply_marked INTEGER, reply_blocks INTEGER NOT NULL DEFAULT 0, reply_failure TEXT NOT NULL DEFAULT '',
        run_record TEXT NOT NULL DEFAULT '', failure TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0, extra TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS servings_key ON servings (channel, topic, route, id);
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
            self._db.executescript("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);")
            row = self._db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
            if row is not None and row["value"] != QUEUE_SCHEMA:
                # Disposable state: an older layout is dropped, and the
                # startup recovery rebuilds the pending set from the index.
                self._db.executescript("DROP TABLE IF EXISTS pending; DROP TABLE IF EXISTS servings;")
            self._db.executescript(self.SCHEMA)
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema', ?)", (QUEUE_SCHEMA,))

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
                     int(row["message_id"]), int(row["attempts"]), bool(row["again"]),
                     None if row["next_at"] is None else float(row["next_at"]), str(row["failure"] or ""))

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
            # A failed entry is re-armed by a newer post: its attempts start
            # over, because the conversation moved on and the failure was
            # about the serving that did not happen, not about the entry.
            rearmed = row["state"] == "failed" and int(message_id) > int(row["message_id"])
            self._db.execute(
                "UPDATE pending SET revision = MAX(revision, ?), message_id = MAX(message_id, ?),"
                " again = CASE WHEN state = 'running' THEN 1 ELSE again END,"
                " state = CASE WHEN state = 'failed' AND ? THEN 'pending' ELSE state END,"
                " attempts = CASE WHEN state = 'failed' AND ? THEN 0 ELSE attempts END,"
                " next_at = CASE WHEN state = 'failed' AND ? THEN NULL ELSE next_at END,"
                " failure = CASE WHEN state = 'failed' AND ? THEN '' ELSE failure END"
                " WHERE channel = ? AND topic = ? AND route = ?",
                (int(revision), int(message_id), rearmed, rearmed, rearmed, rearmed, channel, topic, route))
            return row["state"] == "running" or rearmed

    def take(self, at: float | None = None) -> Entry | None:
        """The oldest pending entry whose retry time has come, owners first,
        marked running."""
        at = time.time() if at is None else at
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pending WHERE state = 'pending' AND (next_at IS NULL OR next_at <= ?)"
                " ORDER BY CASE route WHEN 'owner' THEN 0 ELSE 1 END, enqueued_at, channel, topic LIMIT 1",
                (at,)).fetchone()
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
                "UPDATE pending SET state = 'pending', again = 0, started_at = NULL, next_at = NULL"
                " WHERE channel = ? AND topic = ? AND route = ?", entry.key)

    def retry_later(self, entry: Entry, delay: float, reason: str, at: float | None = None) -> None:
        """Pending again, not before `delay` seconds, with why."""
        at = time.time() if at is None else at
        with self._lock:
            self._db.execute(
                "UPDATE pending SET state = 'pending', started_at = NULL, next_at = ?, failure = ?"
                " WHERE channel = ? AND topic = ? AND route = ?", (at + delay, reason[:500], *entry.key))

    def fail(self, entry: Entry, reason: str) -> None:
        """Out of attempts: kept, visible, and re-armed by the next post."""
        with self._lock:
            self._db.execute(
                "UPDATE pending SET state = 'failed', started_at = NULL, next_at = NULL, failure = ?"
                " WHERE channel = ? AND topic = ? AND route = ?", (reason[:500], *entry.key))

    # -- servings ------------------------------------------------------------------

    @staticmethod
    def _serving(row) -> Serving:
        import json

        return Serving(
            id=int(row["id"]), channel=row["channel"], topic=row["topic"], route=row["route"],
            trigger_id=int(row["trigger_id"]), state=row["state"], attempt=int(row["attempt"]),
            home_channel=row["home_channel"], home_topic=row["home_topic"],
            ack_id=row["ack_id"], input_up_to=row["input_up_to"],
            requester_id=row["requester_id"], requester_name=row["requester_name"],
            reply_channel=row["reply_channel"], reply_topic=row["reply_topic"], reply_text=row["reply_text"],
            reply_after=int(row["reply_after"]), resolve_after=bool(row["resolve_after"]),
            resolved=bool(row["resolved"]), delivered_id=row["delivered_id"],
            reply_marked=None if row["reply_marked"] is None else bool(row["reply_marked"]),
            reply_blocks=int(row["reply_blocks"]), reply_failure=row["reply_failure"],
            run_record=row["run_record"], failure=row["failure"],
            started_at=float(row["started_at"]), updated_at=float(row["updated_at"]),
            extra=json.loads(row["extra"] or "{}"),
        )

    def open_serving(self, entry: Entry, at: float | None = None) -> "QueueJournal":
        """A new `received` record for this entry, and the journal that
        writes into it."""
        at = time.time() if at is None else at
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO servings (channel, topic, route, trigger_id, attempt, started_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (entry.channel, entry.topic, entry.route, int(entry.message_id), int(entry.attempts), at, at))
            return QueueJournal(self, int(cursor.lastrowid))

    def serving(self, serving_id: int) -> Serving | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM servings WHERE id = ?", (int(serving_id),)).fetchone()
        return None if row is None else self._serving(row)

    def latest_serving(self, key: tuple[str, str, str], *, states: tuple[str, ...] | None = None) -> Serving | None:
        """The newest record for this entry key, optionally only in `states`."""
        with self._lock:
            if states:
                marks = ",".join("?" for _ in states)
                row = self._db.execute(
                    f"SELECT * FROM servings WHERE channel = ? AND topic = ? AND route = ? AND state IN ({marks})"
                    " ORDER BY id DESC LIMIT 1", (*key, *states)).fetchone()
            else:
                row = self._db.execute(
                    "SELECT * FROM servings WHERE channel = ? AND topic = ? AND route = ?"
                    " ORDER BY id DESC LIMIT 1", key).fetchone()
        return None if row is None else self._serving(row)

    def servings(self, state: str | None = None, *, limit: int = 200) -> list[Serving]:
        with self._lock:
            if state is None:
                rows = self._db.execute("SELECT * FROM servings ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = self._db.execute("SELECT * FROM servings WHERE state = ? ORDER BY id DESC LIMIT ?",
                                        (state, limit)).fetchall()
        return [self._serving(row) for row in rows]

    def update_serving(self, serving_id: int, **fields) -> None:
        import json

        if "extra" in fields and not isinstance(fields["extra"], str):
            fields["extra"] = json.dumps(fields["extra"], ensure_ascii=False)
        fields["updated_at"] = time.time()
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._db.execute(f"UPDATE servings SET {columns} WHERE id = ?", (*fields.values(), int(serving_id)))

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


class QueueJournal:
    """`agag.serving.Journal` over one row of the queue's `servings` table.
    Every call is one UPDATE, committed at once (autocommit), so the record
    is on disk before the next step of the serving begins."""

    def __init__(self, queue: Queue, serving_id: int):
        self.queue = queue
        self.id = int(serving_id)

    @property
    def trigger_id(self) -> int:
        record = self.serving()
        return record.trigger_id if record is not None else 0

    def serving(self) -> Serving | None:
        return self.queue.serving(self.id)

    def home(self, channel: str, topic: str) -> None:
        self.queue.update_serving(self.id, home_channel=channel, home_topic=topic)

    def acked(self, message_id: int) -> None:
        self.queue.update_serving(self.id, state=ACKED, ack_id=int(message_id))

    def executed(self, input_up_to: int, *, requester_id: int | None, requester_name: str) -> None:
        self.queue.update_serving(self.id, state=EXECUTED, input_up_to=int(input_up_to),
                                  requester_id=requester_id, requester_name=requester_name or "")

    def record(self, path: str) -> None:
        self.queue.update_serving(self.id, run_record=str(path))

    def reply_outcome(self, *, marked: bool | None, blocks: int, failure: str) -> None:
        self.queue.update_serving(self.id, reply_marked=None if marked is None else int(marked),
                                  reply_blocks=int(blocks), reply_failure=failure or "")

    def prepared(self, channel: str, topic: str, text: str, *, resolve_after: bool, after_id: int) -> None:
        self.queue.update_serving(self.id, state=PREPARED, reply_channel=channel, reply_topic=topic,
                                  reply_text=text, resolve_after=int(bool(resolve_after)), reply_after=int(after_id))

    def delivered(self, message_id: int | None) -> None:
        self.queue.update_serving(self.id, state=DELIVERED, delivered_id=message_id)

    def resolved(self) -> None:
        self.queue.update_serving(self.id, resolved=1)

    def failed(self, reason: str) -> None:
        self.queue.update_serving(self.id, state=FAILED, failure=reason[:1000])

    def interrupted(self) -> None:
        self.queue.update_serving(self.id, state=INTERRUPTED)

    def recheck_failed(self, reason: str) -> None:
        record = self.serving()
        extra = dict(record.extra) if record is not None else {}
        extra["recheck_failed"] = reason
        self.queue.update_serving(self.id, extra=extra)

    def last_delivered_for(self, channel: str, topic: str) -> Serving | None:
        """The newest delivered serving of the conversation `channel/topic`
        as home, before this one, whatever route brought it — what the
        continuation view reads its input boundary and last reply from."""
        with self.queue._lock:
            row = self.queue._db.execute(
                "SELECT * FROM servings WHERE home_channel = ? AND home_topic = ? AND state = ? AND id < ?"
                " ORDER BY id DESC LIMIT 1", (channel, topic, DELIVERED, self.id)).fetchone()
        return None if row is None else Queue._serving(row)

    def previous(self) -> Serving | None:
        """The newest interrupted record of the same conversation before
        this one — the evidence the run may need to reconcile."""
        record = self.serving()
        if record is None:
            return None
        with self.queue._lock:
            row = self.queue._db.execute(
                "SELECT * FROM servings WHERE channel = ? AND topic = ? AND route = ? AND state = ? AND id < ?"
                " ORDER BY id DESC LIMIT 1", (*record.key, INTERRUPTED, self.id)).fetchone()
        return None if row is None else Queue._serving(row)


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
        max_attempts: int = MAX_ATTEMPTS,
        retry_seconds: float = RETRY_SECONDS,
        delivery: dict | None = None,
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
        self.max_attempts = int(max_attempts)
        self.retry_seconds = float(retry_seconds)
        #: Keyword overrides for `agag.delivery.deliver` on redelivery.
        self.delivery = dict(delivery or {})
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
        """Who really spoke last: not a selfnote, not a system notice — and
        not this bot's own ack either. The ack is transport: a conversation
        whose newest line is our ack is one we have *not* answered, whatever
        a last-poster check says (a crash after the ack used to hide it)."""
        for message in reversed(self.mirror.messages(channel, live_name, across_resolve=False, limit=60)):
            if not is_speech(message.as_zulip()):
                continue
            if message.sender_id == self.self_id and self.is_ack(message.content.strip()):
                continue
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
        if is_memo_channel(entry.channel):
            # Asked again here, not only at intake: the queue is a file, and
            # an entry may have been written before this rule existed.
            return None
        index = self._live(entry.channel, entry.topic)
        if index is None:
            return None
        if entry.route == OWNER:
            record = self.queue.latest_serving(entry.key, states=(DELIVERED,))
            if record is not None and record.input_up_to is not None:
                # The one completion rule, from the record: speech by
                # somebody else past the input boundary the last delivered
                # serving processed. Neither our ack nor our own later post
                # is evidence that *that* input was answered.
                from .topics import unprocessed_input

                history = [m.as_zulip() for m in self.mirror.messages(entry.channel, index.live_name,
                                                                       across_resolve=False)]
                return index.live_name if unprocessed_input(history, self.self_id, record.input_up_to) else None
        last = self._last_real(entry.channel, index.live_name)
        if last is None:
            # A listed topic holds messages; if none is speech, nobody spoke.
            return None
        if last.sender_id == self.self_id:
            return None
        if entry.route == MENTION:
            marks = self.served_marks() if marks is None else marks
            if self.unanswered_mention(entry.channel, index.live_name,
                                       marks.get((entry.channel, entry.topic), 0)) is None:
                return None
        return index.live_name

    def unanswered_mention(self, channel: str, live_name: str, mark: int) -> Message | None:
        """The newest post naming this bot above its served mark, or None.

        `argue` p1. A mention is an invitation, and it stays owed until this
        bot has marked the conversation served past it — not until the
        *last* post happens to be one that names this bot. Two agents named
        in one post, one of them answering, and a human posting after that
        left the other's invitation invisible to a last-post check; a
        restart in that state erased it. Judging by the newest unmarked
        mention keeps it, and the mark, once written, keeps a finished
        exchange from being replayed.
        """
        for message in reversed(self.mirror.messages(channel, live_name, across_resolve=False)):
            if message.id <= mark:
                return None
            if message.sender_id == self.self_id or not is_speech(message.as_zulip()):
                continue
            if mentions_bot(message.content, self.bot_name):
                return message
        return None

    # -- intake ----------------------------------------------------------------------------

    def _intake(self, change: Change) -> None:
        if change.kind != "resync" and is_memo_channel(change.channel):
            return  # a memo is read, never answered: no post, mention or rename there is work
        if change.kind == "message" and change.message_id is not None:
            message = self.mirror.message(change.message_id)
            if message is None or message.sender_id == self.self_id or is_selfnote(message.content):
                return
            if is_memo_channel(message.channel):
                return  # the change row may predate the channel's name; the message knows it
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
        if is_memo_channel(channel):
            return
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
            if is_memo_channel(index.channel):
                continue
            key = (index.channel, index.name)
            last = index.last_real
            if last is not None and last.sender_id == self.self_id and self.is_ack(last.content.strip()):
                last = self._last_real(index.channel, index.live_name)  # our ack answers nothing
            if last is None or last.sender_id == self.self_id:
                continue
            if topic_matches(index.channel, index.live_name, self.topic_filter):
                route, newest = OWNER, last.id
            elif self.on_mention is not None and index.max_id > marks.get(key, 0):
                mention = self.unanswered_mention(index.channel, index.live_name, marks.get(key, 0))
                if mention is None:
                    continue
                route, newest = MENTION, mention.id
            else:
                continue
            if self.queue.enqueue(index.channel, index.name, route, revision=revision, message_id=newest):
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
        """Entries a crash left `running`: every one is queued again and
        judged by the executor's one rule — what the conversation and the
        serving record show *now*. A record between `received` and
        `prepared` is marked `interrupted` so its evidence reaches the next
        serving; a `prepared` one is redelivered; a `delivered` one leaves
        nothing owed unless input arrived after it."""
        for entry in self.queue.entries("running"):
            if is_memo_channel(entry.channel):
                self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r} is a memo; dropped")
                self.queue.drop(entry)
                continue
            record = self.queue.latest_serving(entry.key)
            if record is not None and record.state in (RECEIVED, ACKED, EXECUTED):
                QueueJournal(self.queue, record.id).interrupted()
                self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r} was interrupted at {record.state!r}; "
                         f"its evidence is kept for the next serving")
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
                self._execute_one(entry)
            except Exception as error:  # noqa: BLE001 - one bad topic must not end the loop
                self._retry(entry, error)

    def _execute_one(self, entry: Entry) -> None:
        # A reply prepared and not confirmed comes first, whatever the
        # conversation looks like: it is the answer to input already
        # processed, and it is delivered — not regenerated — before anything
        # newer is looked at.
        record = self.queue.latest_serving(entry.key, states=(PREPARED, FAILED))
        if record is not None and record.reply_text and record.delivered_id is None \
                and not record.extra.get("terminal"):
            from .topics import resume_prepared

            journal = QueueJournal(self.queue, record.id)
            resume_prepared(self.client, record, journal, log=self.log, **self.delivery)
            self._after_delivery(entry, journal.serving())
            self.queue.requeue(entry)  # then judge what is owed now, once more
            return
        record = self.queue.latest_serving(entry.key, states=(DELIVERED,))
        if record is not None and entry.route == MENTION and not record.extra.get("served_marked"):
            # Delivered, and the restart came before the served mark: mark
            # it now, then judge again — the mark is what keeps the mention
            # from being served twice.
            self._after_delivery(entry, record)
            self.queue.requeue(entry)
            return
        live = self.owed(entry)
        if live is None:
            self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r}: nothing owed now; skipped")
            self._finish(entry)
            return
        self.log(f"{'serving' if entry.route == OWNER else 'serving mention in'} {entry.channel!r}/{live!r}")
        serve = self.handler if entry.route == OWNER else self.on_mention
        journal = self.queue.open_serving(entry)
        trigger = self.mirror.message(entry.message_id) if entry.message_id else None
        if trigger is not None:
            # Captured at intake: who asked, by the post that triggered this
            # serving. `serve_topic` refines it from the processed input.
            self.queue.update_serving(journal.id, requester_id=int(trigger.sender_id),
                                      requester_name=trigger.sender_name or "")
        with serving_record.bound(journal):
            serve(entry.channel, live)  # type: ignore[misc]
        self.served += 1
        record = journal.serving()
        if record is not None and record.state == DELIVERED:
            self._after_delivery(entry, record)
        elif record is not None and record.state == RECEIVED:
            # The handler did not go through `serve_topic` (a participant
            # that posts on its own): the record says only that it ran.
            journal.queue.update_serving(journal.id, state=EXECUTED)
        self._finish(entry)

    def _finish(self, entry: Entry) -> None:
        if self.queue.finish(entry):
            self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r}: an event arrived during the "
                     f"serving; looking again")

    def _retry(self, entry: Entry, error: BaseException) -> None:
        """A serving that raised out of the handler: the transport, not the
        work, failed (the handler's own failures are posted as replies).
        Bounded backoff; exhausted, the entry stays visible as `failed`."""
        terminal = isinstance(error, DeliveryError) and error.terminal
        reason = f"{type(error).__name__}: {error}"
        record = self.queue.latest_serving(entry.key)
        if terminal or entry.attempts >= self.max_attempts:
            self.queue.fail(entry, reason)
            if record is not None and record.state != DELIVERED:
                QueueJournal(self.queue, record.id).failed(reason)
                if terminal:
                    self.queue.update_serving(record.id, extra={**record.extra, "terminal": True})
            self.status.record_error(f"{entry.channel}/{entry.topic}: {reason}")
            self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r} FAILED after {entry.attempts} attempt(s)"
                     f"{' (terminal)' if terminal else ''}: {reason}; kept in the queue, re-armed by the next post")
            return
        delay = min(self.retry_seconds * (2 ** max(0, entry.attempts - 1)), RETRY_CAP_SECONDS)
        self.queue.retry_later(entry, delay, reason)
        self.log(f"{entry.route} {entry.channel!r}/{entry.topic!r} failed (attempt {entry.attempts}/"
                 f"{self.max_attempts}): {reason}; retrying in {delay:.0f}s")

    def _after_delivery(self, entry: Entry, record: Serving | None) -> None:
        """What follows a confirmed delivery on the mention route: the served
        mark, tied to the mention that was actually processed (`trigger_id`)
        — never to whatever is newest in the remote topic, so a mention that
        arrived during the run stays owed."""
        if record is None or entry.route != MENTION or record.extra.get("served_marked"):
            return
        if self.self_id is None or not record.trigger_id:
            return
        remote = Conversation(entry.channel, entry.topic)
        home = Conversation(record.home_channel, record.home_topic) if record.home_channel else remote
        try:
            live = live_topic_name(self.client, home.channel, home.topic)
            self.client.send_to_channel(home.channel, live, served_note(remote, record.trigger_id))
        except Exception as error:  # noqa: BLE001 - the mark is retried with the entry
            raise DeliveryError(f"could not write the served mark for {remote} in {home}: {error!r}",
                                terminal=False, last=error) from error
        extra = dict(record.extra)
        extra["served_marked"] = record.trigger_id
        self.queue.update_serving(record.id, extra=extra)
        self.log(f"marked {remote} served up to {record.trigger_id} in {home}")

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
