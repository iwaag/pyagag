"""`agag.mirror` — an event-updated local copy of the realm's public conversations.

`better_zulip_call` p1. Five readers in this system used to reconstruct the
same public conversations from Zulip independently, each time they were
asked (the agent room, the ops engine, the completion walk, every listener's
sweep, the Observer's tick), and none of them kept what it read. This is the
one reader they share: a `Store` on disk, an ingest thread that registers an
event queue **before** it reads history and then applies events as they
come, and a query API that answers from the copy.

The shape, and the reasons:

- **One mirror per process, on that process's own credential.** Not a
  host-wide service: a listener has to keep serving when the relay is down,
  every bot has a quota of its own so sharing a collector saves no quota,
  and the account-specific half (DMs, the `mentioned` flag) needs a queue
  per bot regardless. The mirror's queue is registered with
  `all_public_streams`, so it sees every public conversation without
  subscribing to anything (verified on this realm for a bot).
- **Zulip stays the record.** Nothing here is authoritative; the store is
  rebuilt from the realm whenever that is simpler than repairing it, and
  `health()` says how fresh and how complete the copy is so a consumer can
  decide whether to trust an absence.
- **Queue first, then history, then reconcile.** A message that lands while
  the history is being read is replayed from the queue rather than lost,
  and replay cannot duplicate or regress: a `message` event never overwrites
  an id already held, an edit older than the one held is dropped, and a
  move to where a message already is does nothing.
- **A queue that expired is a resync, and a resync is a deep read.** Recent
  messages alone cannot recover an edit or a deletion that happened while
  nobody was listening, so the whole realm is read again — by channel, in
  pages of up to a thousand — and diffed against the store: a held message
  the realm no longer returns is marked deleted, a changed one is updated.
  At this realm's size that is a few dozen calls, which is less than one of
  the old startup sweeps.
- **A normal restart reads nothing.** The queue id and the last event id
  are checkpointed with the events they cover; a process that comes back
  within Zulip's queue lifetime resumes the same queue and is live at once.

The store keeps messages by id with the topic each is in *now*, which is
what lets a consumer follow a conversation through a rename, a resolve or a
reused display name (`refactor` p1–p3): a name is how a topic is shown, an
id is what it is.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from agag.selfnote import is_speech
from agag.zulip import (
    QueueExpired,
    RateLimited,
    RETRY_SECONDS,
    RESOLVED_TOPIC_PREFIX,
    ZulipClient,
    ZulipError,
    ZulipRejected,
    ZulipTimeout,
    log as default_log,
    rate_limit_backoff,
)

from .store import Change, Channel, Coverage, Message, Note, Store, TopicRow, bare_topic

#: The event types a mirror asks for. `subscription` is not among them: a
#: bot registered for all public streams is told about channels through
#: `stream` events, and its own subscriptions are nobody's business here.
EVENT_TYPES = ("message", "update_message", "delete_message", "stream")
#: Messages per history page. Zulip accepts more; a thousand keeps one
#: answer well under a megabyte on this realm's message sizes.
PAGE_SIZE = 1000
#: Calls left in the credential's window below which a resync waits for the
#: window to slide rather than spend the last of it — the same reserve the
#: listeners keep (`agag.zulip.SWEEP_BUDGET_RESERVE`), smaller because the
#: resync is paged and cheap.
BUDGET_RESERVE = 20
#: A resync after a queue died must not become a hot loop against the realm.
RESYNC_BACKOFF = 30.0
#: The file name inside a consumer's store directory.
STORE_NAME = "mirror.sqlite"

__all__ = [
    "BUDGET_RESERVE",
    "Change",
    "Channel",
    "Coverage",
    "EVENT_TYPES",
    "Intro",
    "Message",
    "Mirror",
    "Note",
    "PAGE_SIZE",
    "STORE_NAME",
    "Store",
    "TopicIndex",
    "TopicRow",
    "Transport",
    "bare_topic",
]


class Transport(Protocol):  # pragma: no cover - structural typing only
    """What the mirror needs from a Zulip client. `ZulipClient` is one."""

    calls: int
    rate_limit_remaining: float | None
    rate_limit_reset: float | None
    purpose: str
    ledger: dict

    def whoami(self, refresh: bool = ...) -> dict: ...
    def register(self, event_types=..., *, all_public_streams=..., fetch_event_types=...) -> tuple[str, int]: ...
    def poll(self, queue_id: str, last_event_id: int, *, dont_block: bool = ...) -> list[dict]: ...
    def channels(self, *, include_archived: bool = ...) -> list[dict]: ...
    def channel_topics_detail(self, stream_id: int) -> list[dict]: ...
    def messages_page(self, narrow: list[dict], *, anchor=..., num_before=..., num_after=..., include_anchor=...) -> dict: ...
    def message(self, message_id: int, *, strict: bool = ...) -> dict | None: ...


@dataclass(frozen=True)
class TopicIndex:
    """One topic as the index answers it: the listing's name and newest id,
    the store's own count and coverage, and who really spoke last."""

    channel: str
    stream_id: int
    name: str
    live_name: str
    resolved: bool
    max_id: int
    count: int
    complete: bool
    first_id: int = 0
    last_id: int = 0
    last_real: Message | None = None

    @property
    def key(self) -> tuple[str, str]:
        """`(channel, bare name)` — the key every board in this realm uses."""
        return (self.channel, self.name)


