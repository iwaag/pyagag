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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agag.memo import is_memo_message
from agag.selfnote import (
    MOVED_TAG,
    ROOTCHAT_TAG,
    SERVED_TAG,
    Conversation,
    effective_rootchat,
    is_selfnote,
    last_real_message,
    last_real_sender,
    own_rootchat,
    parse_rootchat,
    parse_rootchat_moved,
    replaced_anchor,
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
#: A header value above this is an epoch second, not a number of seconds.
EPOCH_THRESHOLD = 10_000_000.0

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

# Zulip's organization-owner role id, as `GET /users` reports it.
OWNER_ROLE = 100

# Zulip's resolved-topic marker: the topic is renamed to "✔ <topic>".
RESOLVED_TOPIC_PREFIX = "✔ "

# Environment names used by the small outbound convenience function below.
ZULIP_ENV_PATH = "ZULIP_ENV"
ZULIP_CHANNEL = "ZULIP_CHANNEL"


class ZulipError(Exception):
    """A Zulip API call failed for a reason the caller cannot ignore."""


class ZulipRejected(ZulipError):
    """Zulip received the call, understood it, and refused it (HTTP 4xx).

    The distinction this class exists for is **answered** versus
    **unanswered**. A refusal is a statement about the object — no such
    message, not a channel you may read — and a caller may act on it as a
    fact. A timeout, a dropped connection, a 429 or a 5xx is not a statement
    about anything: the object may be perfectly fine and the call simply
    never got an answer.

    Anything that reads absence out of a failure needs that line drawn, or
    one bad minute of network reads as "everything you were pointing at is
    gone". 429 is deliberately *not* a rejection — it is `RateLimited`, and
    it means wait, which is the opposite of a fact about the object.
    """


class QueueExpired(ZulipRejected):
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


def endpoint_template(path: str) -> str:
    """`messages/6931` -> `messages/<id>`, `users/me/35/topics` ->
    `users/me/<id>/topics`: the shape of a call, for counting calls by
    shape rather than by the object they touched."""
    return "/".join(
        "<id>" if part.isdigit() else part for part in path.strip("/").split("/")
    )


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
        seconds = _positive_float(get("Retry-After"))
        if seconds is not None:
            return seconds
        reset = _positive_float(get("x-ratelimit-reset"))
        if reset is not None:
            # Absolute on this server (an epoch second); a delay on others.
            # Measured in `better_zulip_call` p1: read as a delay, the epoch
            # value was a 1.7-billion-second wait that only the backoff
            # ceiling made survivable.
            delay = reset - time.time() if reset > EPOCH_THRESHOLD else reset
            if delay > 0:
                return delay
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


class Budget:
    """One credential's share of the realm's quota, coordinated.

    `better_zulip_call` p1 step 6. Zulip limits **per credential**, and a
    process (or a host) often holds several clients on one credential: the
    relay's mirror poller and reader, a listener's serving client and its
    mirror, every `agentchat` a run spawns. A 429 answered to one of them is
    a fact about all of them, so the pause it asks for is kept **here**, keyed
    by the credential's email, and every client on that email waits it out
    before its next call — one pause, honoured once, instead of each client
    discovering the same refusal and each backing off on its own schedule.

    Two more things the boundary does, because they are cheapest here:

    - **single flight**: an identical `GET` already in flight on this
      credential is joined, not repeated. Two board readers asking the same
      page at the same moment cost one call;
    - **spacing after a pause**: when the pause ends, calls are released a
      few tens of milliseconds apart rather than all at once, so the callers
      that queued up do not arrive as the wave that earns the next 429.

    The coordination is also written **beside the credentials file**
    (`<env path>.ratelimit`) when the client was built from one, so another
    process on the same credential — the relay writing as the Developer, an
    `agentchat` in a shell — sees the pause too. A tool that does not go
    through this module (the web app in a browser, `curl`) is outside it;
    that is a limitation, and the file is a best effort, not a lock.
    """

    #: How far apart calls are released right after a pause ends.
    SPACING_SECONDS = 0.05
    #: How long the post-pause spacing stays in force.
    SPACING_WINDOW_SECONDS = 2.0

    _all: dict[str, "Budget"] = {}
    _registry_lock = threading.Lock()

    @classmethod
    def for_credential(cls, email: str, sidecar: Path | None = None) -> "Budget":
        with cls._registry_lock:
            found = cls._all.get(email)
            if found is None:
                found = cls._all[email] = Budget(email)
            if sidecar is not None and found.sidecar is None:
                found.sidecar = sidecar
            return found

    @classmethod
    def forget_all(cls) -> None:
        """For tests: no pause outlives the test that caused it."""
        with cls._registry_lock:
            cls._all.clear()

    def __init__(self, email: str):
        self.email = email
        self.sidecar: Path | None = None
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self.pause_until = 0.0
        self.spacing_until = 0.0
        self._next_slot = 0.0
        self._inflight: dict[tuple[str, str], threading.Event] = {}
        self._results: dict[tuple[str, str], tuple[dict | None, BaseException | None]] = {}
        #: Counters a measurement reads: how often a caller waited for a
        #: pause, how many 429s were received, how many GETs were joined.
        self.waits = 0
        self.refusals = 0
        self.joined = 0

    # -- the pause -------------------------------------------------------------------

    def pause(self, seconds: float, *, now: float | None = None) -> float:
        """Honour the server's guidance: nobody on this credential calls
        before `now + seconds` (jittered a little, so processes sharing the
        credential do not resynchronise)."""
        now = time.time() if now is None else now
        seconds = max(float(seconds), 1.0)
        until = now + seconds + random.uniform(0.0, seconds * RATE_LIMIT_JITTER_FRACTION)
        with self._lock:
            self.refusals += 1
            self.pause_until = max(self.pause_until, until)
            self.spacing_until = self.pause_until + self.SPACING_WINDOW_SECONDS
            self._next_slot = self.pause_until
            self._condition.notify_all()
        self._write_sidecar(self.pause_until)
        return self.pause_until

    def _read_sidecar(self) -> float:
        if self.sidecar is None:
            return 0.0
        try:
            return float(self.sidecar.read_text(encoding="utf-8").strip() or 0.0)
        except (OSError, ValueError):
            return 0.0

    def _write_sidecar(self, until: float) -> None:
        if self.sidecar is None:
            return
        try:
            self.sidecar.write_text(f"{until:.3f}\n", encoding="utf-8")
        except OSError:
            pass

    def wait_turn(self, *, now_fn=None, sleep=None) -> float:
        """Block until this credential may call again. Returns how long it
        waited (0 when it did not)."""
        now_fn = now_fn or time.time
        sleep = sleep or time.sleep
        waited = 0.0
        while True:
            now = now_fn()
            with self._lock:
                outside = self._read_sidecar()
                if outside > self.pause_until:
                    self.pause_until = outside
                    self.spacing_until = outside + self.SPACING_WINDOW_SECONDS
                    self._next_slot = max(self._next_slot, outside)
                if now < self.pause_until:
                    delay = self.pause_until - now
                elif now < self.spacing_until:
                    slot = max(self._next_slot, now)
                    self._next_slot = slot + self.SPACING_SECONDS
                    delay = slot - now
                    if delay <= 0:
                        return waited
                else:
                    return waited
                if waited == 0.0:
                    self.waits += 1
            sleep(delay)
            waited += delay

    # -- single flight -------------------------------------------------------------------

    def join_or_lead(self, key: tuple[str, str]):
        """For a GET: `(True, None)` when this caller leads the call, or
        `(False, event)` when an identical one is in flight to be joined."""
        with self._lock:
            found = self._inflight.get(key)
            if found is not None:
                self.joined += 1
                return False, found
            event = threading.Event()
            self._inflight[key] = event
            return True, event

    def finish(self, key: tuple[str, str], result: dict | None, error: BaseException | None) -> None:
        with self._lock:
            event = self._inflight.pop(key, None)
            self._results[key] = (result, error)
        if event is not None:
            event.set()

    def joined_result(self, key: tuple[str, str]) -> tuple[dict | None, BaseException | None]:
        with self._lock:
            return self._results.get(key, (None, None))


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
        #: When the window slides, as an epoch second — Zulip's
        #: `x-ratelimit-reset` is absolute on this server, not a delay.
        self.rate_limit_reset: float | None = None
        #: Our own profile, fetched once. A bot's user id and full name do not
        #: change while a process runs, and every serving asks for them at
        #: least twice now (the execution menu is addressed by the name a
        #: mention matches, and `serve_topic` needs the id). A call that is
        #: always the same answer is a call out of the quota the listeners
        #: share.
        self._whoami: dict | None = None
        #: Calls by `(purpose, method, endpoint template)` — the accounting
        #: the `better_zulip_call` episode asked for at the transport
        #: boundary. `purpose` is whatever the caller sets on the client
        #: around a phase of work (`hydrate`, `poll`, `verify`, `write`), so a
        #: measurement can say what a call was *for*, not only where it went.
        self.purpose = ""
        self.ledger: dict[tuple[str, str, str], int] = {}
        #: Shared with every client on this credential in the process (and,
        #: through the sidecar, on the host): the pause a 429 asked for.
        self.budget = Budget.for_credential(email)
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
        client = cls(
            env["ZULIP_URL"], env["ZULIP_EMAIL"], env["ZULIP_API_KEY"],
            ca_bundle=env.get("ZULIP_CA_BUNDLE") or None,
        )
        # The pause file lives beside the credentials, which is the one path
        # every process on this credential already knows.
        client.budget = Budget.for_credential(env["ZULIP_EMAIL"], Path(path).with_name(Path(path).name + ".ratelimit"))
        return client

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
        # The pause a 429 asked for is honoured by every client on this
        # credential; a GET identical to one in flight is joined, not repeated.
        self.budget.wait_turn()
        flight: tuple[str, str] | None = (method, url) if method == "GET" and not path.startswith("events") else None
        if flight is not None:
            lead, event = self.budget.join_or_lead(flight)
            if not lead:
                event.wait(timeout)
                result, error = self.budget.joined_result(flight)
                if error is not None:
                    raise error
                if result is not None:
                    return result
        self.calls += 1
        # A write is a write whatever phase set the purpose; a read with no
        # purpose is an ordinary read. The measurement step 6 asks for keeps
        # polling, hydration, verification and writes apart by this key.
        purpose = "write" if method != "GET" else (self.purpose or "read")
        key = (purpose, method, endpoint_template(path))
        self.ledger[key] = self.ledger.get(key, 0) + 1
        try:
            answer = self._send(request, timeout)
        except BaseException as error:
            if flight is not None:
                self.budget.finish(flight, None, error)
            raise
        if flight is not None:
            self.budget.finish(flight, answer, None)
        return answer

    def _send(self, request, timeout: float) -> dict:
        method, path = request.get_method(), request.full_url
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
                # Its own class: the caller must wait, not reconnect — and
                # the wait is shared with everything on this credential.
                delay = retry_after_seconds(error.headers, parsed)
                self.budget.pause(delay)
                detail = (parsed or {}).get("msg") or body[:200]
                raise RateLimited(
                    f"{method} {path} -> HTTP 429: {detail} (retry after {delay:.0f}s)",
                    retry_after=delay,
                ) from error
            # An answered 4xx is a refusal; a 5xx is the server failing to
            # answer at all, and stays an ordinary error nobody may read as
            # a fact about the object asked for.
            refused = ZulipRejected if 400 <= error.code < 500 else ZulipError
            if parsed is None:
                raise refused(f"{method} {path} -> HTTP {error.code}: {body[:200]}") from error
            if parsed.get("code") == "BAD_EVENT_QUEUE_ID":
                raise QueueExpired(parsed.get("msg", "bad event queue id")) from error
            raise refused(f"{method} {path} -> HTTP {error.code}: {parsed.get('msg')}") from error
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
        reset = _float_or_none(get("x-ratelimit-reset"))
        if reset is not None:
            self.rate_limit_reset = reset if reset > EPOCH_THRESHOLD else time.time() + reset

    # --- the four mechanics the receive side needs -------------------------

    def whoami(self, refresh: bool = False) -> dict:
        """This bot's own profile, fetched once per client.

        `refresh=True` re-reads it, for the one caller that has just changed
        the account it is asking about.
        """
        if refresh or self._whoami is None:
            self._whoami = self.call("GET", "users/me")
        return self._whoami

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
            if profile.get("delivery_email"):
                result["email"] = profile["delivery_email"]
            elif profile.get("email"):
                result["email"] = profile["email"]
        if not result.get("api_key"):
            regenerated = self.call("POST", f"bots/{user_id}/api_key/regenerate")
            if regenerated.get("api_key"):
                result["api_key"] = regenerated["api_key"]

        missing = [key for key in ("email", "api_key") if not result.get(key)]
        if missing:
            raise ZulipError(f"created bot {user_id} is missing {', '.join(missing)}")
        return result

    def register(
        self,
        event_types: list[str] | None = None,
        *,
        all_public_streams: bool = False,
        fetch_event_types: list[str] | None = None,
    ) -> tuple[str, int]:
        """Register an event queue and return `(queue_id, last_event_id)`.

        The default is the listeners' historical shape: `message` events for
        the channels this bot is subscribed to. `all_public_streams` asks
        for every public channel whether or not the bot is in it — verified
        on this realm (feature level 500) for a bot, `better_zulip_call` p1 —
        which is what lets one queue mirror the realm without the
        subscription writes the ops engine used to make. `fetch_event_types`
        narrows the *initial state* the registration answers with; `[]`
        keeps that payload empty, which is all a mirror that reads history
        itself ever wants.
        """
        params: dict = {"event_types": list(event_types or ["message"])}
        if all_public_streams:
            params["all_public_streams"] = True
        if fetch_event_types is not None:
            params["fetch_event_types"] = list(fetch_event_types)
        result = self.call("POST", "register", params)
        return result["queue_id"], int(result["last_event_id"])

    def poll(self, queue_id: str, last_event_id: int, *, dont_block: bool = False) -> list[dict]:
        """Block until events arrive. Raises QueueExpired when the queue died.

        `dont_block` answers at once with whatever is queued — the probe a
        restarted consumer makes to learn whether its persisted queue is
        still alive before it decides to read the realm again.
        """
        params = {"queue_id": queue_id, "last_event_id": str(last_event_id)}
        if dont_block:
            params["dont_block"] = True
        result = self.call(
            "GET", "events", params,
            timeout=POLL_TIMEOUT_SECONDS,
        )
        return result.get("events", [])

    def messages_page(
        self,
        narrow: list[dict],
        *,
        anchor: int | str = "newest",
        num_before: int = 0,
        num_after: int = 0,
        include_anchor: bool = True,
    ) -> dict:
        """One page of `GET /messages`, **with** its coverage flags.

        The other readers here return the list and drop `found_oldest` /
        `found_newest`; a store that has to say whether it holds a whole
        conversation needs them, so this returns the raw answer. Raw
        markdown, as everywhere in this module.
        """
        params: dict = {
            "anchor": str(anchor),
            "num_before": str(int(num_before)),
            "num_after": str(int(num_after)),
            "apply_markdown": "false",
            "narrow": narrow,
        }
        if not include_anchor:
            params["include_anchor"] = "false"
        return self.call("GET", "messages", params)

    def channel_topics_detail(self, stream_id: int) -> list[dict]:
        """Topic rows of one channel as Zulip returns them: `name` and
        `max_id`, newest first. `channel_topics` keeps only the names; a
        resync compares the ids without reading a single topic."""
        result = self.call("GET", f"users/me/{int(stream_id)}/topics")
        return [row for row in result.get("topics", []) if isinstance(row, dict)]

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
        channel leaves that channel's folder alone, and
        `set_channel_folder` is how that one is moved.
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

    def channel_folder_by_name(self, name: str) -> dict | None:
        """One unarchived channel folder by its display name, or `None`."""
        for folder in self.channel_folders():
            if folder.get("name") == name:
                return folder
        return None

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

    def channels(self, *, include_archived: bool = False) -> list[dict]:
        """Public channels visible to this bot.

        Archived channels are excluded by default, which is what almost every
        caller wants: an archived channel is retired and costs no sweep. Pass
        `include_archived` to see them anyway — they keep their `folder_id`
        and their `is_archived` flag, and a folder cannot be archived while
        one of them is still filed in it (`archive_channel_folder`).
        """
        params = {"exclude_archived": "false"} if include_archived else None
        return self.call("GET", "streams", params).get("streams", [])

    def clear_channel_folder(self, stream_id: int) -> dict:
        """Take one channel out of whatever folder it is filed in.

        The counterpart of `set_channel_folder`, and the only way an
        *archived* channel stops holding its folder open. Verified against
        Zulip 12.2 (feature level 500): an archived channel still accepts
        this PATCH.
        """
        return self.call("PATCH", f"streams/{int(stream_id)}", {"folder_id": None})

    def subscriptions(self) -> list[dict]:
        """Channels to which this bot is currently subscribed."""
        return self.call("GET", "users/me/subscriptions").get("subscriptions", [])

    def users(self) -> list[dict]:
        """Realm members, bots included, active and deactivated alike."""
        return self.call("GET", "users").get("members", [])

    def realm_owners(self) -> list[int]:
        """Active, non-bot organization owners (Zulip role 100), by user id.

        Who owns the realm is the closest machine-readable answer to "which
        humans should see a new agent". It is resolved here, at provisioning
        time, so no generated agent has to carry a realm-local user id.
        """
        return [
            int(user["user_id"])
            for user in self.users()
            if user.get("role") == OWNER_ROLE
            and not user.get("is_bot")
            and user.get("is_active", True)
        ]

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

    def set_channel_folder(self, stream_id: int, folder_id: int) -> dict:
        """Move an existing channel into a channel folder by numeric ids.

        `create_channel`'s `folder_id` only files a channel at creation, so
        this is the only way to file one that already exists — including
        every channel created before its realm had folders. Verified against
        Zulip 12.2 (feature level 500).
        """
        return self.call(
            "PATCH",
            f"streams/{int(stream_id)}",
            {"folder_id": int(folder_id)},
        )

    def archive_channel_folder(self, folder_id: int) -> dict:
        """Archive one channel folder; the channels it held are not touched.

        Zulip refuses to archive a folder that still holds **any** channel,
        archived ones included — and an archived channel is not in
        `channels()`, so a caller that only looks there sees an empty folder
        and a 400 it cannot explain (`refactor` p3 ex1, six orphaned folders
        deep). Retire the channels, then `clear_channel_folder` each one, and
        only then archive the folder. Organization administrators only — the
        same principal that files channels it did not create.
        """
        return self.call(
            "PATCH", f"channel_folders/{int(folder_id)}", {"is_archived": True}
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

    def add_reaction(self, message_id: int, emoji_name: str = "eyes") -> None:
        """React to a message — an acknowledgement that is not a post.

        A bot *post* into a run topic re-serves that topic's owner: it is the
        resume mechanism itself. So a bot that wants to say "seen" without
        waking anybody has exactly one move, because Zulip reactions raise no
        message event and no mention. Reacting twice is an error the caller
        can ignore; the reaction is already there.
        """
        self.call(
            "POST", f"messages/{int(message_id)}/reactions",
            {"emoji_name": emoji_name},
        )

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

    def message(self, message_id: int, *, strict: bool = False) -> dict | None:
        """One message by its id, as it stands now, or None when it is gone.

        A message id is the one identifier in Zulip that no rename touches:
        resolving a topic renames it, a topic can be renamed by hand, and a
        message can be moved between topics and channels — through all of
        that the id stays, and this read answers with the conversation the
        message is in **now** (`display_recipient` and `subject`). That is
        what makes an id usable as an anchor for a conversation whose display
        name is reusable.

        A deleted message is absent, not an error: `None` is the honest
        answer, and it is a different answer from "a topic with that name
        exists". Callers rely on that difference.

        `strict=True` narrows `None` to mean *only* that: Zulip answered and
        refused (`ZulipRejected`). A call that never got an answer — a
        timeout, a dropped connection, a 429, a 5xx — is raised instead of
        being flattened into absence, because a caller that is about to do
        something terminal must be able to tell "it is gone" from "I could
        not look". The default stays lenient so that readers for whom an
        unreadable message and a missing one are the same thing keep
        working.
        """
        try:
            result = self.call(
                "GET", f"messages/{int(message_id)}", {"apply_markdown": "false"}
            )
        except ZulipRejected:
            return None
        except ZulipError:
            if strict:
                raise
            return None
        message = result.get("message")
        return message if isinstance(message, dict) else None

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
        # A mention written into a memo invites nobody (`agag.memo`).
        return [m for m in result.get("messages", []) if not is_memo_message(m)]

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
        # A note copied into a memo is text on display, not memory (`agag.memo`).
        return [m for m in result.get("messages", []) if not is_memo_message(m)]

    def public_notes(self, tag: str, num_before: int = ROOTCHAT_HISTORY) -> list[dict]:
        """Recent `[selfnote][<tag>]` candidates written by **anybody**, oldest first.

        `own_notes` without the sender: the question "which conversations
        were opened on behalf of that one" is asked by a reader that is not
        the agent that opened them (`agag.trace`). `channels:public` makes it
        the realm's public history rather than this bot's inbox, which only
        holds the channels it is subscribed to. The caller parses; the search
        only narrows.
        """
        result = self.call(
            "GET", "messages",
            {
                "anchor": "newest",
                "num_before": str(num_before),
                "num_after": "0",
                "apply_markdown": "false",
                "narrow": [
                    {"operator": "channels", "operand": "public"},
                    {"operator": "search", "operand": tag},
                ],
            },
        )
        return [m for m in result.get("messages", []) if not is_memo_message(m)]

    def own_rootchat_notes(self, num_before: int = ROOTCHAT_HISTORY) -> list[dict]:
        """Recent root notes written by this bot, oldest first.

        This one call lists every conversation this agent is party to on
        somebody else's behalf — the question the participation ledger
        existed to answer, asked of the chat instead.
        """
        return self.own_notes(ROOTCHAT_TAG, num_before)

    def own_moved_notes(self, num_before: int = ROOTCHAT_HISTORY) -> list[dict]:
        """Recent deliberate anchor corrections written by this bot.

        Its own narrow rather than a filter over the `rootchat` one: whether
        a full-text search for `rootchat` also matches `rootchat-moved`
        depends on how the server tokenizes a hyphen, and a routing lookup is
        not the place to depend on that. One extra call per recovery sweep.
        """
        return self.own_notes(MOVED_TAG, num_before)

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

    def rename_topic(self, message_id: int, new_name: str) -> None:
        """Rename a whole topic, moving every message in it.

        One PATCH on any message of the topic with `change_all`, which this
        realm permits a bot even for other senders' messages. `resolve_topic`
        is the special case where the new name is the `\u2714 ` one.

        A rename is how a *display name* is released while the conversation
        keeps its identity: a message id survives it, so anything anchored to
        the conversation still finds it, and the freed name is available to
        whatever work comes next. Zulip has one topic per name in a channel,
        so the old name must be released **before** the new work claims it —
        otherwise the two conversations merge into one.
        """
        self.call(
            "PATCH", f"messages/{int(message_id)}",
            {
                "topic": new_name,
                "propagate_mode": "change_all",
                "send_notification_to_new_thread": False,
            },
        )

    def resolve_topic(self, message_id: int, topic: str) -> None:
        """Mark a topic resolved (Zulip's ✔ rename), moving every message in
        it — other senders' included, which this realm permits for bots."""
        if topic.startswith(RESOLVED_TOPIC_PREFIX):
            return
        self.rename_topic(message_id, f"{RESOLVED_TOPIC_PREFIX}{topic}")


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
    if is_memo_message(message):
        return None  # a memo is read, never answered
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
    if is_memo_message(message):
        return False  # a mention written into a memo invites nobody
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


def rootchat_notes(
    client: ZulipClient,
    num_before: int = ROOTCHAT_HISTORY,
    *,
    include_resolved: bool = False,
) -> list[tuple[tuple[str, str], Conversation]]:
    """`((channel, topic), home)` for every root note this bot has written.

    The chat-side replacement for the participation ledger: where this agent
    has spoken on somebody else's behalf, and on whose behalf. Resolved
    topics are dropped — a finished conversation is not one to be called back
    into — and so is anything the search matched that does not parse as a
    root note.

    `include_resolved` keeps them, under their **bare** name: the reader that
    wants them is the one placing threads beside a served run, and the post
    that names an agent is very often the post that finishes the
    conversation. Front Desk lost a completion report on 2026-09-08 to
    exactly that: the task reported and resolved its topic in the same
    second, the callback run got no thread for it, read "post there to start
    it" as the latest word, and posted a second start into a topic that no
    longer existed under that name.

    **The effective anchor decides, not the first note found**
    (`routine_tests` p2 ex1, B2): a topic this bot deliberately corrected
    with `[selfnote][rootchat-moved]` is listed under the home it was moved
    to, so a corrected delegate appears beside its real home in `threads/`
    and its callback recovery is attributed to the run that owns it — and
    not, as p2's manual repair found, still to the conversation that opened
    it by mistake.
    """
    return [(key, home) for key, home, _ in _rootchat_links(client, num_before, include_resolved=include_resolved)]


def _rootchat_links(
    client: ZulipClient,
    num_before: int = ROOTCHAT_HISTORY,
    *,
    include_resolved: bool = False,
) -> list[tuple[tuple[str, str], Conversation, int]]:
    """`rootchat_notes` with the id of the note that decided each row."""
    ordinary: dict[tuple[str, str], tuple[int, Conversation]] = {}
    moved: dict[tuple[str, str], tuple[int, Conversation]] = {}
    order: list[tuple[str, str]] = []
    for message in list(client.own_rootchat_notes(num_before)) + list(
        client.own_moved_notes(num_before)
    ):
        if message.get("type") != "stream":
            continue
        correction = parse_rootchat_moved(message.get("content"))
        home = correction if correction is not None else parse_rootchat(
            message.get("content")
        )
        if home is None:
            continue
        topic = str(message.get("subject") or "")
        channel = channel_name(message)
        if not topic or not channel:
            continue
        if topic.startswith(RESOLVED_TOPIC_PREFIX):
            if not include_resolved:
                continue
            topic = topic[len(RESOLVED_TOPIC_PREFIX):]
        key = (channel, topic)
        if key not in ordinary and key not in moved:
            order.append(key)
        if correction is not None:
            # The newest correction wins; a correction is not identity.
            message_id = int(message.get("id") or 0)
            if message_id >= moved.get(key, (-1, None))[0]:
                moved[key] = (message_id, home)
        elif key not in ordinary:
            # The earliest ordinary note anchors; later ones repeat.
            ordinary[key] = (int(message.get("id") or 0), home)
    return [
        (key, *reversed(moved[key] if key in moved else ordinary[key]))
        for key in order
        if key in moved or key in ordinary
    ]


def remotes_for_home(
    client: ZulipClient, channel: str, topic: str, num_before: int = ROOTCHAT_HISTORY,
    *, home_messages: list[dict] | None = None,
) -> list[Conversation]:
    """Every conversation this one has reached out to, oldest note first.

    The list of threads a run serving `<channel>/<topic>` is party to, which
    is what decides the `threads/` folder it gets. A resolved remote is still
    one of them, named without its `\u2714 `: the run that is called back by
    a completion report must be able to read that report.
    """
    home = Conversation(channel, topic)
    found: list[Conversation] = []
    for (remote_channel, remote_topic), anchored, note_id in _rootchat_links(
        client, num_before, include_resolved=True
    ):
        if home_messages is None:
            if anchored != home:
                continue
        elif not _names_this_home(client, anchored, note_id, home, home_messages):
            continue
        remote = Conversation(remote_channel, remote_topic)
        if remote not in found:
            found.append(remote)
    return found


def _names_this_home(client, anchored: Conversation, note_id: int, home: Conversation,
                     home_messages: list[dict]) -> bool:
    """Whether a root note means *this* conversation, judged against the
    history the serving read (robust_workflow p2 step 2): its anchor is a
    post here (whatever name the note carries — home was renamed); or it
    names this conversation and is not contradicted — no anchor from
    elsewhere in this channel, not older than the conversation now holding
    the name (a reused name inherits nothing)."""
    ids = {int(m.get("id") or 0) for m in home_messages}
    anchor = int(anchored.anchor or 0)
    if anchor and anchor in ids:
        return True
    if (anchored.channel, _bare_topic(anchored.topic)) != (home.channel, _bare_topic(home.topic)):
        return False
    complete = len(home_messages) < ROOTCHAT_HISTORY
    oldest = min(ids) if ids else 0
    if not complete or not ids:
        return True
    if anchor and anchor >= oldest:
        # An anchor that is not here. A callback serving used to write the
        # calling post's id (another channel): that one says nothing.
        where = conversation_of(client, anchor) if hasattr(client, "message") else None
        if where is not None and where.channel == home.channel:
            return False
    return note_id > oldest


def _bare_topic(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def locate(client: ZulipClient, conversation: Conversation) -> Conversation | None:
    """Where a conversation is **now**: by its anchor message when it has
    one, else by its name across the resolve rename.

    `explicit_reply` p1 step 3. A display name is reusable — a resolve
    renames, a retirement frees the name for a replacement — so a delivery
    that follows a name can land in a conversation the request never knew.
    With an anchor the message's own location decides (`conversation_of`),
    including a `✔ ` name, which is where the reply belongs when the topic
    was closed while the run was in flight. An anchor that Zulip says is
    gone falls back to the name under the resolve rule; a name that exists
    nowhere is None — **absent**, never guessed.
    """
    anchor_gone = False
    if conversation.anchor and hasattr(client, "message"):
        try:
            found = conversation_of(client, int(conversation.anchor), strict=True)
            anchor_gone = found is None
        except Exception:  # noqa: BLE001 - a lookup that got no answer is not "gone"
            found = None
        if found is not None and found.channel == conversation.channel:
            return Conversation(found.channel, found.topic, int(conversation.anchor))
    topic = conversation.topic
    if topic.startswith(RESOLVED_TOPIC_PREFIX):
        return conversation
    try:
        names = client.channel_topics(client.stream_id(conversation.channel))
    except Exception:  # noqa: BLE001 - a lookup that got no answer is not "gone"
        return conversation
    if topic in names:
        return conversation
    resolved = f"{RESOLVED_TOPIC_PREFIX}{topic}"
    if resolved in names:
        return Conversation(conversation.channel, resolved, conversation.anchor)
    # The name is listed nowhere. That is "gone" only when the anchor was
    # confirmed deleted as well; a name alone is not evidence, and the
    # conversation is answered as asked.
    return None if anchor_gone else conversation


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
    client: ZulipClient, channel: str, topic: str, num_before: int, *, strict: bool = False
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

    `strict=True` re-raises a read that never got an answer, so an empty list
    means the conversation really is empty under both names rather than
    "something went wrong and here is a list anyway". A refusal
    (`ZulipRejected` — no such channel, not yours to read) is still an answer
    and still returns `[]`.
    """
    try:
        history = client.topic_history(channel, topic, num_before=num_before)
    except ZulipRejected:
        history = []
    except ZulipError:
        if strict:
            raise
        history = []
    if history or topic.startswith(RESOLVED_TOPIC_PREFIX):
        return history
    try:
        return client.topic_history(
            channel, f"{RESOLVED_TOPIC_PREFIX}{topic}", num_before=num_before
        )
    except ZulipRejected:
        return []
    except ZulipError:
        if strict:
            raise
        return []


def conversation_of(
    client: ZulipClient, message_id: int, *, strict: bool = False
) -> Conversation | None:
    """Where a message is **now**, or None when it is gone.

    A message id is the one identifier no rename touches, so this is how a
    conversation whose display name is reusable stays findable. The topic is
    returned exactly as it stands — including a `\u2714 ` prefix — because
    that is the name it can be read under; stripping it would name a topic
    that may not exist.

    Deleted is **absent**. A caller must not fall back to a topic of the
    remembered name: that name may have been taken over by work this id knows
    nothing about, which is the whole reason identity is an id.

    `strict=True` is passed straight through to `ZulipClient.message`: with
    it, `None` means Zulip said the message is not there, and a lookup that
    never got an answer raises instead. Use it wherever `None` is about to
    become a terminal decision.
    """
    # Only passed when it was asked for: the lenient path stays the exact
    # call it has always been, so every stand-in client written against the
    # old signature keeps working and only a strict caller needs the new one.
    message = (
        client.message(int(message_id), strict=True) if strict
        else client.message(int(message_id))
    )
    if message is None:
        return None
    channel = channel_name(message)
    topic = message.get("subject")
    if not channel or not isinstance(topic, str) or not topic.strip():
        return None
    return Conversation(channel, topic)


def inherited_rootchat(
    client: ZulipClient,
    history: list[dict],
    self_id: int,
    num_before: int = ROOTCHAT_HISTORY,
) -> Conversation | None:
    """This bot's anchor, found through **one** replacement hop.

    `routine_tests` p2 ex1, problem A. Retiring a plan renames its whole
    topic — `retire_conversation` moves every message in it, other agents'
    root notes included — and the replacement then takes the freed display
    name. A third party anchored in the retired conversation therefore finds
    no note of its own in the live topic and, before this, ignored the
    mention: p2 watched autolab name Front correctly, twice, and Front refuse
    both while its anchor sat in `\u2714 retired-…`.

    Nobody has to forge a note to fix it, because the replacing agent already
    writes the relation: `[selfnote][replaces] <message id>` names the
    retired work's anchor by id, and a message id survives a rename. So: read
    that pointer (whoever wrote it — see `replaced_anchor`), resolve the id to
    the conversation it is in now, and look **there** for this bot's own root
    note. The note found must still be this bot's own; an anchor belonging to
    somebody else is not this bot's business.

    **One hop, then stop.** A replacement of a replacement is not followed:
    the relation is a fact about the conversation that wrote it, and chaining
    it would turn a bounded lookup into a walk whose length nobody declared.

    A missing pointer, a malformed one, a deleted target, or a target with no
    note of ours all produce `None`. None of them falls back to a topic of the
    remembered name — the reused display name is precisely what cannot be
    trusted here.
    """
    anchor_id = replaced_anchor(history)
    if anchor_id is None:
        return None
    previous = conversation_of(client, anchor_id)
    if previous is None:
        return None
    return effective_rootchat(
        topic_history_across_resolve(
            client, previous.channel, previous.topic, num_before
        ),
        self_id,
    )


def parent_rootchat(
    client: ZulipClient,
    history: list[dict],
    self_id: int,
    num_before: int = ROOTCHAT_HISTORY,
) -> Conversation | None:
    """This bot's anchor, found through the conversation **this one was
    opened for**, one hop.

    `robust_workflow` p1 step 3. A conversation an agent opens on behalf of
    another — autolab's task topics under a mission's `workplan-` topic —
    carries its opener's root note naming that parent. The requester that
    asked in the parent has an anchor *there*; when the opener starts the
    child itself, nobody has posted into the child on the requester's
    behalf, so the child holds no note of the requester's. The answer the
    child sends back still belongs to the conversation that asked for the
    work, and this finds it: the earliest root note written by somebody
    else names the parent, and the parent's own anchor of ours is home.

    One hop, like `inherited_rootchat`, and never an override: a note of our
    own in this conversation always wins (`rootchat_home`).
    """
    for message in history:
        if message.get("sender_id") == self_id:
            continue
        parent = parse_rootchat(message.get("content"))
        if parent is None:
            continue
        return effective_rootchat(
            topic_history_across_resolve(client, parent.channel, parent.topic, num_before),
            self_id,
        )
    return None


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
    anchored and never inherited — a mention that is somebody else's business.

    Read across the resolve rename, because the post that names an agent is
    very often the post that finishes the conversation.

    **This topic's own anchor always wins.** Only when there is none is the
    `replaces` relation followed, one hop, into the conversation this one was
    opened to replace (`inherited_rootchat`), and then the opener's root note
    into the conversation this one was opened for (`parent_rootchat`). An
    inherited anchor is a fallback for a conversation that has not been
    anchored yet, never an override of one that has.
    """
    history = topic_history_across_resolve(client, channel, topic, num_before)
    home = effective_rootchat(history, self_id)
    if home is not None:
        return home
    return inherited_rootchat(client, history, self_id, num_before) or parent_rootchat(
        client, history, self_id, num_before
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
