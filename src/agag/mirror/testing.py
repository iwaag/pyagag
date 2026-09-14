"""A Zulip realm in memory that speaks `agag.mirror.Transport`.

For tests — pyagag's own and every consumer's. Every mutation has a `quiet`
form that changes the realm without queueing an event, which is what
happens while a queue is expired and what a resync has to recover from.
"""

from __future__ import annotations

import threading

from agag.mirror import EVENT_TYPES
from agag.zulip import QueueExpired, RESOLVED_TOPIC_PREFIX

SELF = {"user_id": 42, "full_name": "Mirror Bot", "email": "mirror-bot@example"}

__all__ = ["SELF", "Facet", "FakeRealm"]


class FakeRealm:
    """A Zulip realm in memory, speaking the `Transport` protocol.

    Every mutation has a `quiet` form that changes the realm without
    queueing an event — what happens while a queue is expired.
    """

    def __init__(self):
        self.channels_by_id: dict[int, dict] = {}
        self.messages: dict[int, dict] = {}
        self.next_message_id = 100
        self.next_event_id = 0
        self.queue_id: str | None = None
        self.queue_generation = 0
        self.events: list[dict] = []
        self.expired = False
        self.calls = 0
        self.rate_limit_remaining = None
        self.rate_limit_reset = None
        self.purpose = ""
        self.ledger: dict = {}
        self.on_first_page = None
        self.replay_once = False
        self._condition = threading.Condition()
        self.log: list[str] = []

    # -- realm mutations -------------------------------------------------------

    def add_channel(self, stream_id: int, name: str, *, folder_id=None, archived=False, description=""):
        self.channels_by_id[stream_id] = {"stream_id": stream_id, "name": name, "folder_id": folder_id,
                                          "is_archived": archived, "description": description}

    def _event(self, event: dict) -> None:
        with self._condition:
            event["id"] = self.next_event_id
            self.next_event_id += 1
            self.events.append(event)
            self._condition.notify_all()

    def post(self, channel: str, topic: str, content: str, *, sender_id=7, sender_name="Dev",
             realm="", timestamp=None, quiet=False, flags=()) -> int:
        stream = next(c for c in self.channels_by_id.values() if c["name"] == channel)
        ident = self.next_message_id
        self.next_message_id += 1
        message = {"id": ident, "type": "stream", "stream_id": stream["stream_id"], "display_recipient": channel,
                   "subject": topic, "sender_id": sender_id, "sender_full_name": sender_name,
                   "sender_realm_str": realm, "timestamp": timestamp or (1000 + ident), "content": content}
        self.messages[ident] = message
        if not quiet:
            self._event({"type": "message", "message": dict(message), "flags": list(flags)})
        return ident

    def edit(self, message_id: int, content: str, *, quiet=False, when=None) -> None:
        message = self.messages[message_id]
        when = when or (2000 + message_id)
        message["content"] = content
        message["last_edit_timestamp"] = when
        if not quiet:
            self._event({"type": "update_message", "message_id": message_id, "message_ids": [message_id],
                         "content": content, "orig_content": "", "edit_timestamp": when,
                         "stream_id": message["stream_id"], "rendering_only": False})

    def move(self, message_ids: list[int], new_topic: str, *, quiet=False, propagate="change_all") -> None:
        first = self.messages[message_ids[0]]
        orig = first["subject"]
        for ident in message_ids:
            self.messages[ident]["subject"] = new_topic
        if not quiet:
            self._event({"type": "update_message", "message_id": message_ids[0], "message_ids": list(message_ids),
                         "orig_subject": orig, "subject": new_topic, "propagate_mode": propagate,
                         "stream_id": first["stream_id"], "rendering_only": False, "edit_timestamp": 3000})

    def resolve(self, channel: str, topic: str, *, quiet=False) -> None:
        ids = self._topic_ids(channel, topic)
        self.move(ids, f"{RESOLVED_TOPIC_PREFIX}{topic}", quiet=quiet)

    def delete(self, message_id: int, *, quiet=False) -> None:
        message = self.messages.pop(message_id)
        if not quiet:
            self._event({"type": "delete_message", "message_id": message_id, "stream_id": message["stream_id"],
                         "topic": message["subject"], "message_type": "stream"})

    def archive(self, stream_id: int, *, quiet=False) -> None:
        self.channels_by_id[stream_id]["is_archived"] = True
        if not quiet:
            self._event({"type": "stream", "op": "delete", "streams": [dict(self.channels_by_id[stream_id])]})

    def create_channel(self, stream_id: int, name: str, *, quiet=False, **kwargs) -> None:
        self.add_channel(stream_id, name, **kwargs)
        if not quiet:
            self._event({"type": "stream", "op": "create", "streams": [dict(self.channels_by_id[stream_id])]})

    def expire_queue(self) -> None:
        with self._condition:
            self.expired = True
            self._condition.notify_all()

    def _topic_ids(self, channel: str, topic: str) -> list[int]:
        stream = next(c for c in self.channels_by_id.values() if c["name"] == channel)
        return sorted(i for i, m in self.messages.items()
                      if m["stream_id"] == stream["stream_id"] and m["subject"] == topic)

    # -- the transport --------------------------------------------------------------
    #
    # `FakeRealm` is itself a transport, so a test that passes it as the
    # mirror's client factory works. But a mirror holds *two* clients — the
    # poller and the reader — and their call ledgers are what a test of "what
    # did this question cost" reads; `facet()` hands out a client with its
    # own counters over the same realm, so the poller's polls never land in
    # the reader's ledger.

    def facet(self) -> "Facet":
        return Facet(self)

    def _count(self, name: str) -> None:
        self.calls += 1
        key = (self.purpose, "GET", name)
        self.ledger[key] = self.ledger.get(key, 0) + 1
        self.log.append(name)

    def whoami(self, refresh: bool = False) -> dict:
        self._count("users/me")
        return self._whoami()

    def _whoami(self) -> dict:
        return dict(SELF)

    def register(self, event_types=None, *, all_public_streams=False, fetch_event_types=None):
        self._count("register")
        return self._register(event_types, all_public_streams=all_public_streams)

    def _register(self, event_types, *, all_public_streams):
        assert all_public_streams is True
        assert set(event_types) == set(EVENT_TYPES)
        with self._condition:
            self.queue_generation += 1
            self.queue_id = f"q{self.queue_generation}"
            self.expired = False
            self.events = []
            self.next_event_id = 0
        return self.queue_id, -1

    def poll(self, queue_id: str, last_event_id: int, *, dont_block: bool = False) -> list[dict]:
        self._count("events")
        return self._poll(queue_id, last_event_id, dont_block)

    def _poll(self, queue_id: str, last_event_id: int, dont_block: bool) -> list[dict]:
        with self._condition:
            if self.expired or queue_id != self.queue_id:
                raise QueueExpired("BAD_EVENT_QUEUE_ID")
            pending = [e for e in self.events if e["id"] > last_event_id]
            if self.replay_once and pending:
                self.replay_once = False
                return [dict(e) for e in self.events]
            if not pending and not dont_block:
                self._condition.wait(0.1)
                if self.expired or queue_id != self.queue_id:
                    raise QueueExpired("BAD_EVENT_QUEUE_ID")
                pending = [e for e in self.events if e["id"] > last_event_id]
            return [dict(e) for e in pending]

    def channels(self, *, include_archived: bool = False) -> list[dict]:
        self._count("streams")
        return self._channels(include_archived)

    def _channels(self, include_archived: bool) -> list[dict]:
        return [dict(c) for c in self.channels_by_id.values() if include_archived or not c["is_archived"]]

    def channel_topics_detail(self, stream_id: int) -> list[dict]:
        self._count("topics")
        return self._channel_topics_detail(stream_id)

    def _channel_topics_detail(self, stream_id: int) -> list[dict]:
        rows: dict[str, int] = {}
        for message in self.messages.values():
            if message["stream_id"] == stream_id:
                rows[message["subject"]] = max(rows.get(message["subject"], 0), message["id"])
        return [{"name": name, "max_id": max_id} for name, max_id in sorted(rows.items(), key=lambda r: -r[1])]

    def messages_page(self, narrow, *, anchor="newest", num_before=0, num_after=0, include_anchor=True) -> dict:
        self._count("messages")
        return self._messages_page(narrow, anchor, num_before, include_anchor)

    def _messages_page(self, narrow, anchor, num_before, include_anchor) -> dict:
        if self.on_first_page is not None:
            hook, self.on_first_page = self.on_first_page, None
            hook()
        channel = next(n["operand"] for n in narrow if n["operator"] == "channel")
        topic = next((n["operand"] for n in narrow if n["operator"] == "topic"), None)
        stream = next(c for c in self.channels_by_id.values() if c["name"] == channel)
        rows = sorted((m for m in self.messages.values()
                       if m["stream_id"] == stream["stream_id"] and (topic is None or m["subject"] == topic)),
                      key=lambda m: m["id"])
        if anchor != "newest":
            rows = [m for m in rows if (m["id"] <= anchor if include_anchor else m["id"] < anchor)]
        page = rows[-num_before:] if num_before else []
        oldest = rows[0]["id"] if rows else None
        return {"messages": [dict(m) for m in page],
                "found_oldest": bool(not page or page[0]["id"] == oldest),
                "found_newest": anchor == "newest"}

    def message(self, message_id: int, *, strict: bool = False) -> dict | None:
        self._count("message")
        return self._message(message_id)

    def _message(self, message_id: int) -> dict | None:
        found = self.messages.get(message_id)
        return dict(found) if found else None


