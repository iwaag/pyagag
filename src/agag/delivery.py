"""Posting a reply so that it lands exactly once, or fails out loud.

`explicit_reply` p1 step 1. `ZulipClient.send_to_channel` has three
outcomes and until now the listener could tell two apart: the id came back
(delivered), or an exception (retry — which, when the server *had* accepted
the post and only the response was lost, posted the reply twice; or give
up, which lost it). The third outcome, the ambiguous one, is the whole
reason this module exists:

- A **rejection** (`ZulipRejected`, an answered 4xx) is terminal: the same
  text to the same place will be refused again. `DeliveryError` says so.
- Anything else (`ZulipError`: timeout, dropped connection, 5xx, 429) is
  **ambiguous**. Before sending again the conversation is read back and
  searched for this bot's own post of the same text newer than `after_id`,
  the floor below which nothing this serving posted can be. Found: that is
  the delivery, and its id is returned. Not found: one more attempt, after a
  bounded backoff.
- Attempts exhausted: `DeliveryError`, carrying the last error. The caller
  keeps the prepared text and retries the *delivery* later — never the run.

`after_id` is a message id, and message ids are realm-global and monotonic
in Zulip, so a floor taken from one conversation is valid in another. The
serving takes it from the newest input it processed and its own ack.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .zulip import ZulipClient, ZulipError, ZulipRejected, log as default_log

#: How many times one delivery is attempted before it is handed back.
ATTEMPTS = 3
#: Seconds between attempts, per attempt; the last value repeats.
BACKOFF = (1.0, 3.0, 9.0)
#: How much of the destination is read back to find an earlier delivery.
READ_BACK = 30

__all__ = ["ATTEMPTS", "BACKOFF", "READ_BACK", "DeliveryError", "deliver", "find_delivery", "redeliver"]


class DeliveryError(ZulipError):
    """A reply that could not be confirmed posted. `terminal` says whether
    trying again with the same text is pointless."""

    def __init__(self, message: str, *, terminal: bool, last: BaseException | None = None):
        super().__init__(message)
        self.terminal = terminal
        self.last = last


def find_delivery(
    client: ZulipClient, channel: str, topic: str, text: str, *, self_id: int, after_id: int,
    num_before: int = READ_BACK,
) -> int | None:
    """The id of this bot's post of `text` in the conversation, newer than
    `after_id`, or None. A read that fails is None too: the caller treats
    "could not look" like "not found" and tries the send again, which is the
    safe side only because the *next* attempt reads back first as well."""
    try:
        history = client.topic_history(channel, topic, num_before=num_before)
    except ZulipError:
        return None
    wanted = text.strip()
    for message in reversed(history):
        if int(message.get("id", 0)) <= after_id:
            break
        if message.get("sender_id") == self_id and str(message.get("content", "")).strip() == wanted:
            return int(message["id"])
    return None


def deliver(
    client: ZulipClient, channel: str, topic: str, text: str, *, self_id: int, after_id: int,
    attempts: int = ATTEMPTS, backoff: tuple[float, ...] = BACKOFF,
    sleep: Callable[[float], None] = time.sleep, log=default_log,
) -> int:
    """Post `text` and return its message id, confirmed.

    Reads back first when this text may already be there (a retry of an
    interrupted delivery passes the same `after_id` it had), so a delivery
    that landed is recognized rather than repeated.
    """
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1 or last is not None:
            found = find_delivery(client, channel, topic, text, self_id=self_id, after_id=after_id)
            if found is not None:
                log(f"delivery to {channel!r}/{topic!r} found on read-back as message {found}")
                return found
        try:
            return int(client.send_to_channel(channel, topic, text))
        except ZulipRejected as error:
            raise DeliveryError(f"{channel!r}/{topic!r} refused the reply: {error}", terminal=True, last=error) from error
        except ZulipError as error:
            last = error
            log(f"delivery to {channel!r}/{topic!r} uncertain (attempt {attempt}/{attempts}): {error!r}")
            if attempt < attempts:
                sleep(backoff[min(attempt - 1, len(backoff) - 1)])
    found = find_delivery(client, channel, topic, text, self_id=self_id, after_id=after_id)
    if found is not None:
        return found
    raise DeliveryError(
        f"could not confirm the reply in {channel!r}/{topic!r} after {attempts} attempt(s): {last!r}",
        terminal=False, last=last,
    )


def redeliver(
    client: ZulipClient, channel: str, topic: str, text: str, *, self_id: int, after_id: int, log=default_log, **kwargs,
) -> int:
    """`deliver` for a text that may already have landed: the read-back
    comes first, unconditionally."""
    found = find_delivery(client, channel, topic, text, self_id=self_id, after_id=after_id)
    if found is not None:
        log(f"prepared reply to {channel!r}/{topic!r} was already delivered as message {found}")
        return found
    return deliver(client, channel, topic, text, self_id=self_id, after_id=after_id, log=log, **kwargs)
