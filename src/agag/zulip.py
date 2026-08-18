"""Shared Zulip chat entrance: a stdlib-only bot client and a DM listener loop.

Lifted out of agforge, where the receive side was first proven. Nothing here
knows about a particular agent: a consumer supplies its own credentials file
and its own handler, and gets the mechanics that took an episode to get right.

Three of those mechanics are worth naming, because each one was a bug:

- the identity (`whoami`) lookup sits *inside* the retry loop, so a listener
  survives a Zulip restart instead of dying on the first call;
- `http.client.RemoteDisconnected` escapes `urlopen` unwrapped, so the client
  catches `HTTPException`/`OSError` too;
- realms can hide real email addresses from events, so everything the receive
  side keys on is a numeric user id.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import shlex
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path

from agag.status import StatusWriter, default_status_path

# Long-poll socket timeout. Zulip holds the connection open until an event or
# its own heartbeat; this is only the client-side ceiling.
POLL_TIMEOUT_SECONDS = 90

# How long the listener waits after a failed Zulip call before trying again.
RETRY_SECONDS = 5

# Rate-limit backoff. A 429 is not a broken connection: retrying hard is the
# disease, not the cure (see the lighter_agag_listen episode). The server tells
# us how long to wait; these bound the wait when it does not, and when 429s
# keep coming.
RATE_LIMIT_DEFAULT_SECONDS = 60
RATE_LIMIT_MAX_SECONDS = 300
RATE_LIMIT_JITTER_FRACTION = 0.1

# A full sweep costs `1 + channels + matching topics` calls. When the quota
# window has less than this left, the sweep waits for the window to slide
# rather than spending its last requests halfway through.
SWEEP_BUDGET_RESERVE = 40

# Zulip's resolved-topic marker: the topic is renamed to "✔ <topic>".
RESOLVED_TOPIC_PREFIX = "✔ "

# Environment names used by the small outbound convenience function below.
ZULIP_ENV_PATH = "ZULIP_ENV"
ZULIP_CHANNEL = "ZULIP_CHANNEL"


class ZulipError(Exception):
    """A Zulip API call failed for a reason the caller cannot ignore."""


class QueueExpired(ZulipError):
    """The event queue is gone (BAD_EVENT_QUEUE_ID). Re-register and continue."""


class ZulipTimeout(ZulipError):
    """The call hit the client-side timeout. On a long poll this is normal."""


class RateLimited(ZulipError):
    """HTTP 429. Nothing is wrong with the queue — only wait, then continue.

    `retry_after` is what the server asked for, in seconds; callers still
    apply their own floor and backoff on top of it.
    """

    def __init__(self, message: str, retry_after: float = RATE_LIMIT_DEFAULT_SECONDS):
        super().__init__(message)
        self.retry_after = float(retry_after)


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive_float(value) -> float | None:
    """Header values that only mean something above zero, like a wait length."""
    seconds = _float_or_none(value)
    return seconds if seconds is not None and seconds > 0 else None


def retry_after_seconds(headers, body: dict | None = None) -> float:
    """How long the server wants us to wait, from whatever it told us.

    `Retry-After` first (Zulip sends it on a 429), then `x-ratelimit-reset`,
    then the `retry-after` field Zulip also puts in the JSON error body, then
    a conservative default.
    """
    get = getattr(headers, "get", None)
    if get is not None:
        for name in ("Retry-After", "x-ratelimit-reset"):
            seconds = _positive_float(get(name))
            if seconds is not None:
                return seconds
    if body:
        seconds = _positive_float(body.get("retry-after"))
        if seconds is not None:
            return seconds
    return float(RATE_LIMIT_DEFAULT_SECONDS)


def rate_limit_backoff(retry_after: float, strikes: int, jitter=None) -> float:
    """Seconds to sleep after the `strikes`-th consecutive 429.

    Never shorter than what the server asked for and never shorter than the
    ordinary retry, doubled per consecutive strike up to the ceiling, plus
    jitter so listeners sharing one quota do not resynchronise.
    """
    jitter = jitter if jitter is not None else random.uniform
    base = max(float(retry_after), float(RETRY_SECONDS))
    delay = min(base * (2 ** max(strikes - 1, 0)), float(RATE_LIMIT_MAX_SECONDS))
    return delay + jitter(0.0, delay * RATE_LIMIT_JITTER_FRACTION)


def read_env(path: Path) -> dict[str, str]:
    """Read KEY=value lines without sourcing shell code."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as error:
        raise ZulipError(f"no Zulip credentials at {path}") from error
    env: dict[str, str] = {}
    for line in lines:
        tokens = shlex.split(line, comments=True)
        if len(tokens) == 1 and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            env[key] = value
    return env