class Facet:
    """One client over a shared `FakeRealm`: its own `calls`, `purpose` and
    `ledger`, the realm's state and log."""

    def __init__(self, realm: FakeRealm):
        self.realm = realm
        self.calls = 0
        self.purpose = ""
        self.ledger: dict = {}
        self.rate_limit_remaining = None
        self.rate_limit_reset = None

    @property
    def base_url(self) -> str:
        return str(getattr(self.realm, "base_url", "") or "")

    def _count(self, name: str) -> None:
        self.calls += 1
        key = (self.purpose, "GET", name)
        self.ledger[key] = self.ledger.get(key, 0) + 1
        self.realm.calls += 1
        self.realm.log.append(name)

    def whoami(self, refresh: bool = False) -> dict:
        self._count("users/me")
        return self.realm._whoami()

    def register(self, event_types=None, *, all_public_streams=False, fetch_event_types=None):
        self._count("register")
        return self.realm._register(event_types, all_public_streams=all_public_streams)

    def poll(self, queue_id: str, last_event_id: int, *, dont_block: bool = False) -> list[dict]:
        self._count("events")
        return self.realm._poll(queue_id, last_event_id, dont_block)

    def channels(self, *, include_archived: bool = False) -> list[dict]:
        self._count("streams")
        return self.realm._channels(include_archived)

    def channel_topics_detail(self, stream_id: int) -> list[dict]:
        self._count("topics")
        return self.realm._channel_topics_detail(stream_id)

    def messages_page(self, narrow, *, anchor="newest", num_before=0, num_after=0, include_anchor=True) -> dict:
        self._count("messages")
        return self.realm._messages_page(narrow, anchor, num_before, include_anchor)

    def message(self, message_id: int, *, strict: bool = False) -> dict | None:
        self._count("message")
        return self.realm._message(message_id)
