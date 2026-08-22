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
from typing import Callable

from agag.selfnote import (
    ROOTCHAT_TAG,
    SERVED_TAG,
    Conversation,
    is_selfnote,
    last_real_message,
    last_real_sender,
    own_rootchat,
    parse_rootchat,
    parse_served,
    served_note,
)
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
#: How far back a startup mention recovery looks.
MENTION_HISTORY = 50
#: How far back a startup root-note recovery looks.
ROOTCHAT_HISTORY = 200
#: How many messages a "who spoke last" check reads. More than one,
#: because the newest messages may be selfnotes and a selfnote is not
#: somebody speaking — see `agag.selfnote`.
#:
#: Raised from 10 in `agent_standardize` p9, when the served note gave home
#: topics a second kind of note that accumulates. A home now collects one
#: `[served]` note per callback, and a busy conversation can end on a run of
#: them; read too few messages back and a topic full of its own bookkeeping
#: reads as "nobody has spoken". The cost is unchanged — it is the
#: `num_before` of a call already being made.
LAST_SPEAKER_LOOKBACK = 30

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

    def create_bot(self, full_name: str, short_name: str) -> dict:
        """Create a generic bot and return its usable credentials.

        Zulip versions differ in how much ``POST /bots`` returns.  Fill in
        the bot profile from the user endpoint and regenerate the API key
        only when the creation response omitted it.  Callers must check for
        an existing bot first: regenerating a key invalidates the running
        bot's credential.
        """
        created = self.call(
            "POST",
            "bots",
            {"full_name": full_name, "short_name": short_name, "bot_type": 1},
        )
        try:
            user_id = int(created["user_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ZulipError("POST bots did not return a bot user_id") from error

        result = dict(created)
        result["user_id"] = user_id
        if not result.get("email"):
            fetched = self.call("GET", f"users/{user_id}")
            profile = fetched.get("user", fetched)
            if profile.get("email"):
                result["email"] = profile["email"]
            elif profile.get("delivery_email"):
                result["email"] = profile["delivery_email"]
        if not result.get("api_key"):
            regenerated = self.call("POST", f"bots/{user_id}/api_key/regenerate")
            if regenerated.get("api_key"):
                result["api_key"] = regenerated["api_key"]

        missing = [key for key in ("email", "api_key") if not result.get(key)]
        if missing:
            raise ZulipError(f"created bot {user_id} is missing {', '.join(missing)}")
        return result

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

    def user_by_email(self, email: str) -> dict | None:
        """Find a realm member by visible or owner-visible delivery email."""
        for user in self.users():
            if email in (user.get("delivery_email"), user.get("email")):
                return user
        return None

    def update_channel_description(self, stream_id: int, description: str) -> dict:
        """Replace a channel description by numeric stream id."""
        return self.call(
            "PATCH",
            f"streams/{int(stream_id)}",
            {"description": description},
        )

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

    def ensure_subscribed(self, channel: str) -> bool:
        """Subscribe to `channel` unless already there. True if it changed.

        Reading and posting never need this — a bot may do both in any public
        channel unsubscribed. What needs it is *being called back*: only a
        subscribed channel's messages reach the event stream, so an agent
        that has joined a conversation elsewhere must be in that room to
        learn that somebody answered.
        """
        for subscription in self.subscriptions():
            if str(subscription.get("name", "")) == channel:
                return False
        self.subscribe_channels([channel])
        return True

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

    def topic_since(
        self, channel: str, topic: str, after_id: int, num_after: int = 100
    ) -> list[dict]:
        """Messages of the topic strictly newer than `after_id`, oldest first.

        The anchor itself is excluded, so an id already seen never comes back
        — which is what makes a caller's "has anything new arrived?" loop
        cheap and idempotent.
        """
        result = self.call(
            "GET", "messages",
            {
                "anchor": str(int(after_id)),
                "include_anchor": "false",
                "num_before": "0",
                "num_after": str(num_after),
                "apply_markdown": "false",
                "narrow": [
                    {"operator": "channel", "operand": channel},
                    {"operator": "topic", "operand": topic},
                ],
            },
        )
        return result.get("messages", [])

    def topic_last_id(self, channel: str, topic: str) -> int:
        """Id of the topic's newest message, or 0 when it has none yet."""
        messages = self.topic_history(channel, topic, num_before=1)
        return int(messages[-1]["id"]) if messages else 0

    def mentions(self, num_before: int = MENTION_HISTORY) -> list[dict]:
        """Recent messages that mention this bot, oldest last.

        Zulip's `is:mentioned` narrow, which is what makes a mention that
        arrived while the listener was down recoverable at startup — the same
        losslessness the full topic sweep gives the owner route.
        """
        result = self.call(
            "GET", "messages",
            {
                "anchor": "newest",
                "num_before": str(num_before),
                "num_after": "0",
                "apply_markdown": "false",
                "narrow": [{"operator": "is", "operand": "mentioned"}],
            },
        )
        return result.get("messages", [])

    def own_notes(self, tag: str, num_before: int = ROOTCHAT_HISTORY) -> list[dict]:
        """Recent `[selfnote][<tag>]` messages written by this bot, oldest first.

        `sender:<me>` narrowed by a full-text `search` for the tag word. One
        call lists a whole kind of memory this agent has written down.
        Messages that are not actually notes of that kind (a human quoting
        the word) are filtered by the caller, because parsing is what
        decides, not the search.
        """
        result = self.call(
            "GET", "messages",
            {
                "anchor": "newest",
                "num_before": str(num_before),
                "num_after": "0",
                "apply_markdown": "false",
                "narrow": [
                    {"operator": "sender", "operand": self.email},
                    {"operator": "search", "operand": tag},
                ],
            },
        )
        return result.get("messages", [])

    def own_rootchat_notes(self, num_before: int = ROOTCHAT_HISTORY) -> list[dict]:
        """Recent root notes written by this bot, oldest first.

        This one call lists every conversation this agent is party to on
        somebody else's behalf — the question the participation ledger
        existed to answer, asked of the chat instead.
        """
        return self.own_notes(ROOTCHAT_TAG, num_before)

    def own_served_notes(self, num_before: int = ROOTCHAT_HISTORY) -> list[dict]:
        """Recent served notes written by this bot, oldest first.

        The companion question: of the topics this agent is party to, which
        callbacks has it already answered, and up to which message.
        """
        return self.own_notes(SERVED_TAG, num_before)

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


TopicFilter = str | tuple[str, ...] | Callable[[str, str], bool]


def topic_matches(channel: str, topic: str, topic_filter: TopicFilter) -> bool:
    """Whether a channel/topic pair belongs to a consumer's sweep.

    Prefix strings keep the original lightweight route. A callable lets an
    agent own every topic in one named channel while still applying prefixes
    in its other subscriptions.
    """
    return topic_filter(channel, topic) if callable(topic_filter) else topic.startswith(topic_filter)


def topic_from_event(
    message: dict, self_id: int, topic_filter: TopicFilter
) -> tuple[str, str] | None:
    """The `(channel, topic)` a message event points at, if it can await us.

    A hint, not a verdict: the same rules `sweep_topics` applies to a topic
    name, read off the event payload instead of off a channel listing. Whether
    the topic *actually* awaits a reply is decided by reading its last message,
    which is one call rather than a whole sweep.
    """
    if not is_channel_message_for_us(message, self_id):
        return None
    if is_selfnote(message.get("content")):
        return None  # a note an agent wrote to itself is not a turn
    topic = str(message.get("subject") or "")
    channel = channel_name(message)
    if not topic or not channel or not topic_matches(channel, topic, topic_filter):
        return None
    if topic.startswith(RESOLVED_TOPIC_PREFIX):
        return None
    return (channel, topic)


def is_mention_for_us(
    message: dict, self_id: int, flags=None, bot_name: str | None = None
) -> bool:
    """Whether a channel message calls this bot by name.

    Zulip's own `mentioned` flag is the authority; the name scan is a
    fallback for payloads that arrive without flags, so a mention is not
    missed because of where it was read from.
    """
    if not is_channel_message_for_us(message, self_id):
        return False
    if is_selfnote(message.get("content")):
        return False  # a note an agent wrote to itself is not a turn
    carried = list(flags or []) + list(message.get("flags") or [])
    if "mentioned" in carried:
        return True
    if bot_name:
        return f"@**{bot_name}**" in str(message.get("content", ""))
    return False


def mention_from_event(
    event: dict, self_id: int, bot_name: str | None = None
) -> tuple[str, str] | None:
    """The `(channel, topic)` a mention of this bot points at, if it is one.

    The second trigger beside the owner sweep. An owner is served by anybody
    else's post in its own topic; a *participant* is served only when it is
    named — which is how a run that posted somewhere and finished gets its
    turn back without anyone waiting.
    """
    message = event.get("message") or {}
    if not is_mention_for_us(message, self_id, event.get("flags"), bot_name):
        return None
    topic = str(message.get("subject") or "")
    channel = channel_name(message)
    if not topic or not channel or topic.startswith(RESOLVED_TOPIC_PREFIX):
        return None
    return (channel, topic)


def sweep_mentions(
    client: ZulipClient,
    self_id: int,
    num_before: int = MENTION_HISTORY,
    marks: dict[tuple[str, str], int] | None = None,
) -> list[tuple[str, str]]:
    """Every `(channel, topic)` where this bot was recently mentioned.

    Startup and queue-expiry recovery for the mention route: a mention that
    landed while this was down is found here rather than lost, which is the
    same discipline `sweep_topics` gives the owner route. Whether the topic
    still awaits an answer is decided afterwards by reading its last message,
    exactly as a swept topic is.

    `marks` is `served_marks` — a mention at or below the mark for its topic
    is one this bot has already answered. Before `agent_standardize` p8 this
    route was self-silencing, because answering a mention meant posting in
    that topic and Zulip stops offering a mention once it is read. p8 sent
    the answer *home* instead, so `is:mentioned` keeps returning the same
    posts for the rest of the topic's life and every restart re-serves them.
    p9 gave `sweep_rootchats` the mark and left this one out, and the proof
    run found it on the very next restart.
    """
    found: list[tuple[str, str]] = []
    for message in client.mentions(num_before):
        if message.get("type") != "stream" or message.get("sender_id") == self_id:
            continue
        if is_selfnote(message.get("content")):
            continue
        topic = str(message.get("subject") or "")
        channel = channel_name(message)
        if not topic or not channel or topic.startswith(RESOLVED_TOPIC_PREFIX):
            continue
        served = (marks or {}).get((channel, topic))
        if served is not None and int(message.get("id") or 0) <= served:
            continue  # already answered; the answer went home, not here
        if (channel, topic) not in found:
            found.append((channel, topic))
    return found


def rootchat_notes(
    client: ZulipClient, num_before: int = ROOTCHAT_HISTORY
) -> list[tuple[tuple[str, str], Conversation]]:
    """`((channel, topic), home)` for every root note this bot has written.

    The chat-side replacement for the participation ledger: where this agent
    has spoken on somebody else's behalf, and on whose behalf. Resolved
    topics are dropped — a finished conversation is not one to be called back
    into — and so is anything the search matched that does not parse as a
    root note.
    """
    anchored: list[tuple[tuple[str, str], Conversation]] = []
    seen: set[tuple[str, str]] = set()
    for message in client.own_rootchat_notes(num_before):
        home = parse_rootchat(message.get("content"))
        if home is None or message.get("type") != "stream":
            continue
        topic = str(message.get("subject") or "")
        channel = channel_name(message)
        if not topic or not channel or topic.startswith(RESOLVED_TOPIC_PREFIX):
            continue
        if (channel, topic) in seen:
            continue  # the earliest note anchors the topic; later ones repeat
        seen.add((channel, topic))
        anchored.append(((channel, topic), home))
    return anchored


def remotes_for_home(
    client: ZulipClient, channel: str, topic: str, num_before: int = ROOTCHAT_HISTORY
) -> list[Conversation]:
    """Every conversation this one has reached out to, oldest note first.

    The list of threads a run serving `<channel>/<topic>` is party to, which
    is what decides the `threads/` folder it gets.
    """
    home = Conversation(channel, topic)
    found: list[Conversation] = []
    for (remote_channel, remote_topic), anchored in rootchat_notes(
        client, num_before
    ):
        if anchored != home:
            continue
        remote = Conversation(remote_channel, remote_topic)
        if remote not in found:
            found.append(remote)
    return found


def live_topic_name(client: ZulipClient, channel: str, topic: str) -> str:
    """`topic`, or its resolved `\u2714 ` name when that is what exists now.

    Resolving a topic *renames* it. Anything still holding the old name — an
    event that was queued before the rename, a caller that remembers where it
    posted — has to be told, or it reads an empty topic and writes into a new
    one beside the real conversation.
    """
    if topic.startswith(RESOLVED_TOPIC_PREFIX):
        return topic
    resolved = f"{RESOLVED_TOPIC_PREFIX}{topic}"
    try:
        names = client.channel_topics(client.stream_id(channel))
    except Exception:  # noqa: BLE001 - a lookup is never worth losing the post
        return topic
    return resolved if resolved in names and topic not in names else topic


def topic_history_across_resolve(
    client: ZulipClient, channel: str, topic: str, num_before: int
) -> list[dict]:
    """A topic's history, found under its `\u2714 ` name when it was renamed.

    A conversation does not end when it is resolved, and a mention that
    arrived just before the rename still names the topic as it was called
    then. Reading the bare name in that window returns nothing at all — not
    "no note of ours", but no messages — and `agent_standardize` p9 watched a
    supervisor lose a task's completion report to exactly that gap: the task
    reported, resolved itself a second later, and the callback that should
    have started the next task found an empty topic.

    `agentchat wait` and `read --since` have followed the rename since pyagag
    `5bda102`; this is the same rule for the callback lookup.
    """
    try:
        history = client.topic_history(channel, topic, num_before=num_before)
    except ZulipError:
        history = []
    if history or topic.startswith(RESOLVED_TOPIC_PREFIX):
        return history
    try:
        return client.topic_history(
            channel, f"{RESOLVED_TOPIC_PREFIX}{topic}", num_before=num_before
        )
    except ZulipError:
        return []


def rootchat_home(
    client: ZulipClient,
    channel: str,
    topic: str,
    self_id: int,
    num_before: int = ROOTCHAT_HISTORY,
) -> Conversation | None:
    """Which of this bot's own conversations it is speaking in this one for.

    The callback's whole lookup: a run that was named in somebody else's
    topic reads that topic, finds the root note it wrote there itself, and
    that note is the conversation to serve. `None` for a topic this bot never
    anchored — a mention that is somebody else's business.

    Read across the resolve rename, because the post that names an agent is
    very often the post that finishes the conversation.
    """
    return own_rootchat(
        topic_history_across_resolve(client, channel, topic, num_before), self_id
    )


def served_marks(
    client: ZulipClient, num_before: int = ROOTCHAT_HISTORY
) -> dict[tuple[str, str], int]:
    """`{(channel, topic): newest served message id}` for this bot's callbacks.

    The other half of the chat-as-memory: `rootchat_notes` says which topics
    this agent is party to, and this says how far into each of them it has
    already answered. Only the highest id per topic matters — a later note
    supersedes an earlier one.
    """
    marks: dict[tuple[str, str], int] = {}
    for message in client.own_served_notes(num_before):
        parsed = parse_served(message.get("content"))
        if parsed is None:
            continue
        remote, message_id = parsed
        key = remote.as_pair()
        if message_id > marks.get(key, 0):
            marks[key] = message_id
    return marks


def mark_served(
    client: ZulipClient,
    home: Conversation,
    remote: Conversation,
    message_id: int,
) -> None:
    """Record in `home` that `remote` has been answered up to `message_id`."""
    # The serving may have resolved home on its way out — a task that closes
    # renames its own topic. Posting under the old name would open a second
    # topic beside it holding nothing but this note.
    client.send_to_channel(
        home.channel,
        live_topic_name(client, home.channel, home.topic),
        served_note(remote, message_id),
    )


def note_served(
    client: ZulipClient,
    home: Conversation,
    channel: str,
    topic: str,
    message_id: int | None = None,
) -> int | None:
    """Mark the callback from `<channel>/<topic>` served, and say up to where.

    `message_id` defaults to the newest real message in the remote topic —
    the post that named this agent, read at the moment the serving finished.
    Anything posted there afterwards is newer than the mark and calls this
    agent back again, which is exactly right.

    Returns the id recorded, or None when there was nothing real to mark.
    """
    if message_id is None:
        history = topic_history_across_resolve(
            client, channel, topic, LAST_SPEAKER_LOOKBACK
        )
        last = last_real_message(history)
        if last is None or last.get("id") is None:
            return None
        message_id = int(last["id"])
    mark_served(client, home, Conversation(channel, topic), int(message_id))
    return int(message_id)


def sweep_rootchats(
    client: ZulipClient,
    self_id: int,
    bot_name: str | None = None,
    num_before: int = ROOTCHAT_HISTORY,
    marks: dict[tuple[str, str], int] | None = None,
) -> list[tuple[str, str]]:
    """Every topic this bot anchored that is now waiting on it.

    Startup and queue-expiry recovery for the callback route, and the thing
    that made the ledger walk unnecessary: the topics this agent is party to
    are the topics carrying its own root notes. A topic qualifies when its
    last real speaker is somebody else, that message names this bot, and it
    is **newer than the served mark** for that topic.

    That last condition is `agent_standardize` p9, and without it this sweep
    cannot be run twice. Since p8 a called-back run answers at home, so this
    bot never becomes the last poster in the topic that named it: the first
    two conditions stay true for the rest of the topic's life, and every
    listener restart would re-serve every exchange the agent ever had. Where
    a run costs a supercoder against a live repository, that is not an extra
    post — it is the work done twice.
    """
    marks = served_marks(client, num_before) if marks is None else marks
    waiting: list[tuple[str, str]] = []
    for (channel, topic), _home in rootchat_notes(client, num_before):
        history = client.topic_history(
            channel, topic, num_before=LAST_SPEAKER_LOOKBACK
        )
        last = last_real_message(history)
        if last is None or last.get("sender_id") == self_id:
            continue
        if bot_name and f"@**{bot_name}**" not in str(last.get("content", "")):
            continue
        served = marks.get((channel, topic))
        if served is not None and int(last.get("id") or 0) <= served:
            continue  # already answered; the answer went home, not here
        if (channel, topic) not in waiting:
            waiting.append((channel, topic))
    return waiting


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
    client: ZulipClient, self_id: int, topic_filter: TopicFilter
) -> list[tuple[str, str]]:
    """Every `(channel, topic)` currently awaiting this bot's reply.

    A topic qualifies when it is in a channel this bot is subscribed to, its
    name passes `topic_filter` (a prefix, tuple of prefixes, or callable), it
    is not resolved, and the last person to *really* speak in it is somebody
    else. The last-poster rule is what makes the pull loop self-stabilizing:
    the bot's own ack or reply silences a topic until a human speaks again.

    "Really" is `agag.selfnote`: a `[selfnote]` line is machine-to-machine
    and buys nobody a run. Miss that here and the root note an agent writes
    in another agent's topic is itself a post by somebody else — an ack loop
    with no ack in it.
    """
    matches: list[tuple[str, str]] = []
    for subscription in client.subscriptions():
        channel = str(subscription.get("name", ""))
        stream = subscription.get("stream_id")
        if not channel or stream is None:
            continue
        for topic in client.channel_topics(int(stream)):
            if not topic_matches(channel, topic, topic_filter):
                continue
            if topic.startswith(RESOLVED_TOPIC_PREFIX):
                continue
            history = client.topic_history(
                channel, topic, num_before=LAST_SPEAKER_LOOKBACK
            )
            # An *empty* topic still matches — `serve_topic`'s `empty_reply`
            # is what silences it. A topic holding only selfnotes does not:
            # nobody has spoken in it.
            if history and last_real_sender(history) in (None, self_id):
                continue
            matches.append((channel, topic))
    return matches


def sweep_serve(
    client: ZulipClient,
    handler,
    *,
    topic_filter: TopicFilter,
    on_mention=None,
    mention_messages: int = MENTION_HISTORY,
    log=log,
    status=None,
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

    **The second trigger.** `on_mention(channel, topic)`, when given, is
    called for a topic this bot was *mentioned* in but does not own. That is
    the turn-taking rule made mechanical: the owner of a topic is served by
    anybody else's post in it, and a participant is served only when it is
    named. It is what replaces waiting inside a run — an agent posts
    somewhere, finishes, and is called again when the answer names it.
    Mentions have their own pending set, drained after the owner set, and
    recovered on every queue registration through `sweep_mentions` **and**
    `sweep_rootchats` — the second asks the chat which topics this bot
    anchored with a root note and which of them are waiting on it, which is
    what made the participation ledger unnecessary. So a mention that arrived
    while this was down is not lost either. A topic that
    `topic_filter` already matches is served as an owner topic and never as a
    mention.

    The loop is single-threaded and serial, so a long handler simply delays
    the next poll; events keep queueing meanwhile. `status` behaves exactly as
    in `serve`.
    """
    status = status if status is not None else StatusWriter(default_status_path(), log=log)
    self_id: int | None = None
    bot_name: str | None = None
    queue_id: str | None = None
    last_event_id = -1
    pending: set[tuple[str, str]] = set()
    pending_mentions: set[tuple[str, str]] = set()
    full_sweep = False
    strikes = 0
    while True:
        try:
            if self_id is None:
                self_user = client.whoami()
                self_id = int(self_user["user_id"])
                bot_name = str(self_user.get("full_name") or "") or None
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
                    mentioned: list[tuple[str, str]] = []
                    if on_mention is not None:
                        # One lookup, both routes: what this bot has already
                        # answered is a fact about the agent, not about the
                        # route the recovery came in on.
                        marks = served_marks(client)
                        recovered = list(
                            sweep_mentions(client, self_id, mention_messages, marks)
                        )
                        for match in sweep_rootchats(
                            client, self_id, bot_name, marks=marks
                        ):
                            if match not in recovered:
                                recovered.append(match)
                        mentioned = [
                            match
                            for match in recovered
                            if not topic_matches(match[0], match[1], topic_filter)
                        ]
                        pending_mentions.update(match for match in mentioned if match not in pending)
                    spent = getattr(client, "calls", 0) - before
                    left = getattr(client, "rate_limit_remaining", None)
                    log(
                        f"full sweep: {len(matched)} awaiting, "
                        f"{len(mentioned)} mentioning, {spent} calls spent, "
                        f"{'unknown' if left is None else f'{left:.0f}'} left in the window"
                    )
            while pending or pending_mentions:
                # Peeked, not popped: a rate limit inside the check leaves this
                # entry — and every other one — pending for after the backoff.
                # Owned topics first: a topic this bot owns is never also a
                # mention to answer somewhere else.
                if pending:
                    queue, serve_one, kind = pending, handler, "serving"
                else:
                    queue, serve_one, kind = pending_mentions, on_mention, "serving mention in"
                channel, topic = min(queue)
                history = client.topic_history(
                    channel, topic, num_before=LAST_SPEAKER_LOOKBACK
                )
                queue.discard((channel, topic))
                if history and last_real_sender(history) in (None, self_id):
                    continue  # we already answered; the event was stale
                log(f"{kind} {channel!r}/{topic!r}")
                try:
                    serve_one(channel, topic)
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
                pending_mentions.discard(match)
                continue
            if on_mention is None:
                continue
            match = mention_from_event(event, self_id, bot_name)
            if match is not None and match not in pending:
                pending_mentions.add(match)