class ZulipClient:
    """HTTP Basic bot client. One instance is safe for one polling thread."""

    def __init__(self, base_url: str, email: str, api_key: str, ca_bundle: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._auth = b64encode(f"{email}:{api_key}".encode("utf-8")).decode("ascii")
        # Budget visibility: every Zulip response carries the quota headers, so
        # knowing what is left before spending it is free.
        self.calls = 0
        self.rate_limit_remaining: float | None = None
        self.rate_limit_limit: float | None = None
        if ca_bundle:
            self._ssl = ssl.create_default_context(cafile=ca_bundle)
        else:
            # Self-hosted deployments commonly use a self-signed certificate
            # and there is no trust store to point at. Set ZULIP_CA_BUNDLE in
            # the credentials file once one exists.
            self._ssl = ssl._create_unverified_context()

    @classmethod
    def from_env(cls, path: Path) -> "ZulipClient":
        """Build a client from a `KEY=value` credentials file.

        Required keys: `ZULIP_URL`, `ZULIP_EMAIL`, `ZULIP_API_KEY`.
        Optional: `ZULIP_CA_BUNDLE`.
        """
        env = read_env(path)
        missing = [k for k in ("ZULIP_URL", "ZULIP_EMAIL", "ZULIP_API_KEY") if not env.get(k)]
        if missing:
            raise ZulipError(f"{path} is missing {', '.join(missing)}")
        return cls(
            env["ZULIP_URL"], env["ZULIP_EMAIL"], env["ZULIP_API_KEY"],
            ca_bundle=env.get("ZULIP_CA_BUNDLE") or None,
        )

    def call(
        self, method: str, path: str, params: dict | None = None, timeout: float = 30
    ) -> dict:
        query = urllib.parse.urlencode(
            {k: v if isinstance(v, str) else json.dumps(v) for k, v in (params or {}).items()}
        )
        url = f"{self.base_url}/api/v1/{path.lstrip('/')}"
        data = None
        if method in ("POST", "PATCH", "DELETE"):
            data = query.encode("utf-8")
        elif query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Basic {self._auth}")
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        self.calls += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._ssl) as response:
                self._record_budget(response.headers)
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            self._record_budget(error.headers)
            body = error.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            if error.code == 429:
                # Its own class: the caller must wait, not reconnect.
                delay = retry_after_seconds(error.headers, parsed)
                detail = (parsed or {}).get("msg") or body[:200]
                raise RateLimited(
                    f"{method} {path} -> HTTP 429: {detail} (retry after {delay:.0f}s)",
                    retry_after=delay,
                ) from error
            if parsed is None:
                raise ZulipError(f"{method} {path} -> HTTP {error.code}: {body[:200]}") from error
            if parsed.get("code") == "BAD_EVENT_QUEUE_ID":
                raise QueueExpired(parsed.get("msg", "bad event queue id")) from error
            raise ZulipError(f"{method} {path} -> HTTP {error.code}: {parsed.get('msg')}") from error
        except TimeoutError as error:
            raise ZulipTimeout(f"{method} {path} timed out after {timeout}s") from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise ZulipTimeout(f"{method} {path} timed out after {timeout}s") from error
            raise ZulipError(f"{method} {path} -> {error}") from error
        except (http.client.HTTPException, OSError) as error:
            # urlopen does not wrap a connection dropped while the response is
            # being read; a Zulip restart during a long poll lands here.
            raise ZulipError(f"{method} {path} -> {error!r}") from error

    def _record_budget(self, headers) -> None:
        """Remember the quota headers Zulip puts on every response."""
        get = getattr(headers, "get", None)
        if get is None:
            return
        remaining = _float_or_none(get("x-ratelimit-remaining"))
        if remaining is not None:
            self.rate_limit_remaining = remaining
        limit = _float_or_none(get("x-ratelimit-limit"))
        if limit is not None:
            self.rate_limit_limit = limit

    # --- the four mechanics the receive side needs -------------------------

    def whoami(self) -> dict:
        return self.call("GET", "users/me")

    def register(self) -> tuple[str, int]:
        result = self.call("POST", "register", {"event_types": ["message"]})
        return result["queue_id"], int(result["last_event_id"])

    def poll(self, queue_id: str, last_event_id: int) -> list[dict]:
        """Block until events arrive. Raises QueueExpired when the queue died."""
        result = self.call(
            "GET", "events",
            {"queue_id": queue_id, "last_event_id": str(last_event_id)},
            timeout=POLL_TIMEOUT_SECONDS,
        )
        return result.get("events", [])

    def deregister(self, queue_id: str) -> None:
        self.call("DELETE", "events", {"queue_id": queue_id})

    def dm_history(self, user_ids: list[int], num_before: int = 50) -> list[dict]:
        """The DM conversation as the participants see it, newest last, raw text.

        `user_ids` are the other participants; the bot itself is implicit.
        Emails are avoided on purpose: a realm can hide them from events.
        """
        result = self.call(
            "GET", "messages",
            {
                "anchor": "newest",
                "num_before": str(num_before),
                "num_after": "0",
                "apply_markdown": "false",
                "narrow": [{"operator": "dm", "operand": user_ids}],
            },
        )
        return result.get("messages", [])

    def send_dm(self, user_ids: list[int], content: str) -> int:
        result = self.call(
            "POST", "messages",
            {"type": "direct", "to": user_ids, "content": content},
        )
        return int(result["id"])

    # --- channel/topic mechanics (zulip_channel_topic episode) --------------

    def create_channel(
        self,
        name: str,
        description: str,
        principals: list[int],
        announce: bool = False,
        folder_id: int | None = None,
    ) -> dict:
        """Create (or join) a public channel and subscribe `principals` to it.

        Zulip's subscribe call creates the channel when the name is new; a
        default-role bot may do this on this realm (proven in Step 1). The
        response says who was newly subscribed vs already subscribed.

        `folder_id` places a newly created channel into a channel folder
        (Zulip 11.0+). It only applies at creation: joining an existing
        channel leaves that channel's folder alone.
        """
        params: dict = {
            "subscriptions": [{"name": name, "description": description}],
            "principals": principals,
            "announce": announce,
        }
        if folder_id is not None:
            params["folder_id"] = folder_id
        return self.call("POST", "users/me/subscriptions", params)

    def channel_folders(self) -> list[dict]:
        """Channel folders in the realm, unarchived only, each with its `id`."""
        return self.call("GET", "channel_folders").get("channel_folders", [])

    def create_channel_folder(self, name: str, description: str = "") -> int:
        """Create a channel folder and return its id.

        Not idempotent — Zulip rejects a duplicate name — so look the name up
        in `channel_folders()` first when the folder may already exist.
        """
        result = self.call(
            "POST", "channel_folders/create",
            {"name": name, "description": description},
        )
        return int(result["channel_folder_id"])

    def channels(self) -> list[dict]:
        """Public channels visible to this bot."""
        return self.call("GET", "streams").get("streams", [])

    def subscriptions(self) -> list[dict]:
        """Channels to which this bot is currently subscribed."""
        return self.call("GET", "users/me/subscriptions").get("subscriptions", [])

    def users(self) -> list[dict]:
        """Realm members, bots included, active and deactivated alike."""
        return self.call("GET", "users").get("members", [])

    def channel_subscribers(self, stream_id: int) -> list[int]:
        """User ids currently subscribed to one channel."""
        return self.call("GET", f"streams/{stream_id}/members").get("subscribers", [])

    def subscribe_channels(self, names: list[str], principals: list[int] | None = None) -> dict:
        """Subscribe this bot, or `principals`, to existing channels by name.

        Subscribing other users needs no special role on this realm; a
        default-role bot may do it for a public channel.
        """
        if not names:
            return {"subscribed": [], "already_subscribed": []}
        params: dict = {"subscriptions": [{"name": name} for name in names]}
        if principals is not None:
            params["principals"] = principals
        return self.call("POST", "users/me/subscriptions", params)

    def unsubscribe_channels(self, names: list[str], principals: list[int] | None = None) -> dict:
        """Unsubscribe this bot, or `principals`, from channels by name.

        The counterpart of `subscribe_channels`, and the only way a listener's
        sweep cost ever goes down: a finished experiment's channel keeps
        costing every startup sweep a call until somebody leaves it.
        """
        if not names:
            return {"removed": [], "not_removed": []}
        params: dict = {"subscriptions": names}
        if principals is not None:
            params["principals"] = principals
        return self.call("DELETE", "users/me/subscriptions", params)

    def archive_channel(self, stream_id: int) -> dict:
        """Archive one channel; its messages and topics survive the move.

        Zulip calls the operation `DELETE streams/<id>`, but it archives
        rather than deletes: the channel leaves every channel listing and
        stops costing a sweep, and an organization administrator can still
        reach its history and unarchive it. The caller must be able to
        administer the channel — creating it is one way to qualify.
        """
        return self.call("DELETE", f"streams/{stream_id}")

    def send_to_channel(self, channel: str, topic: str, content: str) -> int:
        result = self.call(
            "POST", "messages",
            {"type": "stream", "to": channel, "topic": topic, "content": content},
        )
        return int(result["id"])

    def topic_history(self, channel: str, topic: str, num_before: int = 50) -> list[dict]:
        """The topic's conversation, newest last, raw text — `dm_history`'s
        channel analog."""
        result = self.call(
            "GET", "messages",
            {
                "anchor": "newest",
                "num_before": str(num_before),
                "num_after": "0",
                "apply_markdown": "false",
                "narrow": [
                    {"operator": "channel", "operand": channel},
                    {"operator": "topic", "operand": topic},
                ],
            },
        )
        return result.get("messages", [])

    def stream_id(self, name: str) -> int:
        """Resolve a channel name to Zulip's numeric stream id."""
        return int(self.call("GET", "get_stream_id", {"stream": name})["stream_id"])

    def channel_topics(self, stream_id: int) -> list[str]:
        """Topic names in one channel, newest first, resolved ones included."""
        result = self.call("GET", f"users/me/{stream_id}/topics")
        return [str(row["name"]) for row in result.get("topics", [])]

    def resolve_topic(self, message_id: int, topic: str) -> None:
        """Mark a topic resolved (Zulip's ✔ rename), moving every message in
        it — other senders' included, which this realm permits for bots."""
        if topic.startswith(RESOLVED_TOPIC_PREFIX):
            return
        self.call(
            "PATCH", f"messages/{message_id}",
            {
                "topic": f"{RESOLVED_TOPIC_PREFIX}{topic}",
                "propagate_mode": "change_all",
                "send_notification_to_new_thread": False,
            },
        )


