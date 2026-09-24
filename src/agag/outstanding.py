"""Which requests for somebody's answer are still outstanding.

`clearer_chat_ui` step 2. `agag.post` lets a post say "I cannot go on until
<user> answers"; this reads a conversation's history and says, for every
such post, where it stands now. It is a pure function of the messages it
is given — no call, no store of its own — so a restart, a second process or
a mirror rebuilt from nothing reconstructs exactly the same answer from the
same recorded history.

**Identity is the request's message id.** A rename, a resolve or a reused
topic name does not change it; a caller that follows a conversation by its
anchor (`agag.zulip.locate`, the relay's desk and argue ids) hands this the
history found there.

The request's own post keeps its intent forever — a question that has been
answered is still a question in the record, and a room showing old dialogue
labels it as one. What changes is its **state**, derived from what came
after it:

| state | meaning |
|---|---|
| `pending` | nothing addressed has answered it yet: `to` is being asked |
| `overtaken` | `to` spoke after the asker last read the conversation and before the question landed; the asker owes a look at that input, so `to` is not being asked yet |
| `answered` | `to` responded (how: `reference`, `quote` or `next_post`) — a receipt, **not** approval, acceptance or completion |
| `withdrawn` | the asker referenced its own request in a later post (`re=`): it no longer stands |
| `superseded` | the asker asked `to` again and referenced this one: the newer request stands instead |
| `closed` | still unanswered when the conversation was ✔'d; it comes back as `pending` if the conversation is reopened |

**Correlation, strongest first.**

1. An explicit reference: a post by `to` whose `ag-post` line names the
   request (`re=`), or Zulip's own quote-and-reply link to it
   (`agag.post.quoted_ids`). It settles exactly the requests it names.
2. The next post: a post by `to` that references nothing settles the one
   request pending to `to` — **only when there is exactly one**. With two
   or more pending, it settles none, and is listed as an `unmatched`
   reply so a room can ask which question it answered; two questions never
   both disappear because somebody spoke. A post whose `re=` names no open
   request is about something else and settles nothing.
3. An explicit non-answer: a post by `to` marked `answer=none` settles
   nothing and is never `unmatched`, however many requests are pending —
   the person said so. It is still their speech, so it overtakes a request
   composed before it like any other post.

Only speech by `to` counts. Progress posts, acknowledgements, selfnotes,
Zulip's system notices and anybody else's posts — a third party, another
agent, the asker's own later reports — settle nothing.

**The processed-input boundary.** A request written by a listener carries
`seen=<id>`, the newest post that serving had read. When `to` spoke after
`seen` and before the request was posted, the question was composed without
that input: the listener is already due to serve it, so the request is
`overtaken` — shown as "your newer message is being read", never as
"waiting for you". It stands again as `pending` once the asker speaks after
it (the serving that read the input has had its say) and the asker has not
withdrawn it. A request with no `seen` (posted by `agentchat send` from
inside a run) is never overtaken.

**Uncertainty is said, not hidden.**

- `complete=False` (the history read did not reach the conversation's
  beginning): an earlier request to the same person may be outside the
  window, so a `next_post` settlement is marked `certain=False`, and the
  result says `history incomplete`.
- `stale=True` (the mirror could not confirm it is current): every state is
  "as of the last update", said once in `uncertain`.
- Edits are read as they are now: an edited request that lost its line is
  no longer a request, an edited reply that gained a reference counts from
  then; `edited` says a post was changed. A deleted request is absent; a
  deleted answer returns its request to `pending`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .post import PROGRESS, RESPONSE_REQUEST, parse_post, quoted_ids
from .selfnote import is_progress, is_speech

PENDING, OVERTAKEN, ANSWERED = "pending", "overtaken", "answered"
WITHDRAWN, SUPERSEDED, CLOSED = "withdrawn", "superseded", "closed"
STATES = (PENDING, OVERTAKEN, ANSWERED, WITHDRAWN, SUPERSEDED, CLOSED)
#: The states in which the request still stands.
OPEN_STATES = (PENDING, OVERTAKEN)

SCHEMA = "ag.outstanding.v1"

__all__ = [
    "ANSWERED",
    "CLOSED",
    "OPEN_STATES",
    "OVERTAKEN",
    "PENDING",
    "SCHEMA",
    "STATES",
    "SUPERSEDED",
    "WITHDRAWN",
    "Outstanding",
    "Request",
    "read_requests",
]


@dataclass
class Request:
    """One response request and where it stands."""

    id: int
    sender_id: int
    sender_name: str
    to: int
    to_name: str
    ask: str | None
    text: str
    timestamp: int = 0
    seen: int | None = None
    edited: bool = False
    state: str = PENDING
    #: The post that settled it (answered, withdrawn, superseded).
    settled_by: int | None = None
    #: `reference`, `quote` or `next_post` for an answer.
    how: str | None = None
    #: False when the settlement rests on a history known to be partial.
    certain: bool = True
    #: Posts by `to` that overtook it (between `seen` and the request).
    overtaken_by: list[int] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Outstanding:
    """Every response request in a conversation, in order, and what could
    not be established."""

    requests: list[Request] = field(default_factory=list)
    #: Replies by a request's recipient that named nothing while several of
    #: their requests were pending: `{reply id: [request ids]}`.
    unmatched: dict[int, list[int]] = field(default_factory=dict)
    uncertain: list[str] = field(default_factory=list)
    closed: bool = False

    @property
    def pending(self) -> list[Request]:
        """The requests waiting for their recipient now."""
        return [r for r in self.requests if r.state == PENDING]

    def pending_for(self, user_id: int) -> list[Request]:
        return [r for r in self.pending if r.to == int(user_id)]

    def by_id(self) -> dict[int, Request]:
        return {r.id: r for r in self.requests}

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "requests": [r.as_dict() for r in self.requests],
            "pending": [r.id for r in self.pending],
            "unmatched": {str(k): v for k, v in self.unmatched.items()},
            "uncertain": list(self.uncertain),
            "closed": self.closed,
        }


def _names(messages) -> dict[int, str]:
    return {int(m["sender_id"]): str(m.get("sender_full_name") or "") for m in messages
            if m.get("sender_id") is not None}


def read_requests(
    messages,
    *,
    complete: bool = True,
    closed: bool = False,
    stale: bool = False,
    is_ack=lambda content: False,
    names: dict[int, str] | None = None,
) -> Outstanding:
    """Where every response request in `messages` (one conversation, any
    order) stands. `closed` is whether the conversation is ✔'d now;
    `complete` whether the read reached its beginning; `stale` whether the
    source could not confirm it is current; `is_ack` recognizes a
    listener's acknowledgement; `names` adds display names for recipients
    who have not spoken here."""
    rows = sorted((m for m in messages or () if m.get("id") is not None), key=lambda m: int(m["id"]))
    known = {**(names or {}), **_names(rows)}
    result = Outstanding(closed=bool(closed))
    requests: dict[int, Request] = {}

    for message in rows:
        if not is_speech(message):
            continue
        content = str(message.get("content") or "")
        mid = int(message["id"])
        sender = int(message.get("sender_id") or 0)
        if is_ack(content.strip()):
            continue
        parsed = parse_post(content)
        meta = parsed.meta
        progress = is_progress(content) or (meta is not None and meta.intent == PROGRESS)
        refs = list(meta.re) if meta is not None else []
        refs += [i for i in quoted_ids(content) if i not in refs]

        # The asker speaking again: its own references withdraw or
        # supersede, and any speech lets an overtaken request stand again.
        if not progress:
            for request in requests.values():
                if request.sender_id != sender or request.id >= mid:
                    continue
                if request.id in refs and request.open:
                    superseding = meta is not None and meta.intent == RESPONSE_REQUEST and meta.to == request.to
                    request.state = SUPERSEDED if superseding else WITHDRAWN
                    request.settled_by = mid
                elif request.state == OVERTAKEN and (meta is None or meta.seen is None
                                                     or meta.seen >= max(request.overtaken_by)):
                    request.state = PENDING

        # The recipient speaking: explicit references first, else the one
        # unambiguous pending request — unless they said it answers nothing.
        if not progress and not (meta is not None and meta.not_answer):
            mine = [r for r in requests.values() if r.to == sender and r.sender_id != sender]
            named = [r for r in mine if r.id in refs and r.open]
            if named:
                how = "reference" if meta is not None and any(r.id in meta.re for r in named) else "quote"
                for request in named:
                    request.state, request.settled_by, request.how = ANSWERED, mid, how
            elif meta is None or not meta.re:
                # An `re=` that names no open request of theirs is about
                # something else: it settles nothing rather than being
                # guessed into an answer. A quote of some other post is
                # ordinary conversation and falls through to this rule.
                waiting = [r for r in mine if r.open]
                if len(waiting) == 1:
                    request = waiting[0]
                    request.state, request.settled_by, request.how = ANSWERED, mid, "next_post"
                    request.certain = bool(complete)
                elif len(waiting) > 1:
                    result.unmatched[mid] = [r.id for r in waiting]

        if meta is not None and meta.requests_response and meta.to != sender:
            request = Request(
                id=mid, sender_id=sender, sender_name=str(message.get("sender_full_name") or ""),
                to=int(meta.to), to_name=known.get(int(meta.to), ""), ask=meta.ask, text=parsed.text,
                timestamp=int(message.get("timestamp") or 0), seen=meta.seen,
                edited=bool(message.get("last_edit_timestamp")),
            )
            if meta.seen is not None:
                request.overtaken_by = [
                    int(m["id"]) for m in rows
                    if meta.seen < int(m["id"]) < mid and int(m.get("sender_id") or 0) == request.to
                    and is_speech(m) and not is_ack(str(m.get("content") or "").strip())
                    and not is_progress(m.get("content"))
                ]
                if request.overtaken_by:
                    request.state = OVERTAKEN
            requests[mid] = request

    if closed:
        for request in requests.values():
            if request.open:
                request.state = CLOSED
    result.requests = [requests[k] for k in sorted(requests)]
    if not complete:
        result.uncertain.append("history incomplete: an earlier request may be outside what was read")
    if stale:
        result.uncertain.append("the source is stale: states are as of its last update")
    return result
