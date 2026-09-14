"""The mirror's store: every public message this credential can see, on disk.

SQLite, one file per consumer, stdlib only. The store is **recoverable
state, never a record**: Zulip is the conversation and the work record,
this is a local copy of it that a process reads instead of asking Zulip the
same questions every few seconds. Delete the file and the next start
rebuilds it from the realm; that is the whole migration strategy, and the
reason nothing in here has a version to be careful about.

What is kept, and why each thing is a table of its own:

- `messages` by **id**, with the channel and topic each message is in
  *now*. A message id is the one identifier in Zulip that no rename, move
  or resolve touches, which is why everything in this realm that has to
  survive a reused display name anchors to one (`refactor` p1–p3,
  `observer` p1 ex1). A move is an update of `topic` on the ids the event
  names; a delete is a flag, so a consumer that remembered an id learns it
  is gone rather than finding nothing.
- `topics` from the channel listing: the names Zulip currently has, with
  the newest id in each. This is what makes a topic with no hydrated
  history still *known* — and it is what a resync compares against without
  reading a single conversation.
- `coverage` per topic: whether the store holds the whole conversation or
  a window. A partial read must never be read as a short conversation; the
  flag is what a consumer checks before it decides anything from absence.
- `notes`: every `[selfnote][<tag>] <value>` line, indexed generically. The
  tags are each writer's own vocabulary (`rootchat`, `served`, `mission`,
  `watch`…), so the store knows the shape and not the meaning.
- `changes`: an append-only feed with a monotonic revision, so a consumer
  can ask "what moved since I last looked" and block until something does.
- `meta`: the queue checkpoint, committed **in the same transaction** as
  the events it covers. A crash between the two cannot happen, so a replay
  from the checkpoint is exactly the events not yet applied.

Every write goes through one connection under one lock; readers take the
same lock for the microseconds a query takes. The realm is small (a few
thousand messages) and simplicity is worth more here than concurrency.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from agag.selfnote import SELFNOTE_MARKER, is_speech
from agag.zulip import RESOLVED_TOPIC_PREFIX

#: One `[selfnote][tag] value` line. Indexed per line rather than per
#: message so a note written into an otherwise ordinary post is still found;
#: `agag.selfnote.parse_note` stays the strict, whole-message reader.
NOTE_LINE = re.compile(r"^\s*\[selfnote\]\[(?P<tag>[\w-]+)\]\s*(?P<value>.*?)\s*$")
#: Kinds a change row may carry.
CHANGE_KINDS = ("message", "edit", "move", "delete", "channel", "resync")
#: How many change rows are kept. A consumer that falls further behind than
#: this re-reads the index instead of the feed, which `changes()` says by
#: returning `None`.
CHANGE_MEMORY = 20000

__all__ = [
    "CHANGE_MEMORY",
    "Channel",
    "Change",
    "Coverage",
    "Message",
    "Note",
    "Store",
    "TopicRow",
    "bare_topic",
    "notes_of",
]


def bare_topic(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


@dataclass(frozen=True)
class Message:
    """One message as the store holds it — the Zulip fields the realm's
    readers actually use, plus where it is *now*."""

    id: int
    stream_id: int
    channel: str
    topic: str
    sender_id: int
    sender_name: str
    sender_realm: str
    timestamp: int
    content: str
    edited_at: int | None = None
    deleted: bool = False

    def as_zulip(self) -> dict:
        """The dict shape every existing reader in this realm expects
        (`display_recipient`, `subject`, `sender_full_name`…), so a consumer
        moved onto the mirror keeps its parsers unchanged."""
        return {
            "id": self.id,
            "type": "stream",
            "stream_id": self.stream_id,
            "display_recipient": self.channel,
            "subject": self.topic,
            "sender_id": self.sender_id,
            "sender_full_name": self.sender_name,
            "sender_realm_str": self.sender_realm,
            "timestamp": self.timestamp,
            "content": self.content,
            "last_edit_timestamp": self.edited_at,
        }

    @property
    def resolved(self) -> bool:
        return self.topic.startswith(RESOLVED_TOPIC_PREFIX)


@dataclass(frozen=True)
class Channel:
    stream_id: int
    name: str
    folder_id: int | None = None
    archived: bool = False
    description: str = ""


@dataclass(frozen=True)
class TopicRow:
    """One topic name as the channel listing has it, with the store's own
    count beside Zulip's `max_id`."""

    stream_id: int
    channel: str
    name: str
    max_id: int
    count: int = 0