def _safe_topic_component(value: str, label: str) -> str:
    """Keep a Zulip display name as one local path component."""
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"{label} must be a non-empty path component")
    return value


def topic_dump(
    channel: str,
    topic: str,
    chatlog: str,
    *,
    cwd: Path | None = None,
) -> str:
    """Write a numbered, local-only snapshot of one topic conversation.

    This is deliberately non-idempotent: every trigger preserves a new
    conversation version instead of replacing evidence from an earlier run.
    """
    channel = _safe_topic_component(channel, "channel")
    topic = _safe_topic_component(topic, "topic")
    root = (cwd or Path.cwd()) / ".local" / "topics" / channel / topic
    root.mkdir(parents=True, exist_ok=True)
    number = 1
    while True:
        version = root / str(number)
        try:
            version.mkdir()
        except FileExistsError:
            number += 1
            continue
        break
    relative = Path(".local") / "topics" / channel / topic / str(number) / "chatlog.txt"
    (version / "chatlog.txt").write_text(chatlog, encoding="utf-8")
    return f"{relative.as_posix()} is the log of a chat you are participating in."


def topic_write(
    topic: str,
    text: str,
    *,
    channel: str | None = None,
    env_path: Path | None = None,
    client: ZulipClient | None = None,
) -> str:
    """Write text to a topic and return the stable success marker.

    `topic` and `text` are the user-facing arguments. The transport context is
    supplied by an existing listener client, or by `ZULIP_CHANNEL` and
    `ZULIP_ENV` when used as a standalone helper.
    """
    destination = channel or os.environ.get(ZULIP_CHANNEL)
    if not destination:
        raise ZulipError(f"channel is required (argument or {ZULIP_CHANNEL})")
    if client is None:
        credentials = env_path or (
            Path(os.environ[ZULIP_ENV_PATH]) if os.environ.get(ZULIP_ENV_PATH) else None
        )
        if credentials is None:
            raise ZulipError(f"credentials path is required (argument or {ZULIP_ENV_PATH})")
        client = ZulipClient.from_env(credentials)
    client.send_to_channel(destination, topic, text)
    return "success"


