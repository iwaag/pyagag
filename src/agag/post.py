"""What a post is for: progress, a report, or a request for somebody's answer.

`clearer_chat_ui` step 1. Until now a reader could tell who spoke and whom
they named, and nothing else. A mention hands the turn over mechanically
(`agag.topics.mention_of`) but says nothing about whether an answer is
wanted: "done, here is the result" and "which of these two do you want?"
both name the requester the same way, and a progress line looks like
either. The only inference anybody made was `agag.trace`'s
`awaiting_human` — "the agent answered last in a human conversation" —
which is true of every report ever posted.

The contract (`ag.post.v1`, `docs/post-intent-v1.md`) is one machine line at
the very end of a post, outside any code fence:

    `ag-post intent=response_request to=8 ask=question`

- `intent` is one of `progress` (work is under way; nothing is asked),
  `report` (information or a result; nothing is asked) and
  `response_request` (the poster waits for `to`'s answer).
- `to` is the **user id** the request is addressed to — required for a
  response request and meaningless for anything else.
- `ask` is an optional nuance of a request: `question` or `confirmation`.
- `re` names the request(s) a post answers, by message id
  (`re=9001` or `re=9001,9005`), and may stand without an intent: a
  human's reply through a room carries only that.
- `answer=none` says the post is **not** an answer to anything, even
  where the next-post rule would have read it as one (a person's aside
  while a question waits). It is the explicit third choice beside "let the
  next post decide" (no field) and "this answers #n" (`re=`); the two
  explicit ones contradict each other, so `answer=none` with `re=` is
  malformed.
- `seen` is the newest message the poster had read when it wrote the
  post — a serving's processed-input boundary. The listener writes it on
  a request, so a reader can tell that the recipient spoke *after* the
  asker read the conversation and *before* the question landed
  (`agag.outstanding`: the question is overtaken, not awaiting them).

Semantic intent is deliberately apart from everything else a post can be:
the sender is Zulip's, an ack is a transport receipt (`agag.agent.is_ack`),
execution state is the serving journal's. **A post without the line is
unclassified**, and unclassified never means "somebody is waiting".

The line is in the same message as the text — one write, so the meaning
cannot be lost separately from the words, and a prepared reply redelivered
after a crash carries it byte for byte (`agag.delivery` matches on the
whole content). Zulip shows it as a small code span; the rooms and every
agent-facing rendering (`label`) replace it with its meaning, so nobody
reads — and no model learns to imitate — the raw line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

#: The version name of the wire format, for documentation and records.
SCHEMA = "ag.post.v1"
#: The first word inside the code span.
TOKEN = "ag-post"

PROGRESS, REPORT, RESPONSE_REQUEST = "progress", "report", "response_request"
INTENTS = (PROGRESS, REPORT, RESPONSE_REQUEST)
QUESTION, CONFIRMATION = "question", "confirmation"
ASKS = (QUESTION, CONFIRMATION)
#: `answer=` values: only the explicit non-answer; an answer is `re=`.
NONE = "none"
ANSWERS = (NONE,)
#: When several marked blocks of one reply disagree, the post is the
#: strongest thing any of them is: a report that also asks is a request.
STRENGTH = {PROGRESS: 0, REPORT: 1, RESPONSE_REQUEST: 2}

_LINE = re.compile(r"^[ \t]*`" + re.escape(TOKEN) + r"(?P<body>(?:[ \t][^`\n]*)?)`[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\n]*)$")
_PAIR = re.compile(r"^(?P<key>[a-z_]+)=(?P<value>\S+)$")

__all__ = [
    "ANSWERS",
    "ASKS",
    "CONFIRMATION",
    "INTENTS",
    "NONE",
    "PROGRESS",
    "QUESTION",
    "REPORT",
    "RESPONSE_REQUEST",
    "SCHEMA",
    "TOKEN",
    "ParsedPost",
    "PostMeta",
    "combine",
    "compose",
    "describe",
    "label",
    "merge",
    "parse_attributes",
    "parse_post",
    "quoted_ids",
    "strip",
]


@dataclass(frozen=True)
class PostMeta:
    """The meaning of one post. Every field optional; an empty meta is
    "unclassified" and composes to no line at all."""

    intent: str | None = None
    to: int | None = None
    ask: str | None = None
    re: tuple[int, ...] = field(default_factory=tuple)
    seen: int | None = None
    #: `none`: explicitly not an answer to any request (see the module doc).
    answer: str | None = None

    @property
    def empty(self) -> bool:
        return self.intent is None and not self.re and self.answer is None

    @property
    def not_answer(self) -> bool:
        return self.answer == NONE

    @property
    def requests_response(self) -> bool:
        return self.intent == RESPONSE_REQUEST and self.to is not None

    def problem(self) -> str | None:
        """Why this meta is not a valid `ag.post.v1` line, or None."""
        if self.intent is not None and self.intent not in INTENTS:
            return f"unknown intent {self.intent!r} (one of {', '.join(INTENTS)})"
        if self.intent == RESPONSE_REQUEST and self.to is None:
            return "a response_request needs to=<user id>"
        if self.intent != RESPONSE_REQUEST and self.to is not None:
            return "to= is only for a response_request"
        if self.ask is not None and self.intent != RESPONSE_REQUEST:
            return "ask= is only for a response_request"
        if self.ask is not None and self.ask not in ASKS:
            return f"unknown ask {self.ask!r} (one of {', '.join(ASKS)})"
        if self.answer is not None and self.answer not in ANSWERS:
            return f"unknown answer {self.answer!r} (only answer=none; an answer names its request with re=)"
        if self.answer is not None and self.re:
            return "answer=none and re= contradict: a post either answers the named requests or none"
        if self.seen is not None and self.intent is None and not self.re:
            return "seen= says nothing without an intent or re="
        return None

    def line(self) -> str:
        """The wire line, or "" for an unclassified post."""
        if self.empty:
            return ""
        words = [TOKEN]
        if self.intent:
            words.append(f"intent={self.intent}")
        if self.to is not None:
            words.append(f"to={int(self.to)}")
        if self.ask:
            words.append(f"ask={self.ask}")
        if self.re:
            words.append("re=" + ",".join(str(int(i)) for i in self.re))
        if self.answer:
            words.append(f"answer={self.answer}")
        if self.seen is not None:
            words.append(f"seen={int(self.seen)}")
        return "`" + " ".join(words) + "`"

    def as_dict(self) -> dict:
        out: dict = {}
        if self.intent:
            out["intent"] = self.intent
        if self.to is not None:
            out["to"] = int(self.to)
        if self.ask:
            out["ask"] = self.ask
        if self.re:
            out["re"] = [int(i) for i in self.re]
        if self.answer:
            out["answer"] = self.answer
        if self.seen is not None:
            out["seen"] = int(self.seen)
        return out


@dataclass(frozen=True)
class ParsedPost:
    """`text` is the post as a person reads it (the line removed); `meta`
    what the line says (None when there is no line, or it is unusable);
    `error` why a line that is there could not be used."""

    text: str
    meta: PostMeta | None
    error: str | None = None

    @property
    def intent(self) -> str | None:
        return self.meta.intent if self.meta is not None else None


def parse_attributes(text: str, *, require_to: bool = True) -> tuple[PostMeta, str | None]:
    """`intent=… to=… ask=… re=…` words as a meta, and what is wrong with
    them (None when nothing is). Used for the wire line and for the
    attributes a run writes on its `ag-reply` fence — where `to` may be
    left out of a request (`require_to=False`): the listener addresses it
    to the requester it recorded. Unknown keys and
    malformed values are errors: in a breaking-change phase a misspelled
    key is a bug to see, not a field to skip."""
    values: dict = {}
    for word in str(text or "").split():
        match = _PAIR.match(word)
        if match is None:
            return PostMeta(), f"{word!r} is not key=value"
        key, value = match.group("key"), match.group("value")
        if key in values:
            return PostMeta(), f"{key}= given twice"
        if key == "intent":
            values["intent"] = value.lower()
        elif key == "ask":
            values["ask"] = value.lower()
        elif key == "answer":
            values["answer"] = value.lower()
        elif key == "to":
            if not value.isdigit():
                return PostMeta(), f"to={value} is not a user id"
            values["to"] = int(value)
        elif key == "seen":
            if not value.isdigit():
                return PostMeta(), f"seen={value} is not a message id"
            values["seen"] = int(value)
        elif key == "re":
            ids = value.split(",")
            if not all(part.isdigit() and int(part) > 0 for part in ids):
                return PostMeta(), f"re={value} is not a list of message ids"
            values["re"] = tuple(dict.fromkeys(int(part) for part in ids))
        else:
            return PostMeta(), f"unknown key {key}="
    meta = PostMeta(**values)
    if not require_to and meta.intent == RESPONSE_REQUEST and meta.to is None:
        return meta, replace(meta, to=0).problem()
    return meta, meta.problem()


def _trailer_index(lines: list[str]) -> int | None:
    """The index of the last non-blank line when it is a candidate `ag-post`
    line **outside** every code fence, else None. Fence-aware in the same
    CommonMark sense as `agag.reply.split_reply`: a line that only looks
    like the trailer inside a quoted code block is text."""
    last = max((i for i, line in enumerate(lines) if line.strip()), default=None)
    if last is None or not _LINE.match(lines[last]):
        return None
    fence: tuple[str, int] | None = None
    for line in lines[:last]:
        match = _FENCE.match(line)
        if match is None:
            continue
        marker = match.group("fence")
        if fence is None:
            fence = (marker[0], len(marker))
        elif marker[0] == fence[0] and len(marker) >= fence[1] and not match.group("info").strip():
            fence = None
    return last if fence is None else None


def parse_post(content) -> ParsedPost:
    """Split a post's raw content into what a person reads and what it
    means. A post with no line is unclassified (`meta=None`, no error). A
    line that is there but wrong is removed from the text all the same —
    it is a machine line either way — and reported in `error`, with the
    post read as unclassified."""
    raw = str(content or "")
    lines = raw.splitlines()
    index = _trailer_index(lines)
    if index is None:
        return ParsedPost(raw.strip(), None)
    text = "\n".join(lines[:index]).strip()
    body = _LINE.match(lines[index]).group("body")
    meta, error = parse_attributes(body)
    if error is None and meta.empty:
        error = "the ag-post line says nothing"
    if error is not None:
        return ParsedPost(text, None, error)
    return ParsedPost(text, meta)


def strip(content) -> str:
    """The post as a person reads it."""
    return parse_post(content).text


def compose(text: str, meta: PostMeta | None) -> str:
    """`text` with `meta`'s line appended — one message, one write. An
    existing line is replaced rather than stacked; an empty meta leaves the
    text unclassified. An invalid meta is refused (`ValueError`): what is
    written is always readable back."""
    body = strip(text)
    if meta is None or meta.empty:
        return body
    problem = meta.problem()
    if problem is not None:
        raise ValueError(problem)
    return f"{body}\n\n{meta.line()}" if body else meta.line()


def merge(metas) -> PostMeta | None:
    """One meta for a post written in several pieces (several `ag-reply`
    blocks): the strongest intent wins with its own `to` and `ask`; the
    references are the union, in order."""
    chosen: PostMeta | None = None
    refs: list[int] = []
    for meta in metas:
        if meta is None:
            continue
        refs += list(meta.re)
        if meta.intent is not None and (chosen is None or chosen.intent is None
                                        or STRENGTH[meta.intent] > STRENGTH[chosen.intent]):
            chosen = meta
    if chosen is None and not refs:
        return None
    base = chosen or PostMeta()
    return replace(base, re=tuple(dict.fromkeys(refs)), answer=None if refs else base.answer)


def combine(declared: PostMeta | None, handler: PostMeta | None) -> PostMeta | None:
    """One meta for a post whose words a run wrote (`declared`, from its
    `ag-reply` fence) and whose handler knows a state of its own
    (`TopicResult.meta`). Strength alone cannot decide this: it says nothing
    about whose recipient or which kind of request wins.

    - A handler's `response_request` is a **requirement**: the handler's
      own state needs somebody's answer (a task waiting for its requester's
      agreement), whatever the run called its words. The post is a request;
      `to` and `ask` are the handler's. Where the handler left one out, a
      request the run declared supplies it (`ask` only when the run asked
      the same person); a `to` still missing is filled by the listener
      with the requester it recorded.
    - Any other handler intent is a **default** for words that declared
      none: a run's own `progress`, `report` or question stands.
    - `re=` is the union, the run's references first; `seen` is written by
      the listener afterwards, never taken from either side.
    """
    refs = tuple(dict.fromkeys([*(declared.re if declared else ()), *(handler.re if handler else ())]))
    # An explicit non-answer is the words' own claim and yields to any reference.
    aside = None if refs else next((m.answer for m in (declared, handler) if m is not None and m.answer), None)
    if handler is not None and handler.intent == RESPONSE_REQUEST:
        asked = declared if declared is not None and declared.intent == RESPONSE_REQUEST else None
        to = handler.to if handler.to is not None else (asked.to if asked else None)
        ask = handler.ask
        if ask is None and asked is not None and (asked.to is None or asked.to == to):
            ask = asked.ask
        return PostMeta(intent=RESPONSE_REQUEST, to=to, ask=ask, re=refs, answer=aside)
    chosen = declared if declared is not None and declared.intent is not None else handler
    if chosen is None and not refs and aside is None:
        return None
    base = chosen or PostMeta()
    return PostMeta(intent=base.intent, to=base.to, ask=base.ask, re=refs, answer=aside)


def describe(meta: PostMeta | None, name_of=None) -> str:
    """The meaning in words, for a reader: `progress`, `report`,
    `asks Developer to answer (question)`, `answers #9001`. "" when there
    is nothing to say."""
    if meta is None:
        return ""
    words = []
    if meta.intent == PROGRESS:
        words.append("progress")
    elif meta.intent == REPORT:
        words.append("report")
    elif meta.intent == RESPONSE_REQUEST:
        who = (name_of(meta.to) if name_of else None) or f"user {meta.to}"
        words.append(f"asks {who} to answer" + (f" ({meta.ask})" if meta.ask else ""))
    if meta.re:
        words.append("answers " + ", ".join(f"#{i}" for i in meta.re))
    if meta.not_answer:
        words.append("not an answer to any request")
    return "; ".join(words)


def label(content, name_of=None, message_id: int | None = None) -> tuple[str, str]:
    """`(text, "(meaning) ")` for an agent-facing rendering: the post
    without its line, and a parenthesized prefix saying what it is ("" for
    an unclassified post). A request is labelled with its own id, which is
    what `re=` takes."""
    parsed = parse_post(content)
    words = describe(parsed.meta, name_of)
    if words and message_id and parsed.meta is not None and parsed.meta.intent == RESPONSE_REQUEST:
        words = f"{words}; request #{int(message_id)}"
    return parsed.text, (f"({words}) " if words else "")


_QUOTE = re.compile(r"/near/(?P<id>\d+)\)")


def quoted_ids(content) -> tuple[int, ...]:
    """Message ids a post quotes through Zulip's own quote-and-reply
    (`[said](…/near/<id>):`), the one explicit reference a person writing
    in Zulip itself makes without knowing this contract."""
    return tuple(dict.fromkeys(int(m.group("id")) for m in _QUOTE.finditer(str(content or ""))))
