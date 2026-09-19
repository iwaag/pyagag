"""One serving's lifecycle, kept where a crash cannot take it.

`explicit_reply` p1 step 1. Until now the only record that a request had
been answered was the conversation itself, read as "who spoke last". That
reading is wrong in four ways a run can end: the ack is this bot's own post,
so a crash right after it looked like an answer; a final send whose response
was lost looked like no answer at all and was retried into a duplicate; a
handler exception left the queue entry finished; and a post arriving during
a run that resolved the topic on its way out was never served.

What replaces it is a **serving record** with five stages, each written when
it becomes true and not before:

    received   the queue took the entry; the trigger message ids are recorded
    acked      the acknowledgement is on the conversation (its message id)
    executed   the handler returned; the input boundary it processed is
               recorded (`input_up_to`) and the requester it answers
    prepared   the reply text is on disk, with where it goes, *before* the
               send — so an interrupted send is retried as a delivery of the
               same text, never as another run of the model
    delivered  the posted reply's message id was confirmed (by the send's
               answer or by reading it back after an ambiguous one)

and two ways out that are not stages: `failed`, a terminal failure written
out loud with its reason, and `interrupted`, what a restart writes over a
record it found between `received` and `prepared` — the evidence is kept
for the run that serves the same input next.

The record lives in the listener's own queue file (`agag.listen.Queue`) and
reaches `serve_topic` through the thread-local `current()`, so no consumer
passes it by hand: the executor binds the journal for the entry it serves,
and every `serve_topic` call made on that thread writes into it. Outside a
listener — a test, a one-off command — `current()` is None and the journal
is a `NullJournal` that records nothing.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol

#: The stages, in order, and the two exits.
RECEIVED, ACKED, EXECUTED, PREPARED, DELIVERED = "received", "acked", "executed", "prepared", "delivered"
FAILED, INTERRUPTED = "failed", "interrupted"
STAGES = (RECEIVED, ACKED, EXECUTED, PREPARED, DELIVERED)

__all__ = [
    "ACKED",
    "DELIVERED",
    "EXECUTED",
    "FAILED",
    "INTERRUPTED",
    "PREPARED",
    "RECEIVED",
    "STAGES",
    "Journal",
    "NullJournal",
    "Serving",
    "bound",
    "current",
]


@dataclass
class Serving:
    """One serving record as the queue holds it. Field names are the
    column names; `None` is "not yet"."""

    id: int
    channel: str
    topic: str
    route: str
    trigger_id: int
    state: str = RECEIVED
    attempt: int = 1
    home_channel: str = ""
    home_topic: str = ""
    ack_id: int | None = None
    input_up_to: int | None = None
    requester_id: int | None = None
    requester_name: str = ""
    reply_channel: str = ""
    reply_topic: str = ""
    reply_text: str | None = None
    reply_after: int = 0
    resolve_after: bool = False
    resolved: bool = False
    delivered_id: int | None = None
    #: Reply-contract outcome (step 2): whether the model's output carried
    #: a marked reply, how many blocks, and what went wrong when none could
    #: be made.
    reply_marked: bool | None = None
    reply_blocks: int = 0
    reply_failure: str = ""
    run_record: str = ""
    failure: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.channel, self.topic, self.route)

    @property
    def replies_pending(self) -> bool:
        """A prepared reply that has not been confirmed delivered."""
        return self.state == PREPARED and bool(self.reply_text)


class Journal(Protocol):  # pragma: no cover - structural typing only
    """What `serve_topic` tells the record as a serving advances."""

    def home(self, channel: str, topic: str) -> None: ...
    def acked(self, message_id: int) -> None: ...
    def executed(self, input_up_to: int, *, requester_id: int | None, requester_name: str) -> None: ...
    def record(self, path: str) -> None: ...
    def reply_outcome(self, *, marked: bool | None, blocks: int, failure: str) -> None: ...
    def prepared(self, channel: str, topic: str, text: str, *, resolve_after: bool, after_id: int) -> None: ...
    def delivered(self, message_id: int | None) -> None: ...
    def resolved(self) -> None: ...
    def failed(self, reason: str) -> None: ...
    def recheck_failed(self, reason: str) -> None: ...
    def previous(self) -> Serving | None: ...
    def serving(self) -> Serving | None: ...
    @property
    def trigger_id(self) -> int: ...


class NullJournal:
    """The journal outside a listener: remembers the serving in memory, so a
    caller that wants the outcome of one `serve_topic` call can read it,
    and persists nothing."""

    def __init__(self, trigger_id: int = 0):
        self._serving = Serving(0, "", "", "", trigger_id)

    @property
    def trigger_id(self) -> int:
        return self._serving.trigger_id

    def home(self, channel: str, topic: str) -> None:
        self._serving.home_channel, self._serving.home_topic = channel, topic

    def acked(self, message_id: int) -> None:
        self._serving.state, self._serving.ack_id = ACKED, message_id

    def executed(self, input_up_to: int, *, requester_id: int | None, requester_name: str) -> None:
        self._serving.state = EXECUTED
        self._serving.input_up_to = input_up_to
        self._serving.requester_id, self._serving.requester_name = requester_id, requester_name

    def record(self, path: str) -> None:
        self._serving.run_record = path

    def reply_outcome(self, *, marked: bool | None, blocks: int, failure: str) -> None:
        self._serving.reply_marked, self._serving.reply_blocks, self._serving.reply_failure = marked, blocks, failure

    def prepared(self, channel: str, topic: str, text: str, *, resolve_after: bool, after_id: int) -> None:
        s = self._serving
        s.state, s.reply_channel, s.reply_topic, s.reply_text = PREPARED, channel, topic, text
        s.resolve_after, s.reply_after = resolve_after, after_id

    def delivered(self, message_id: int | None) -> None:
        self._serving.state, self._serving.delivered_id = DELIVERED, message_id

    def resolved(self) -> None:
        self._serving.resolved = True

    def failed(self, reason: str) -> None:
        self._serving.state, self._serving.failure = FAILED, reason

    def recheck_failed(self, reason: str) -> None:
        self._serving.extra["recheck_failed"] = reason

    def previous(self) -> Serving | None:
        return None

    def serving(self) -> Serving:
        return self._serving


_local = threading.local()


def current() -> Journal | None:
    """The journal bound to this thread's serving, or None outside one."""
    return getattr(_local, "journal", None)


@contextmanager
def bound(journal: Journal):
    """Bind `journal` as this thread's serving record for the block."""
    previous = getattr(_local, "journal", None)
    _local.journal = journal
    try:
        yield journal
    finally:
        _local.journal = previous