def dm_partners(message: dict, self_id: int) -> list[int]:
    """Everyone in the DM except the bot, in Zulip's own order."""
    recipients = message.get("display_recipient")
    if not isinstance(recipients, list):
        return []
    return [r["id"] for r in recipients if isinstance(r, dict) and r.get("id") != self_id]


def is_dm_for_us(message: dict, self_id: int) -> bool:
    """A private message from somebody else. The bot's own DMs echo back."""
    return message.get("type") == "private" and message.get("sender_id") != self_id


def is_channel_message_for_us(message: dict, self_id: int) -> bool:
    """A channel (stream) message from somebody else, in any channel the bot
    is subscribed to. Which channels *matter* is the caller's rule."""
    return message.get("type") == "stream" and message.get("sender_id") != self_id


def channel_name(message: dict) -> str:
    """The channel a stream message was sent to ('' for DMs)."""
    recipient = message.get("display_recipient")
    return recipient if isinstance(recipient, str) else ""


def topic_from_event(
    message: dict, self_id: int, topic_filter: str | tuple[str, ...]
) -> tuple[str, str] | None:
    """The `(channel, topic)` a message event points at, if it can await us.

    A hint, not a verdict: the same rules `sweep_topics` applies to a topic
    name, read off the event payload instead of off a channel listing. Whether
    the topic *actually* awaits a reply is decided by reading its last message,
    which is one call rather than a whole sweep.
    """
    if not is_channel_message_for_us(message, self_id):
        return None
    topic = str(message.get("subject") or "")
    if not topic or not topic.startswith(topic_filter):
        return None
    if topic.startswith(RESOLVED_TOPIC_PREFIX):
        return None
    channel = channel_name(message)
    return (channel, topic) if channel else None


