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

# Long-poll socket timeout. Zulip holds the connection open until an event or
# its own heartbeat; this is only the client-side ceiling.
POLL_TIMEOUT_SECONDS = 90

# How long the listener waits after a failed Zulip call before trying again.
RETRY_SECONDS = 5


class ZulipError(Exception):
    """A Zulip API call failed for a reason the caller cannot ignore."""


class QueueExpired(ZulipError):
    """The event queue is gone (BAD_EVENT_QUEUE_ID). Re-register and continue."""


class ZulipTimeout(ZulipError):
    """The call hit the client-side timeout. On a long poll this is normal."""


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
        if method in ("POST", "DELETE"):
            data = query.encode("utf-8")
        elif query:
            url = f"{url}?{query}"
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Basic {self._auth}")
        if data is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._ssl) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
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


def dm_partners(message: dict, self_id: int) -> list[int]:
    """Everyone in the DM except the bot, in Zulip's own order."""
    recipients = message.get("display_recipient")
    if not isinstance(recipients, list):
        return []
    return [r["id"] for r in recipients if isinstance(r, dict) and r.get("id") != self_id]


def is_dm_for_us(message: dict, self_id: int) -> bool:
    """A private message from somebody else. The bot's own DMs echo back."""
    return message.get("type") == "private" and message.get("sender_id") != self_id


def log(message: str) -> None:
    """Default listener log line: UTC-stamped, unbuffered, on stderr."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def serve(client: ZulipClient, handler, log=log) -> None:
    """Long-poll for DMs addressed to this bot and pass each to `handler`.

    `handler(client, message, self_id)` is called for every direct message
    from somebody else; anything it raises is logged and the loop continues.
    Deliberately dumb: no queue persistence, no delivery guarantees. A DM that
    arrives while this is down is lost and the sender can resend.
    """
    self_id: int | None = None
    queue_id: str | None = None
    last_event_id = -1
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
        except ZulipTimeout:
            continue  # nothing happened within the poll window
        except QueueExpired as error:
            log(f"event queue expired ({error}); re-registering")
            queue_id = None
            continue
        except ZulipError as error:
            log(f"zulip call failed: {error}; retrying in {RETRY_SECONDS}s")
            queue_id = None
            time.sleep(RETRY_SECONDS)
            continue
        for event in events:
            last_event_id = max(last_event_id, int(event.get("id", last_event_id)))
            if event.get("type") != "message":
                continue
            message = event.get("message") or {}
            if not is_dm_for_us(message, self_id):
                continue
            try:
                handler(client, message, self_id)
            except Exception as error:  # one bad message must not end the loop
                log(f"handler failed on message #{message.get('id')}: {error!r}")