@dataclass(frozen=True)
class Coverage:
    stream_id: int
    topic: str
    complete: bool
    oldest_id: int
    newest_id: int
    read_at: float


@dataclass(frozen=True)
class Note:
    tag: str
    value: str
    message_id: int
    sender_id: int
    stream_id: int
    channel: str
    topic: str
    timestamp: int = 0


@dataclass(frozen=True)
class Change:
    revision: int
    at: float
    kind: str
    stream_id: int | None
    channel: str
    topic: str
    message_id: int | None
    detail: dict = field(default_factory=dict)


def notes_of(content: str) -> list[tuple[str, str]]:
    """Every `(tag, value)` note line in a message body."""
    found: list[tuple[str, str]] = []
    for line in str(content or "").splitlines():
        if SELFNOTE_MARKER not in line:
            continue
        match = NOTE_LINE.match(line)
        if match:
            found.append((match.group("tag"), match.group("value")))
    return found


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS channels (
    stream_id INTEGER PRIMARY KEY, name TEXT NOT NULL, folder_id INTEGER,
    archived INTEGER NOT NULL DEFAULT 0, description TEXT NOT NULL DEFAULT '', seen_at REAL
);
CREATE INDEX IF NOT EXISTS channels_name ON channels (name);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, stream_id INTEGER NOT NULL, topic TEXT NOT NULL,
    sender_id INTEGER NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
    sender_realm TEXT NOT NULL DEFAULT '', timestamp INTEGER NOT NULL,
    content TEXT NOT NULL, edited_at INTEGER, deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS messages_topic ON messages (stream_id, topic, id);