def log(message: str) -> None:
    """Default listener log line: UTC-stamped, unbuffered, on stderr."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def serve(client: ZulipClient, handler, log=log, accept=is_dm_for_us, status=None) -> None:
    """Long-poll for messages addressed to this bot and pass each to `handler`.

    `handler(client, message, self_id)` is called for every message `accept`
    lets through (DMs only by default; pass a wider predicate to also see
    channel messages); anything it raises is logged and the loop continues.
    Deliberately dumb: no queue persistence, no delivery guarantees. A message
    that arrives while this is down is lost and the sender can resend.

    `status` is a `StatusWriter`; by default one at `agag.status`'s default
    path, so a listener started in its workspace is observable without being
    configured. Its file is rewritten only after a poll that actually
    returned — see `agag/status.py` for why that is the whole honesty rule.
    """
    status = status if status is not None else StatusWriter(default_status_path(), log=log)
    self_id: int | None = None
    queue_id: str | None = None
    last_event_id = -1
    strikes = 0
    while True:
        try:
            if self_id is None:
                # Inside the loop on purpose: Zulip restarts, and a listener
                # that dies of that is a listener someone has to babysit.
                self_id = int(client.whoami()["user_id"])
                log(f"listening as user_id={self_id} ({client.email})")
            if queue_id is None:
                queue_id, last_event_id = client.register()
                log(f"registered event queue {queue_id} (last_event_id={last_event_id})")
            events = client.poll(queue_id, last_event_id)
        except ZulipTimeout as error:
            status.record_error(str(error))
            continue  # nothing happened within the poll window
        except QueueExpired as error:
            status.record_error(str(error))
            log(f"event queue expired ({error}); re-registering")
            queue_id = None
            continue
        except RateLimited as error:
            # Before the generic arm on purpose: keep the queue, just wait.
            strikes += 1
            delay = rate_limit_backoff(error.retry_after, strikes)
            status.record_error(str(error))
            log(f"rate limited: {error}; backing off {delay:.1f}s (strike {strikes}, queue kept)")
            time.sleep(delay)
            continue
        except ZulipError as error:
            status.record_error(str(error))
            log(f"zulip call failed: {error}; retrying in {RETRY_SECONDS}s")
            queue_id = None
            time.sleep(RETRY_SECONDS)
            continue
        strikes = 0
        status.record_poll_ok(queue_id)
        for event in events:
            last_event_id = max(last_event_id, int(event.get("id", last_event_id)))
            if event.get("type") != "message":
                continue
            message = event.get("message") or {}
            if not accept(message, self_id):
                continue
            try:
                handler(client, message, self_id)
            except Exception as error:  # one bad message must not end the loop
                log(f"handler failed on message #{message.get('id')}: {error!r}")


def sweep_topics(
    client: ZulipClient, self_id: int, topic_filter: str | tuple[str, ...]
) -> list[tuple[str, str]]:
    """Every `(channel, topic)` currently awaiting this bot's reply.

    A topic qualifies when it is in a channel this bot is subscribed to, its
    name passes `topic_filter` (prefix match — a tuple matches any of its
    prefixes, like `str.startswith`), it is not resolved, and its
    last poster is somebody else. The last-poster rule is what makes the pull
    loop self-stabilizing: the bot's own ack or reply silences a topic until
    a human speaks again.
    """
    matches: list[tuple[str, str]] = []
    for subscription in client.subscriptions():
        channel = str(subscription.get("name", ""))
        stream = subscription.get("stream_id")
        if not channel or stream is None:
            continue
        for topic in client.channel_topics(int(stream)):
            if not topic.startswith(topic_filter):
                continue
            if topic.startswith(RESOLVED_TOPIC_PREFIX):
                continue
            history = client.topic_history(channel, topic, num_before=1)
            if history and history[-1].get("sender_id") == self_id:
                continue
            matches.append((channel, topic))
    return matches


def sweep_serve(
    client: ZulipClient, handler, *, topic_filter: str | tuple[str, ...], log=log, status=None
) -> None:
    """Pull-based listener: poll for message events and serve the topics that
    await a reply. `handler(channel, topic)` is called once per topic;
    anything it raises is logged and the loop goes on.

    An event is a *hint*: it names a `(channel, topic)`, which joins a pending
    set, and the topic's real state is then read with one
    `topic_history(num_before=1)` call before the handler is invoked. So the
    steady-state cost of an incoming message is about one API call, and a
    burst on one topic coalesces in the set for free. The "don't trust the
    event payload" discipline is kept — the payload only says *where* to look.

    A **full** `sweep_topics` pass runs on every queue (re-)registration —
    startup and `QueueExpired` recovery — which is what makes downtime
    lossless: a post that arrived while this was down is found by the startup
    sweep, with no queue persistence involved. That pass is expensive
    (`1 + channels + matching topics` calls), so it waits for the quota window
    to slide when fewer than `SWEEP_BUDGET_RESERVE` requests are left.

    The loop is single-threaded and serial, so a long handler simply delays
    the next poll; events keep queueing meanwhile. `status` behaves exactly as
    in `serve`.
    """
    status = status if status is not None else StatusWriter(default_status_path(), log=log)
    self_id: int | None = None
    queue_id: str | None = None
    last_event_id = -1
    pending: set[tuple[str, str]] = set()
    full_sweep = False
    strikes = 0
    while True:
        try:
            if self_id is None:
                self_id = int(client.whoami()["user_id"])
                log(f"sweeping as user_id={self_id} ({client.email})")
            if queue_id is None:
                queue_id, last_event_id = client.register()
                log(f"registered event queue {queue_id} (last_event_id={last_event_id})")
                full_sweep = True  # anything may have happened while unregistered
            if full_sweep:
                remaining = getattr(client, "rate_limit_remaining", None)
                if remaining is not None and remaining < SWEEP_BUDGET_RESERVE:
                    # Not an error and not a sleep: fall through to the long
                    # poll, which both waits and refreshes the quota headers.
                    log(f"full sweep deferred: {remaining:.0f} requests left in the window")
                else:
                    before = getattr(client, "calls", 0)
                    matched = sweep_topics(client, self_id, topic_filter)
                    # Cleared only once the pass finished: a sweep a rate limit
                    # cut short is work still owed, not work done.
                    full_sweep = False
                    pending.update(matched)
                    spent = getattr(client, "calls", 0) - before
                    left = getattr(client, "rate_limit_remaining", None)
                    log(
                        f"full sweep: {len(matched)} awaiting, {spent} calls spent, "
                        f"{'unknown' if left is None else f'{left:.0f}'} left in the window"
                    )
            while pending:
                # Peeked, not popped: a rate limit inside the check leaves this
                # entry — and every other one — pending for after the backoff.
                channel, topic = min(pending)
                history = client.topic_history(channel, topic, num_before=1)
                pending.discard((channel, topic))
                if history and history[-1].get("sender_id") == self_id:
                    continue  # we already answered; the event was stale
                log(f"serving {channel!r}/{topic!r}")
                try:
                    handler(channel, topic)
                except Exception as error:  # one bad topic must not end the loop
                    log(f"handler failed on {channel!r}/{topic!r}: {error!r}")
            events = client.poll(queue_id, last_event_id)
        except ZulipTimeout as error:
            status.record_error(str(error))
            continue  # nothing happened within the poll window
        except QueueExpired as error:
            status.record_error(str(error))
            log(f"event queue expired ({error}); re-registering")
            queue_id = None
            continue
        except RateLimited as error:
            # Before the generic arm on purpose: the queue is fine and
            # `pending`/`full_sweep` stay as they are, so the work outstanding
            # when the limit hit is served after the backoff.
            strikes += 1
            delay = rate_limit_backoff(error.retry_after, strikes)
            status.record_error(str(error))
            log(f"rate limited: {error}; backing off {delay:.1f}s (strike {strikes}, queue kept)")
            time.sleep(delay)
            continue
        except ZulipError as error:
            status.record_error(str(error))
            log(f"zulip call failed: {error}; retrying in {RETRY_SECONDS}s")
            queue_id = None
            time.sleep(RETRY_SECONDS)
            continue
        strikes = 0
        status.record_poll_ok(queue_id)
        for event in events:
            last_event_id = max(last_event_id, int(event.get("id", last_event_id)))
            if event.get("type") != "message":
                continue
            match = topic_from_event(event.get("message") or {}, self_id, topic_filter)
            if match is not None:
                # A hint about where to look, not a decision to act. Repeats
                # within one burst collapse into a single set entry.
                pending.add(match)