@dataclass(frozen=True)
class Intro:
    """One agent's introduction as the mirror reads it off `#agents`."""

    instance: str
    channel: str
    topic: str
    retired: bool
    message: Message | None
    roster: object | None
    history: list[Message] = field(default_factory=list)


class Mirror:
    """The engine and the query door, one object.

    `Mirror.open(env_path, store_dir)` is the whole setup; `start()` runs the
    ingest thread, and every query below answers from the store without a
    Zulip call unless it says otherwise (`hydrate`, `verify`).
    """

    def __init__(
        self,
        store: Store,
        client_factory: Callable[[], Transport],
        *,
        log=default_log,
        page_size: int = PAGE_SIZE,
        budget_reserve: float = BUDGET_RESERVE,
        resync_backoff: float = RESYNC_BACKOFF,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.client_factory = client_factory
        self.log = log
        self.page_size = int(page_size)
        self.budget_reserve = float(budget_reserve)
        self.resync_backoff = float(resync_backoff)
        self.clock = clock
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poller: Transport | None = None
        self._reader: Transport | None = None
        self._live = False
        self._reason = "not started"
        self._stale_since: float | None = None
        self._last_event_at: float | None = None
        self._last_resync_at: float | None = None
        self._resyncs = 0
        self._resync_calls = 0
        self._resumed = False
        self._error: str | None = None
        self._hydrating: dict[tuple[int, str], threading.Event] = {}
        self._index_cache: tuple[int, list[TopicIndex]] | None = None
        self._self: dict | None = None

    # -- construction --------------------------------------------------------

    @classmethod
    def open(
        cls,
        env_path: Path,
        store_dir: Path,
        *,
        client_factory: Callable[[], Transport] | None = None,
        log=default_log,
        start: bool = True,
        **kwargs,
    ) -> "Mirror":
        """A mirror for the credential at `env_path`, stored under `store_dir`.

        The store remembers which account built it; a different account
        finds an empty store, because what one credential can see is not
        what another can.
        """
        env_path = Path(env_path)
        factory = client_factory or (lambda: ZulipClient.from_env(env_path))
        store = Store(Path(store_dir) / STORE_NAME)
        mirror = cls(store, factory, log=log, **kwargs)
        if start:
            mirror.start()
        return mirror

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="mirror", daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        with self._lock:
            self._thread = None

    def close(self) -> None:
        self.stop()
        self.store.close()

    @property
    def self_id(self) -> int | None:
        found = self._self or {}
        return int(found["user_id"]) if found.get("user_id") is not None else None

    @property
    def bot_name(self) -> str:
        return str((self._self or {}).get("full_name") or "")

    @property
    def base_url(self) -> str:
        """The realm's URL, for links; empty until a client exists."""
        client = self._poller or self._reader
        return str(getattr(client, "base_url", "") or "")

    def resolved_times(self) -> dict[tuple[str, str], float]:
        """`{(channel, live ✔ name): when}` for every resolve this mirror
        watched happen — the only record of *when* a topic was resolved."""
        names = self.store.channel_names()
        return {(names.get(stream_id, ""), topic): at
                for (stream_id, topic), at in self.store.change_times("move").items()
                if topic.startswith(RESOLVED_TOPIC_PREFIX)}

    def _client(self) -> Transport:
        if self._poller is None:
            self._poller = self.client_factory()
        return self._poller

    def _reader_client(self) -> Transport:
        if self._reader is None:
            self._reader = self.client_factory()
        return self._reader

    # -- the ingest thread ----------------------------------------------------

    def _run(self) -> None:
        strikes = 0
        while not self._stop.is_set():
            try:
                client = self._client()
                if self._self is None:
                    profile = client.whoami()
                    self._self = dict(profile)
                    held = self.store.get_meta("email")
                    if held and held != str(profile.get("email") or ""):
                        # Another account's copy: not this credential's view.
                        self.log(f"mirror store was built by {held}; rebuilding for {profile.get('email')}")
                        self._forget_everything()
                    self.store.set_meta("email", str(profile.get("email") or ""))
                    self.store.set_meta("self_id", str(profile.get("user_id")))
                queue_id, last_event_id = self.store.checkpoint()
                if queue_id is not None:
                    # A persisted queue: is it still alive? One non-blocking
                    # poll answers, and applies what was queued meanwhile.
                    try:
                        client.purpose = "poll"
                        events = client.poll(queue_id, last_event_id, dont_block=True)
                    except QueueExpired:
                        self.log(f"mirror queue {queue_id} is gone; re-registering and resyncing")
                        queue_id = None
                    else:
                        last_event_id = self._apply_batch(queue_id, last_event_id, events)
                        self._set_live("live (resumed queue)")
                        self._resumed = True
                if queue_id is None:
                    client.purpose = "register"
                    queue_id, last_event_id = client.register(
                        list(EVENT_TYPES), all_public_streams=True, fetch_event_types=[])
                    self.log(f"mirror registered queue {queue_id} (last_event_id={last_event_id})")
                    self._resync(client)
                    with self.store.transaction():
                        self.store.set_checkpoint(queue_id, last_event_id)
                    self._set_live("live")
                self._poll_forever(client, queue_id, last_event_id)
                strikes = 0
            except QueueExpired:
                self._set_stale("event queue expired; resyncing")
                with self.store.transaction():
                    self.store.set_checkpoint(None, -1)
                continue
            except RateLimited as limited:
                strikes += 1
                delay = rate_limit_backoff(limited.retry_after, strikes)
                self._set_stale(f"rate limited: {limited}")
                self.log(f"mirror rate limited: {limited}; waiting {delay:.0f}s")
                self._stop.wait(delay)
            except ZulipTimeout:
                continue
            except Exception as error:  # noqa: BLE001 - the thread outlives its errors
                self._set_stale(f"{type(error).__name__}: {error}")
                self.log(f"mirror error: {error!r}; retrying in {self.resync_backoff:.0f}s")
                self._stop.wait(self.resync_backoff)

    def _poll_forever(self, client: Transport, queue_id: str, last_event_id: int) -> None:
        while not self._stop.is_set():
            client.purpose = "poll"
            try:
                events = client.poll(queue_id, last_event_id)
            except ZulipTimeout:
                continue
            last_event_id = self._apply_batch(queue_id, last_event_id, events)
            if events:
                self._last_event_at = self.clock()
            self._set_live("live")

    def _apply_batch(self, queue_id: str, last_event_id: int, events: list[dict]) -> int:
        """Apply one poll's events and the checkpoint in one transaction."""
        if not events:
            return last_event_id
        with self.store.transaction():
            for event in events:
                last_event_id = max(last_event_id, int(event.get("id", last_event_id)))
                try:
                    self._apply(event)
                except Exception as error:  # noqa: BLE001 - one bad event must not stop the feed
                    self.log(f"mirror could not apply event {event.get('type')}#{event.get('id')}: {error!r}")
            self.store.set_checkpoint(queue_id, last_event_id)
        self._notify()
        return last_event_id

    def _apply(self, event: dict) -> None:
        kind = event.get("type")
        at = self.clock()
        if kind == "message":
            message = event.get("message") or {}
            if message.get("type") != "stream":
                return
            change = self.store.put_message(message, at=at)
            if change is not None and "mentioned" in (event.get("flags") or []):
                change.detail["mentioned"] = True
            # The first message of a topic makes the topic, so a topic born
            # on this queue is complete by construction.
            stream_id, topic = int(message.get("stream_id") or 0), str(message.get("subject") or "")
            if change is not None and self.store.coverage(stream_id, topic) is None:
                if len(self.store.message_ids(stream_id, topic)) == 1:
                    self.store.set_coverage(stream_id, topic, complete=True,
                                            oldest_id=int(message["id"]), newest_id=int(message["id"]), at=at)
        elif kind == "update_message":
            if event.get("rendering_only"):
                return
            ids = [int(i) for i in (event.get("message_ids") or [event.get("message_id")]) if i is not None]
            moved = "orig_subject" in event or event.get("new_stream_id") is not None
            if moved:
                stream_id = event.get("new_stream_id")
                self.store.move_messages(
                    ids,
                    stream_id=int(stream_id) if stream_id is not None else (
                        int(event["stream_id"]) if event.get("stream_id") is not None else None),
                    topic=event.get("subject"), orig_topic=event.get("orig_subject"), at=at)
            if "content" in event and event.get("message_id") is not None:
                self.store.edit_message(int(event["message_id"]), str(event.get("content") or ""),
                                        event.get("edit_timestamp"), at=at)
        elif kind == "delete_message":
            ids = event.get("message_ids") or ([event["message_id"]] if event.get("message_id") is not None else [])
            for ident in ids:
                self.store.delete_message(int(ident), at=at)
        elif kind == "stream":
            op = event.get("op")
            if op in ("create", "delete"):
                for row in event.get("streams") or []:
                    record = dict(row)
                    if op == "delete":
                        record["is_archived"] = True
                    self.store.put_channel(record, at=at)
            elif op == "update" and event.get("stream_id") is not None:
                record = {"stream_id": event["stream_id"]}
                prop = event.get("property")
                if prop in ("name", "description", "folder_id", "is_archived"):
                    record[prop] = event.get("value")
                self.store.put_channel(record, at=at)

    # -- the deep resync ------------------------------------------------------

    def _wait_for_budget(self, client: Transport) -> None:
        remaining = getattr(client, "rate_limit_remaining", None)
        if remaining is None or remaining >= self.budget_reserve:
            return
        reset = getattr(client, "rate_limit_reset", None)
        delay = max(1.0, float(reset) - self.clock()) if reset else 15.0
        self.log(f"mirror resync waiting {delay:.0f}s for the quota window ({remaining:.0f} left)")
        self._stop.wait(min(delay, 90.0))

    def _patient(self, client: Transport, call, *args, **kwargs):
        for attempt in range(4):
            self._wait_for_budget(client)
            try:
                return call(*args, **kwargs)
            except RateLimited as limited:
                if attempt == 3:
                    raise
                self._stop.wait(max(1.0, limited.retry_after))
        raise RuntimeError("unreachable")

    def _read_channel(self, client: Transport, channel: Channel) -> list[dict]:
        """Every message of one channel, newest page first, until Zulip says
        it has no older ones."""
        found: list[dict] = []
        anchor: int | str = "newest"
        include_anchor = True
        narrow = [{"operator": "channel", "operand": channel.name}]
        while True:
            page = self._patient(
                client, client.messages_page, narrow,
                anchor=anchor, num_before=self.page_size, num_after=0, include_anchor=include_anchor)
            messages = page.get("messages") or []
            found.extend(messages)
            if page.get("found_oldest") or not messages:
                break
            anchor = min(int(m["id"]) for m in messages)
            include_anchor = False
        return found

    def _resync(self, client: Transport) -> None:
        """Read the realm whole and reconcile the store with it.

        Channels, then each channel's listing, then each channel's messages
        in pages. Everything read is written with `replace`, so an edit that
        happened while nobody listened is corrected; every held message the
        realm did not return anywhere is marked deleted.
        """
        started = self.clock()
        before = client.calls
        client.purpose = "resync"
        channels = self._patient(client, client.channels, include_archived=True)
        with self.store.transaction():
            self.store.put_channels(channels, at=started)
        seen: set[int] = set()
        for channel in self.store.channels():
            if self._stop.is_set():
                return
            listing = self._patient(client, client.channel_topics_detail, channel.stream_id)
            messages = self._read_channel(client, channel)
            with self.store.transaction():
                self.store.put_topics(channel.stream_id, listing)
                by_topic: dict[str, list[int]] = {}
                for message in messages:
                    self.store.put_message(message, replace=True, at=started)
                    ident = int(message["id"])
                    seen.add(ident)
                    by_topic.setdefault(str(message.get("subject") or ""), []).append(ident)
                self.store.clear_coverage(channel.stream_id)
                for topic, ids in by_topic.items():
                    self.store.set_coverage(channel.stream_id, topic, complete=True,
                                            oldest_id=min(ids), newest_id=max(ids), at=started)
        with self.store.transaction():
            for message in list(self.store.iter_all()):
                if message.id not in seen:
                    held = self.store.channel_by_id(message.stream_id)
                    if held is not None and held.archived:
                        continue  # an archived channel is not read; its copy is kept as it was
                    self.store.delete_message(message.id, at=started)
            self.store.note_change("resync", {"calls": client.calls - before,
                                              "seconds": round(self.clock() - started, 1)}, at=started)
            self.store.prune_changes()
        with self._lock:
            self._resyncs += 1
            self._resync_calls = client.calls - before
            self._last_resync_at = self.clock()
            self._index_cache = None
        self._notify()
        self.log(f"mirror resync: {len(seen)} messages in {self.store.counts()['channels']} channels, "
                 f"{client.calls - before} calls, {self.clock() - started:.1f}s")

    def resync(self) -> None:
        """Read the realm again, now, on the reader client. For an operator
        or a test; the ingest thread does it by itself when its queue dies."""
        self._resync(self._reader_client())

    def _forget_everything(self) -> None:
        path = self.store.path
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(path) + suffix).unlink()
            except FileNotFoundError:
                pass
        self.store = Store(path)

    # -- state ------------------------------------------------------------------

    def _set_live(self, reason: str) -> None:
        with self._lock:
            self._live = True
            self._reason = reason
            self._stale_since = None
            self._error = None

    def _set_stale(self, reason: str) -> None:
        with self._lock:
            if self._live or self._stale_since is None:
                self._stale_since = self.clock()
            self._live = False
            self._reason = reason
            self._error = reason

    def _notify(self) -> None:
        with self._lock:
            self._index_cache = None
            self._condition.notify_all()

    def health(self) -> dict:
        """Freshness and completeness, for a consumer deciding what to trust."""
        with self._lock:
            live, reason, stale_since = self._live, self._reason, self._stale_since
            resyncs, resync_calls = self._resyncs, self._resync_calls
            last_event_at, last_resync_at = self._last_event_at, self._last_resync_at
        queue_id, last_event_id = self.store.checkpoint()
        ledger: dict[str, int] = {}
        for client in (self._poller, self._reader):
            for (purpose, method, endpoint), n in (getattr(client, "ledger", None) or {}).items():
                key = f"{purpose or '-'} {method} {endpoint}"
                ledger[key] = ledger.get(key, 0) + n
        counts = self.store.counts()
        return {
            "state": "live" if live else "stale",
            "reason": reason,
            "stale_since": stale_since,
            "queue": queue_id,
            "last_event_id": last_event_id,
            "last_event_at": last_event_at,
            "last_resync_at": last_resync_at,
            "resyncs": resyncs,
            "resync_calls": resync_calls,
            "revision": self.store.revision(),
            "self_id": self.self_id,
            "bot": self.bot_name,
            "counts": counts,
            "calls": sum((getattr(c, "calls", 0) or 0) for c in (self._poller, self._reader) if c is not None),
            "ledger": dict(sorted(ledger.items())),
        }

    @property
    def live(self) -> bool:
        with self._lock:
            return self._live

    # -- queries: channels ---------------------------------------------------------

    def channels(self, *, include_archived: bool = False) -> list[Channel]:
        return self.store.channels(include_archived=include_archived)

    def channel(self, name: str) -> Channel | None:
        return self.store.channel(name)

    # -- queries: topics -------------------------------------------------------------

    def _build_index(self) -> list[TopicIndex]:
        found: list[TopicIndex] = []
        for row in self.store.topic_rows():
            coverage = self.store.coverage(row.stream_id, row.name)
            ids = self.store.message_ids(row.stream_id, row.name)
            found.append(TopicIndex(
                channel=row.channel, stream_id=row.stream_id, name=bare_topic(row.name), live_name=row.name,
                resolved=row.name.startswith(RESOLVED_TOPIC_PREFIX), max_id=row.max_id, count=len(ids),
                complete=bool(coverage and coverage.complete),
                first_id=min(ids) if ids else 0, last_id=max(ids) if ids else 0,
                last_real=self.store.last_real(row.stream_id, row.name) if ids else None,
            ))
        found.sort(key=lambda t: (t.channel, t.name, t.resolved))
        return found

    def topics(self, channel: str | None = None, *, include_resolved: bool = True) -> list[TopicIndex]:
        """Every listed topic, from a per-revision cache."""
        revision = self.store.revision()
        with self._lock:
            cached = self._index_cache
            if cached is None or cached[0] != revision:
                cached = (revision, self._build_index())
                self._index_cache = cached
        rows = cached[1]
        if channel is not None:
            rows = [t for t in rows if t.channel == channel]
        if not include_resolved:
            rows = [t for t in rows if not t.resolved]
        return list(rows)

    def topic(self, channel: str, name: str) -> list[TopicIndex]:
        """The topics whose bare name is `name` — the open one, the ✔ one,
        or both when a post after a resolve opened a twin."""
        bare = bare_topic(name)
        return [t for t in self.topics(channel) if t.name == bare]

    def live_name(self, channel: str, name: str) -> str | None:
        """The name a conversation can be read under now: the open one when
        it exists, else the ✔ one, else None."""
        found = self.topic(channel, name)
        if not found:
            return None
        open_ones = [t for t in found if not t.resolved]
        return (open_ones or found)[0].live_name

    # -- queries: messages -------------------------------------------------------------

    def messages(self, channel: str, topic: str, *, since_id: int = 0, limit: int | None = None,
                 hydrate: bool = False, across_resolve: bool = True) -> list[Message]:
        """A conversation's messages, oldest first, from the store.

        `across_resolve` merges the bare and the ✔ names, which is what
        every reader in this realm means by "the conversation". `hydrate`
        reads Zulip first when the store's coverage of it is not complete.
        """
        found = self.store.channel(channel)
        if found is None:
            return []
        names = [topic]
        if across_resolve:
            bare = bare_topic(topic)
            names = [bare, f"{RESOLVED_TOPIC_PREFIX}{bare}"]
        merged: dict[int, Message] = {}
        for name in names:
            if hydrate:
                coverage = self.store.coverage(found.stream_id, name)
                # A listed topic without complete coverage, or any topic of
                # an archived channel: the resync never reads archived
                # channels (there are more of them than live ones and nothing
                # changes in them), so their conversations are hydrated the
                # first time somebody asks and kept for good.
                listed = any(t.live_name == name for t in self.topic(channel, name))
                if not (coverage and coverage.complete) and (listed or found.archived):
                    self.hydrate(channel, name)
            for message in self.store.messages(found.stream_id, name, since_id=since_id):
                merged[message.id] = message
        rows = [merged[i] for i in sorted(merged)]
        if limit is not None and len(rows) > limit:
            rows = rows[-limit:]
        return rows

    def history(self, channel: str, topic: str, num_before: int = 50, **kwargs) -> list[dict]:
        """`ZulipClient.topic_history`'s shape (Zulip dicts, newest last),
        answered from the store: the drop-in for every reader that parses
        message dicts."""
        return [m.as_zulip() for m in self.messages(channel, topic, limit=num_before, **kwargs)]

    def message(self, message_id: int) -> Message | None:
        return self.store.message(message_id)

    def last_real(self, channel: str, topic: str) -> Message | None:
        for message in reversed(self.messages(channel, topic, limit=60)):
            if is_speech(message.as_zulip()):
                return message
        return None

    def verify(self, message_id: int) -> Message | None:
        """One targeted read of Zulip for a message, folded into the store.

        The check a consumer makes right before something terminal — a
        notification, a resolve — where event lag is not an acceptable
        reason to be wrong. `None` means Zulip **said** it is gone; a lookup
        that got no answer raises, exactly as `ZulipClient.message(strict=True)`.
        """
        client = self._reader_client()
        client.purpose = "verify"
        found = client.message(int(message_id), strict=True)
        with self.store.transaction():
            if found is None:
                self.store.delete_message(int(message_id), at=self.clock())
            else:
                self.store.put_message(found, replace=True, at=self.clock())
        self._notify()
        return None if found is None else self.store.message(int(message_id))

    def hydrate(self, channel: str, topic: str) -> Coverage | None:
        """Read one topic whole from Zulip under exactly the name given.

        Single-flight: a second caller for the same topic waits for the first
        read rather than making its own. Returns the coverage afterwards, or
        None when the channel is unknown to the store.
        """
        found = self.store.channel(channel)
        if found is None:
            return None
        key = (found.stream_id, topic)
        with self._lock:
            waiting = self._hydrating.get(key)
            if waiting is None:
                waiting = threading.Event()
                self._hydrating[key] = waiting
                owner = True
            else:
                owner = False
        if not owner:
            waiting.wait(120.0)
            return self.store.coverage(*key)
        try:
            client = self._reader_client()
            client.purpose = "hydrate"
            narrow = [{"operator": "channel", "operand": channel}, {"operator": "topic", "operand": topic}]
            messages: list[dict] = []
            anchor: int | str = "newest"
            include_anchor = True
            while True:
                page = self._patient(client, client.messages_page, narrow, anchor=anchor,
                                     num_before=self.page_size, num_after=0, include_anchor=include_anchor)
                rows = page.get("messages") or []
                messages.extend(rows)
                if page.get("found_oldest") or not rows:
                    break
                anchor = min(int(m["id"]) for m in rows)
                include_anchor = False
            at = self.clock()
            with self.store.transaction():
                held = set(self.store.message_ids(found.stream_id, topic))
                ids = []
                for message in messages:
                    self.store.put_message(message, replace=True, at=at)
                    ids.append(int(message["id"]))
                for ident in held - set(ids):
                    # Held under this name and not returned: it moved or it
                    # is gone. A verify would say which; a hydrate only knows
                    # it is not here, and marks it so the index is honest.
                    self.store.delete_message(ident, at=at)
                if ids:
                    self.store.set_coverage(found.stream_id, topic, complete=True,
                                            oldest_id=min(ids), newest_id=max(ids), at=at)
                else:
                    self.store.refresh_topic_row(found.stream_id, topic)
                    if found.archived:
                        # Nothing there, and nothing can ever be: remember
                        # the empty answer so the name is not read again.
                        self.store.set_coverage(found.stream_id, topic, complete=True,
                                                oldest_id=0, newest_id=0, at=at)
            self._notify()
            return self.store.coverage(*key)
        finally:
            with self._lock:
                self._hydrating.pop(key, None)
            waiting.set()

    # -- queries: notes and introductions -------------------------------------------

    def notes(self, *, tag: str | None = None, sender_id: int | None = None, channel: str | None = None,
              topic: str | None = None, since_id: int = 0) -> list[Note]:
        stream_id = None
        if channel is not None:
            found = self.store.channel(channel)
            if found is None:
                return []
            stream_id = found.stream_id
        return self.store.notes(tag=tag, sender_id=sender_id, stream_id=stream_id, topic=topic, since_id=since_id)

    def intros(self, channel: str = "agents", prefix: str = "intro-") -> dict[str, Intro]:
        """Every agent's introduction: the newest post of each `intro-`
        topic, parsed for its roster block, retired when the topic is ✔."""
        from agag.intro import parse_roster

        found: dict[str, Intro] = {}
        for index in self.topics(channel):
            if not index.name.startswith(prefix):
                continue
            instance = index.name[len(prefix):]
            history = self.messages(channel, index.live_name, across_resolve=False)
            posts = [m for m in history if is_speech(m.as_zulip())]
            newest = posts[-1] if posts else None
            current = found.get(instance)
            # An open topic outranks a ✔ twin of the same name.
            if current is not None and not current.retired and index.resolved:
                continue
            found[instance] = Intro(
                instance=instance, channel=channel, topic=index.live_name, retired=index.resolved,
                message=newest, roster=parse_roster(newest.content) if newest else None, history=posts,
            )
        return found

    # -- the change feed --------------------------------------------------------------

    def revision(self) -> int:
        return self.store.revision()

    def changes(self, since: int, *, limit: int = 5000) -> list[Change] | None:
        return self.store.changes(since, limit=limit)

    def wait(self, since: int, timeout: float | None = None) -> int:
        """Block until the revision passes `since` (or `timeout` elapses);
        returns the revision now."""
        deadline = None if timeout is None else self.clock() + timeout
        with self._condition:
            while True:
                revision = self.store.revision()
                if revision > since or self._stop.is_set():
                    return revision
                remaining = None if deadline is None else max(0.0, deadline - self.clock())
                if remaining == 0.0:
                    return revision
                self._condition.wait(remaining if remaining is not None else 30.0)