CREATE INDEX IF NOT EXISTS messages_sender ON messages (sender_id, id);
CREATE TABLE IF NOT EXISTS topics (
    stream_id INTEGER NOT NULL, name TEXT NOT NULL, max_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stream_id, name)
);
CREATE TABLE IF NOT EXISTS coverage (
    stream_id INTEGER NOT NULL, topic TEXT NOT NULL, complete INTEGER NOT NULL,
    oldest_id INTEGER NOT NULL, newest_id INTEGER NOT NULL, read_at REAL NOT NULL,
    PRIMARY KEY (stream_id, topic)
);
CREATE TABLE IF NOT EXISTS notes (
    message_id INTEGER NOT NULL, tag TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (message_id, tag, value)
);
CREATE INDEX IF NOT EXISTS notes_tag ON notes (tag, message_id);
CREATE TABLE IF NOT EXISTS changes (
    revision INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
    stream_id INTEGER, topic TEXT NOT NULL DEFAULT '', message_id INTEGER, detail TEXT NOT NULL DEFAULT '{}'
);
"""


class Store:
    """The SQLite file, with the one lock every access takes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- transactions ------------------------------------------------------

    class _Transaction:
        def __init__(self, store: "Store"):
            self.store = store

        def __enter__(self):
            self.store._lock.acquire()
            self.store._db.execute("BEGIN IMMEDIATE")
            return self.store

        def __exit__(self, kind, value, traceback):
            try:
                if kind is None:
                    self.store._db.execute("COMMIT")
                else:
                    self.store._db.execute("ROLLBACK")
            finally:
                self.store._lock.release()
            return False

    def transaction(self) -> "Store._Transaction":
        """`with store.transaction(): …` — everything inside lands together
        or not at all. Nested use is not supported and not needed."""
        return Store._Transaction(self)

    # -- meta ----------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def checkpoint(self) -> tuple[str | None, int]:
        queue = self.get_meta("queue_id")
        last = self.get_meta("last_event_id")
        return queue, int(last) if last is not None else -1

    def set_checkpoint(self, queue_id: str | None, last_event_id: int) -> None:
        self.set_meta("queue_id", queue_id)
        self.set_meta("last_event_id", str(int(last_event_id)))

    # -- channels ------------------------------------------------------------

    def put_channels(self, rows: Iterable[dict], *, at: float | None = None) -> list[Change]:
        """Replace the channel table from a `GET /streams` answer (archived
        included). Returns the changes that were news."""
        at = time.time() if at is None else at
        news: list[Change] = []
        with self._lock:
            known = {row["stream_id"]: dict(row) for row in self._db.execute("SELECT * FROM channels")}
            seen: set[int] = set()
            for row in rows:
                stream_id = int(row["stream_id"])
                seen.add(stream_id)
                record = {
                    "stream_id": stream_id, "name": str(row.get("name") or ""),
                    "folder_id": row.get("folder_id"),
                    "archived": 1 if row.get("is_archived") else 0,
                    "description": str(row.get("description") or ""),
                }
                before = known.get(stream_id)
                self._db.execute(
                    "INSERT OR REPLACE INTO channels (stream_id, name, folder_id, archived, description, seen_at)"
                    " VALUES (:stream_id, :name, :folder_id, :archived, :description, :at)",
                    {**record, "at": at},
                )
                if before is None or any(before[key] != record[key] for key in ("name", "folder_id", "archived", "description")):
                    news.append(self._change("channel", stream_id, "", None, {
                        "name": record["name"], "archived": bool(record["archived"]),
                        "new": before is None,
                    }, at))
            # A channel the listing no longer has and that is not marked
            # archived is one this credential lost sight of (made private,
            # deleted outright). Say so rather than keep it live.
            for stream_id, before in known.items():
                if stream_id in seen or before["archived"]:
                    continue
                self._db.execute("UPDATE channels SET archived = 1, seen_at = ? WHERE stream_id = ?", (at, stream_id))
                news.append(self._change("channel", stream_id, "", None, {
                    "name": before["name"], "archived": True, "new": False, "vanished": True,
                }, at))
        return news

    def put_channel(self, row: dict, *, at: float | None = None) -> Change | None:
        """One channel from a `stream` event (create / update)."""
        at = time.time() if at is None else at
        stream_id = int(row["stream_id"])
        with self._lock:
            before = self._db.execute("SELECT * FROM channels WHERE stream_id = ?", (stream_id,)).fetchone()
            merged = {
                "stream_id": stream_id,
                "name": str(row.get("name") if row.get("name") is not None else (before["name"] if before else "")),
                "folder_id": row.get("folder_id", before["folder_id"] if before else None),
                "archived": 1 if row.get("is_archived", bool(before["archived"]) if before else False) else 0,
                "description": str(row.get("description", before["description"] if before else "") or ""),
            }
            self._db.execute(
                "INSERT OR REPLACE INTO channels (stream_id, name, folder_id, archived, description, seen_at)"
                " VALUES (:stream_id, :name, :folder_id, :archived, :description, :at)", {**merged, "at": at})
            if before is not None and all(before[key] == merged[key] for key in ("name", "folder_id", "archived", "description")):
                return None
            return self._change("channel", stream_id, "", None, {
                "name": merged["name"], "archived": bool(merged["archived"]), "new": before is None}, at)

    def channels(self, *, include_archived: bool = False) -> list[Channel]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM channels ORDER BY name").fetchall()
        found = [Channel(int(r["stream_id"]), r["name"], r["folder_id"], bool(r["archived"]), r["description"]) for r in rows]
        return found if include_archived else [c for c in found if not c.archived]

    def channel(self, name: str) -> Channel | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM channels WHERE name = ? ORDER BY archived LIMIT 1", (name,)).fetchone()
        return Channel(int(row["stream_id"]), row["name"], row["folder_id"], bool(row["archived"]), row["description"]) if row else None

    def channel_by_id(self, stream_id: int) -> Channel | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM channels WHERE stream_id = ?", (int(stream_id),)).fetchone()
        return Channel(int(row["stream_id"]), row["name"], row["folder_id"], bool(row["archived"]), row["description"]) if row else None

    def channel_names(self) -> dict[int, str]:
        with self._lock:
            return {int(r["stream_id"]): r["name"] for r in self._db.execute("SELECT stream_id, name FROM channels")}

    # -- topics (the listing) -----------------------------------------------

    def put_topics(self, stream_id: int, rows: Iterable[dict]) -> None:
        """Replace one channel's topic listing (`name`, `max_id`)."""
        with self._lock:
            self._db.execute("DELETE FROM topics WHERE stream_id = ?", (int(stream_id),))
            self._db.executemany(
                "INSERT OR REPLACE INTO topics (stream_id, name, max_id) VALUES (?, ?, ?)",
                [(int(stream_id), str(r["name"]), int(r.get("max_id") or 0)) for r in rows],
            )

    def listing(self, stream_id: int) -> dict[str, int]:
        with self._lock:
            return {r["name"]: int(r["max_id"]) for r in self._db.execute(
                "SELECT name, max_id FROM topics WHERE stream_id = ?", (int(stream_id),))}

    def touch_topic(self, stream_id: int, name: str, message_id: int) -> None:
        """Keep the listing current from events: a post raises `max_id`, a
        move creates the destination name."""
        with self._lock:
            row = self._db.execute("SELECT max_id FROM topics WHERE stream_id = ? AND name = ?",
                                   (int(stream_id), name)).fetchone()
            if row is None or int(row["max_id"]) < int(message_id):
                self._db.execute("INSERT OR REPLACE INTO topics (stream_id, name, max_id) VALUES (?, ?, ?)",
                                 (int(stream_id), name, int(message_id)))

    def refresh_topic_row(self, stream_id: int, name: str) -> None:
        """Recompute one listing row from the messages held: gone when the
        topic holds nothing any more (every message moved or deleted)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(id) AS max_id, COUNT(*) AS n FROM messages WHERE stream_id = ? AND topic = ? AND deleted = 0",
                (int(stream_id), name)).fetchone()
            if not row or not row["n"]:
                self._db.execute("DELETE FROM topics WHERE stream_id = ? AND name = ?", (int(stream_id), name))
            else:
                self._db.execute("INSERT OR REPLACE INTO topics (stream_id, name, max_id) VALUES (?, ?, ?)",
                                 (int(stream_id), name, int(row["max_id"])))

    # -- messages ------------------------------------------------------------

    @staticmethod
    def _row_to_message(row, channel: str) -> Message:
        return Message(
            id=int(row["id"]), stream_id=int(row["stream_id"]), channel=channel, topic=row["topic"],
            sender_id=int(row["sender_id"]), sender_name=row["sender_name"], sender_realm=row["sender_realm"],
            timestamp=int(row["timestamp"]), content=row["content"], edited_at=row["edited_at"],
            deleted=bool(row["deleted"]),
        )

    def put_message(self, message: dict, *, replace: bool = False, at: float | None = None) -> Change | None:
        """Insert one Zulip message dict. Without `replace`, an id already
        held is left exactly as it is — a `message` event replayed after a
        hydration that already saw the edited version must not regress it.
        With `replace` (a history read, which is always current) the row is
        overwritten and the difference, if any, is the change."""
        if message.get("type") not in (None, "stream"):
            return None
        at = time.time() if at is None else at
        ident = int(message["id"])
        stream_id = int(message.get("stream_id") or 0)
        topic = str(message.get("subject") or "")
        with self._lock:
            before = self._db.execute("SELECT * FROM messages WHERE id = ?", (ident,)).fetchone()
            if before is not None and not replace:
                return None
            record = {
                "id": ident, "stream_id": stream_id, "topic": topic,
                "sender_id": int(message.get("sender_id") or 0),
                "sender_name": str(message.get("sender_full_name") or ""),
                "sender_realm": str(message.get("sender_realm_str") or ""),
                "timestamp": int(message.get("timestamp") or 0),
                "content": str(message.get("content") or ""),
                "edited_at": message.get("last_edit_timestamp"),
                "deleted": 0,
            }
            if before is not None and all(
                (before[key] == record[key]) for key in ("stream_id", "topic", "content", "deleted", "edited_at")
            ):
                return None
            self._db.execute(
                "INSERT OR REPLACE INTO messages (id, stream_id, topic, sender_id, sender_name, sender_realm,"
                " timestamp, content, edited_at, deleted) VALUES (:id, :stream_id, :topic, :sender_id,"
                " :sender_name, :sender_realm, :timestamp, :content, :edited_at, :deleted)", record)
            self._index_notes(ident, record["content"])
            self.touch_topic(stream_id, topic, ident)
            if before is not None and (before["stream_id"] != stream_id or before["topic"] != topic):
                self.refresh_topic_row(int(before["stream_id"]), before["topic"])
                return self._change("move", stream_id, topic, ident, {
                    "from_stream_id": int(before["stream_id"]), "from_topic": before["topic"]}, at)
            if before is not None:
                return self._change("edit", stream_id, topic, ident, {}, at)
            return self._change("message", stream_id, topic, ident, {"sender_id": record["sender_id"]}, at)

    def edit_message(self, message_id: int, content: str, edited_at: int | None, *, at: float | None = None) -> Change | None:
        """A content edit from an `update_message` event. An edit older than
        the one already held is ignored, so a replayed event cannot regress a
        newer edit or a hydrated copy."""
        at = time.time() if at is None else at
        with self._lock:
            before = self._db.execute("SELECT * FROM messages WHERE id = ?", (int(message_id),)).fetchone()
            if before is None:
                return None
            held = before["edited_at"] or 0
            if edited_at is not None and int(edited_at) < int(held):
                return None
            if before["content"] == content and (edited_at is None or int(edited_at) == int(held)):
                return None
            self._db.execute("UPDATE messages SET content = ?, edited_at = ? WHERE id = ?",
                             (content, edited_at if edited_at is not None else held or None, int(message_id)))
            self._index_notes(int(message_id), content)
            return self._change("edit", int(before["stream_id"]), before["topic"], int(message_id), {}, at)

    def move_messages(self, message_ids: Iterable[int], *, stream_id: int | None, topic: str | None,
                      orig_topic: str | None = None, at: float | None = None) -> list[Change]:
        """A topic (and possibly channel) move from an `update_message`
        event. Idempotent: ids already where the event says are untouched."""
        at = time.time() if at is None else at
        news: list[Change] = []
        touched: set[tuple[int, str]] = set()
        with self._lock:
            for ident in message_ids:
                before = self._db.execute("SELECT * FROM messages WHERE id = ?", (int(ident),)).fetchone()
                if before is None:
                    continue
                new_stream = int(stream_id) if stream_id is not None else int(before["stream_id"])
                new_topic = topic if topic is not None else before["topic"]
                if new_stream == int(before["stream_id"]) and new_topic == before["topic"]:
                    continue
                self._db.execute("UPDATE messages SET stream_id = ?, topic = ? WHERE id = ?",
                                 (new_stream, new_topic, int(ident)))
                touched.add((int(before["stream_id"]), before["topic"]))
                self.touch_topic(new_stream, new_topic, int(ident))
                news.append(self._change("move", new_stream, new_topic, int(ident), {
                    "from_stream_id": int(before["stream_id"]), "from_topic": before["topic"]}, at))
            for old_stream, old_topic in touched:
                self.refresh_topic_row(old_stream, old_topic)
                # Coverage travels with the conversation: what was complete
                # under the old name is complete under the new one when the
                # whole topic moved (`change_all`, which is what a resolve is).
                row = self._db.execute("SELECT * FROM coverage WHERE stream_id = ? AND topic = ?",
                                       (old_stream, old_topic)).fetchone()
                still = self._db.execute(
                    "SELECT COUNT(*) AS n FROM messages WHERE stream_id = ? AND topic = ? AND deleted = 0",
                    (old_stream, old_topic)).fetchone()["n"]
                if row is not None and not still and topic is not None:
                    self._db.execute("DELETE FROM coverage WHERE stream_id = ? AND topic = ?", (old_stream, old_topic))
                    self._db.execute(
                        "INSERT OR REPLACE INTO coverage (stream_id, topic, complete, oldest_id, newest_id, read_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (int(stream_id) if stream_id is not None else old_stream, topic,
                         int(row["complete"]), int(row["oldest_id"]), int(row["newest_id"]), float(row["read_at"])))
        return news

    def delete_message(self, message_id: int, *, at: float | None = None) -> Change | None:
        at = time.time() if at is None else at
        with self._lock:
            before = self._db.execute("SELECT * FROM messages WHERE id = ?", (int(message_id),)).fetchone()
            if before is None or before["deleted"]:
                return None
            self._db.execute("UPDATE messages SET deleted = 1 WHERE id = ?", (int(message_id),))
            self._db.execute("DELETE FROM notes WHERE message_id = ?", (int(message_id),))
            self.refresh_topic_row(int(before["stream_id"]), before["topic"])
            return self._change("delete", int(before["stream_id"]), before["topic"], int(message_id), {}, at)

    def _index_notes(self, message_id: int, content: str) -> None:
        self._db.execute("DELETE FROM notes WHERE message_id = ?", (int(message_id),))
        self._db.executemany(
            "INSERT OR IGNORE INTO notes (message_id, tag, value) VALUES (?, ?, ?)",
            [(int(message_id), tag, value) for tag, value in notes_of(content)],
        )

    def message(self, message_id: int, *, include_deleted: bool = False) -> Message | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (int(message_id),)).fetchone()
            if row is None or (row["deleted"] and not include_deleted):
                return None
            names = self.channel_names()
        return self._row_to_message(row, names.get(int(row["stream_id"]), ""))

    def messages(self, stream_id: int, topic: str, *, since_id: int = 0, limit: int | None = None,
                 newest_first: bool = False) -> list[Message]:
        """One topic's messages, oldest first unless asked otherwise."""
        order = "DESC" if newest_first else "ASC"
        sql = ("SELECT * FROM messages WHERE stream_id = ? AND topic = ? AND deleted = 0 AND id > ?"
               f" ORDER BY id {order}")
        params: list = [int(stream_id), topic, int(since_id)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
            name = self.channel_names().get(int(stream_id), "")
        return [self._row_to_message(row, name) for row in rows]

    def messages_by_sender(self, sender_id: int, *, limit: int = 500) -> list[Message]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE sender_id = ? AND deleted = 0 ORDER BY id DESC LIMIT ?",
                (int(sender_id), int(limit))).fetchall()
            names = self.channel_names()
        return [self._row_to_message(row, names.get(int(row["stream_id"]), "")) for row in reversed(rows)]

    def message_ids(self, stream_id: int, topic: str | None = None) -> list[int]:
        with self._lock:
            if topic is None:
                rows = self._db.execute("SELECT id FROM messages WHERE stream_id = ? AND deleted = 0", (int(stream_id),))
            else:
                rows = self._db.execute("SELECT id FROM messages WHERE stream_id = ? AND topic = ? AND deleted = 0",
                                        (int(stream_id), topic))
            return [int(r["id"]) for r in rows]

    def newest_id(self, stream_id: int | None = None) -> int:
        with self._lock:
            if stream_id is None:
                row = self._db.execute("SELECT MAX(id) AS m FROM messages").fetchone()
            else:
                row = self._db.execute("SELECT MAX(id) AS m FROM messages WHERE stream_id = ?", (int(stream_id),)).fetchone()
        return int(row["m"] or 0)

    def counts(self) -> dict:
        with self._lock:
            messages = self._db.execute("SELECT COUNT(*) AS n FROM messages WHERE deleted = 0").fetchone()["n"]
            deleted = self._db.execute("SELECT COUNT(*) AS n FROM messages WHERE deleted = 1").fetchone()["n"]
            topics = self._db.execute("SELECT COUNT(*) AS n FROM topics").fetchone()["n"]
            channels = self._db.execute("SELECT COUNT(*) AS n FROM channels WHERE archived = 0").fetchone()["n"]
            complete = self._db.execute("SELECT COUNT(*) AS n FROM coverage WHERE complete = 1").fetchone()["n"]
            partial = self._db.execute("SELECT COUNT(*) AS n FROM coverage WHERE complete = 0").fetchone()["n"]
            notes = self._db.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
        return {"messages": messages, "deleted": deleted, "topics": topics, "channels": channels,
                "complete": complete, "partial": partial, "notes": notes}

    # -- the topic index -----------------------------------------------------

    def topic_rows(self, stream_id: int | None = None) -> list[TopicRow]:
        """Every listed topic with the store's own count for it."""
        with self._lock:
            names = self.channel_names()
            sql = ("SELECT t.stream_id, t.name, t.max_id,"
                   " (SELECT COUNT(*) FROM messages m WHERE m.stream_id = t.stream_id AND m.topic = t.name AND m.deleted = 0) AS n"
                   " FROM topics t")
            params: tuple = ()
            if stream_id is not None:
                sql += " WHERE t.stream_id = ?"
                params = (int(stream_id),)
            rows = self._db.execute(sql, params).fetchall()
        return [TopicRow(int(r["stream_id"]), names.get(int(r["stream_id"]), ""), r["name"], int(r["max_id"]), int(r["n"]))
                for r in rows]

    def last_real(self, stream_id: int, topic: str, *, lookback: int = 60) -> Message | None:
        """The newest message that is speech — not a selfnote, not a Zulip
        notice — or None. The predicate every "does this await me?" check is
        built on (`agag.selfnote`)."""
        for message in self.messages(stream_id, topic, limit=lookback, newest_first=True):
            if is_speech(message.as_zulip()):
                return message
        return None

    # -- coverage ------------------------------------------------------------

    def set_coverage(self, stream_id: int, topic: str, *, complete: bool, oldest_id: int, newest_id: int,
                     at: float | None = None) -> None:
        at = time.time() if at is None else at
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO coverage (stream_id, topic, complete, oldest_id, newest_id, read_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (int(stream_id), topic, 1 if complete else 0, int(oldest_id), int(newest_id), at))

    def coverage(self, stream_id: int, topic: str) -> Coverage | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM coverage WHERE stream_id = ? AND topic = ?",
                                   (int(stream_id), topic)).fetchone()
        return Coverage(int(row["stream_id"]), row["topic"], bool(row["complete"]), int(row["oldest_id"]),
                        int(row["newest_id"]), float(row["read_at"])) if row else None

    def clear_coverage(self, stream_id: int | None = None) -> None:
        with self._lock:
            if stream_id is None:
                self._db.execute("DELETE FROM coverage")
            else:
                self._db.execute("DELETE FROM coverage WHERE stream_id = ?", (int(stream_id),))

    # -- notes ---------------------------------------------------------------

    def notes(self, *, tag: str | None = None, sender_id: int | None = None, stream_id: int | None = None,
              topic: str | None = None, since_id: int = 0) -> list[Note]:
        sql = ("SELECT n.tag, n.value, n.message_id, m.sender_id, m.stream_id, m.topic, m.timestamp"
               " FROM notes n JOIN messages m ON m.id = n.message_id WHERE m.deleted = 0 AND n.message_id > ?")
        params: list = [int(since_id)]
        if tag is not None:
            sql += " AND n.tag = ?"
            params.append(tag)
        if sender_id is not None:
            sql += " AND m.sender_id = ?"
            params.append(int(sender_id))
        if stream_id is not None:
            sql += " AND m.stream_id = ?"
            params.append(int(stream_id))
        if topic is not None:
            sql += " AND m.topic = ?"
            params.append(topic)
        sql += " ORDER BY n.message_id"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
            names = self.channel_names()
        return [Note(r["tag"], r["value"], int(r["message_id"]), int(r["sender_id"]), int(r["stream_id"]),
                     names.get(int(r["stream_id"]), ""), r["topic"], int(r["timestamp"])) for r in rows]

    # -- the change feed -----------------------------------------------------

    def _change(self, kind: str, stream_id: int | None, topic: str, message_id: int | None, detail: dict,
                at: float) -> Change:
        cursor = self._db.execute(
            "INSERT INTO changes (at, kind, stream_id, topic, message_id, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (at, kind, stream_id, topic, message_id, json.dumps(detail, sort_keys=True)))
        revision = int(cursor.lastrowid)
        channel = ""
        if stream_id is not None:
            row = self._db.execute("SELECT name FROM channels WHERE stream_id = ?", (int(stream_id),)).fetchone()
            channel = row["name"] if row else ""
        return Change(revision, at, kind, stream_id, channel, topic, message_id, detail)

    def note_change(self, kind: str, detail: dict, *, at: float | None = None) -> Change:
        """A change that is not about one message: a resync boundary."""
        at = time.time() if at is None else at
        with self._lock:
            return self._change(kind, None, "", None, detail, at)

    def revision(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT MAX(revision) AS r FROM changes").fetchone()
        return int(row["r"] or 0)

    def change_times(self, kind: str) -> dict[tuple[int, str], float]:
        """`{(stream_id, topic): newest time}` of one kind of change — for a
        board that wants to know *when* a topic was resolved, which the
        realm itself does not record."""
        with self._lock:
            rows = self._db.execute(
                "SELECT stream_id, topic, MAX(at) AS at FROM changes WHERE kind = ? AND stream_id IS NOT NULL"
                " GROUP BY stream_id, topic", (kind,)).fetchall()
        return {(int(r["stream_id"]), r["topic"]): float(r["at"]) for r in rows}

    def oldest_revision(self) -> int:
        with self._lock:
            row = self._db.execute("SELECT MIN(revision) AS r FROM changes").fetchone()
        return int(row["r"] or 0)

    def changes(self, since: int, *, limit: int = 5000) -> list[Change] | None:
        """Changes newer than `since`, oldest first — or None when `since` is
        older than the feed remembers, which means "re-read the index"."""
        with self._lock:
            oldest = self.oldest_revision()
            if oldest > 1 and since < oldest - 1:
                return None
            rows = self._db.execute(
                "SELECT * FROM changes WHERE revision > ? ORDER BY revision LIMIT ?", (int(since), int(limit))).fetchall()
            names = self.channel_names()
        return [Change(int(r["revision"]), float(r["at"]), r["kind"], r["stream_id"],
                       names.get(int(r["stream_id"]), "") if r["stream_id"] is not None else "",
                       r["topic"], r["message_id"], json.loads(r["detail"] or "{}")) for r in rows]

    def prune_changes(self, keep: int = CHANGE_MEMORY) -> None:
        with self._lock:
            newest = self.revision()
            self._db.execute("DELETE FROM changes WHERE revision <= ?", (max(0, newest - int(keep)),))

    # -- bulk helpers used by a resync --------------------------------------

    def held_ids_by_channel(self, stream_id: int) -> dict[int, tuple[str, str, int | None]]:
        """`{id: (topic, content, edited_at)}` for every live message of one
        channel: what a deep resync diffs a fresh read against."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, topic, content, edited_at FROM messages WHERE stream_id = ? AND deleted = 0",
                (int(stream_id),)).fetchall()
        return {int(r["id"]): (r["topic"], r["content"], r["edited_at"]) for r in rows}

    def iter_all(self) -> Iterator[Message]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE deleted = 0 ORDER BY id").fetchall()
            names = self.channel_names()
        for row in rows:
            yield self._row_to_message(row, names.get(int(row["stream_id"]), ""))
