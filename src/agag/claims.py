"""A reply that claims an act it never did (failsafe p7).

`agent_guide` p2's live hold trial: Front's run-0183 answered the Omni
Agent's "I release the hold; stop following this request" in one turn with
no tool call — "Released hold #15837 … Recorded the disposition as
`withdrawn` …" — and nothing was recorded. Observer saw only the records (a
hold in force) and would never have asked; the Omni Agent noticed by
comparing the reply with the records, by hand (#15847). The same
conversation on the fixture recorded both acts 12 times out of 12: the
event is rare and model-side, and no guide sentence could be shown to help.

So the system checks, after every delivered reply:

- **The records a serving wrote are its window.** A listener serves one
  conversation at a time, and a run's tools post with the agent's own
  credential, so every post the agent's bot made between the serving's ack
  (or its start) and its delivered reply is that serving's — whatever the
  harness and wherever the post went. They are read off the listener's own
  mirror: no transcript, no Zulip call (`window_records`).
- **What the reply claims is read by a local model** (`OllamaReader`): the
  reply's own words only (never its `ag-post` line, which a reader took for
  a send in step 2's probe), answered as a list of acts — `hold`,
  `release`, `disposition`, `relation`, `accept`, `reserve`, `receipt`,
  `send` — each with the target the reply names. The model reads; it
  decides nothing.
- **Code judges** (`judge`): a claimed act is true when the window holds a
  record of that kind, or when one is already on record — naming the
  target, or in the target's conversation, or (no target) in the reply's
  own conversation — which is a reply restating an earlier act. Only a
  claim with no record anywhere is `missing`.

A mismatch is written into the reply's conversation as `[selfnote][claim]
{json}` beside the owner's own `[selfnote][start]` (`agag.selfnote`), so the
notice is what serves the agent again — not the stale decision — and every
conversational prompt carries the open claims (`notice`, appended by
`agag.topics.prompt_with_guide`). The serving that answers it settles it
(`[selfnote][claim-settled] #<claim> recorded|corrected #<reply>`); a
repeat of the same claim is attempt 2, written without a start: one
mismatch buys one serving, and the trace's `claim` candidate takes it to
Observer, who reports it to the owners. `docs` in
`devdocs/episodes/failsafe/p7/` (report1 is the contract).

The reader's endpoint is a host fact, `~/.config/agag/claims.toml` (or
`$XDG_CONFIG_HOME/agag/claims.toml`; `AGAG_CLAIMS_CONFIG` names another):

    [reader]
    url = "http://<host>:11434"
    model = "qwen3.8:27b-mxfp8"
    timeout = 60

No file, no reader: every serving is `unchecked` and says why — never
`clean`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .memo import is_memo_channel
from .selfnote import is_selfnote, note, parse_note

__all__ = [
    "ACTS",
    "CLAIM_TAG",
    "SETTLED_TAG",
    "Claim",
    "ClaimCheck",
    "OllamaReader",
    "ReaderError",
    "Record",
    "claim_note",
    "claims_of",
    "judge",
    "notice",
    "open_claims",
    "parse_claim",
    "parse_reader_output",
    "parse_settled",
    "reply_words",
    "settled_note",
    "window_records",
]

CLAIM_TAG = "claim"
SETTLED_TAG = "claim-settled"
#: The acts a reply can claim (report1's table), in the reader's words.
ACTS = ("hold", "release", "disposition", "relation", "accept", "reserve", "receipt", "send")
#: The notes that are each act's record. `send` has none: its record is the
#: post itself.
NOTE_TAGS = {
    "hold": ("hold",),
    "release": ("hold-release",),
    "disposition": ("disposition", "disposition-reversed"),
    "relation": ("relation", "rootchat"),
    "accept": ("acceptance", "state"),
    "reserve": ("approval",),
    "receipt": ("receipt", "served"),
}
ACT_OF_TAG = {tag: act for act, tags in NOTE_TAGS.items() for tag in tags}
#: Acts whose target must be named by the record itself: a release is of one
#: hold, a receipt of one answer. For the others a record in the target's
#: conversation is the same act (an acceptance note names its evidence, not
#: the mission it accepts).
PRECISE = ("release", "receipt")
#: The tool that makes each record, said in the notice (each `--help` says how).
TOOL = {
    "hold": "agentchat hold", "release": "agentchat release", "disposition": "agentchat disposition",
    "relation": "agentchat relation", "accept": "agentchat accept", "reserve": "agentchat reserve",
    "receipt": "agentchat receipt --repair", "send": "agentchat send",
}
#: `[state]` words that are an acceptance's record (`agag.acceptance`).
ACCEPT_WORDS = ("accepted", "done")
#: How long an attempt-1 claim may wait for its repair serving before the
#: trace calls it owed (a listener down, a queue stuck).
REPAIR_SECONDS = 600
CONFIG_VARIABLE = "AGAG_CLAIMS_CONFIG"
_IDS = re.compile(r"(?<![\w.])[#am]?(\d{2,})\b")
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```\s*$")
_POST_LINE = re.compile(r"^`?ag-post\b.*$", re.M)
_LEADING_MENTIONS = re.compile(r"^(?:\s*@\*\*[^*]+\*\*\s*)+")


# --- the records -------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """One post that is an act's record: `act`, the post's id and where it
    is, and every id it names (its own included) for matching a target."""

    act: str
    id: int
    channel: str
    topic: str
    ids: frozenset[int]
    text: str = ""

    def as_dict(self) -> dict:
        return {"act": self.act, "id": self.id, "where": f"{self.channel}/{self.topic}"}


def _bare(topic: str) -> str:
    from .zulip import RESOLVED_TOPIC_PREFIX

    topic = str(topic or "")
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def _ids(text: str) -> set[int]:
    return {int(n) for n in _IDS.findall(str(text or ""))}


def records_of(message, *, home: tuple[str, str] | None = None, is_ack=lambda content: False) -> list[Record]:
    """The act records one post is, or none. `message` is a mirror
    `Message` or a Zulip dict. A post of speech outside `home` (the reply's
    own conversation) is a `send`; a note is the act its tag records."""
    from .mirror.store import notes_of

    if hasattr(message, "as_zulip"):
        message = message.as_zulip()
    channel = str(message.get("display_recipient") or "")
    topic = str(message.get("subject") or "")
    ident = int(message.get("id") or 0)
    content = str(message.get("content") or "")
    if is_memo_channel(channel):
        return []  # a memo is presentation (`agag.memo`): never a record, never a send
    if is_selfnote(content):
        found = []
        for tag, value in notes_of(content):
            act = ACT_OF_TAG.get(tag)
            if act is None:
                continue
            if tag == "state" and (value.split() or [""])[0] not in ACCEPT_WORDS:
                continue
            if tag == "rootchat" and "rel=" not in value:
                continue  # a root note records a relation only when it says one
            found.append(Record(act, ident, channel, topic, frozenset(_ids(value) | {ident}), value))
        return found
    if home is not None and (channel, _bare(topic)) == (home[0], _bare(home[1])):
        return []
    if is_ack(content.strip()):
        return []
    return [Record("send", ident, channel, topic, frozenset({ident}), content[:200])]


def window_records(mirror, self_id: int, after: int, upto: int, *, home: tuple[str, str],
                   exclude: Iterable[int] = (), is_ack=lambda content: False) -> list[Record]:
    """Every record the agent's bot posted with an id in `(after, upto)`:
    the serving's own acts. `exclude` are the listener's own lines (the ack,
    the reply)."""
    skip = {int(i) for i in exclude if i}
    rows = mirror.store.messages_by_sender(int(self_id), since_id=int(after), upto_id=int(upto) - 1)
    found: list[Record] = []
    for message in rows:
        if message.id in skip or message.id <= after or message.id >= upto:
            continue
        found.extend(records_of(message, home=home, is_ack=is_ack))
    return found


# --- what the reply claims ---------------------------------------------------------


@dataclass
class Claim:
    """One act the reader says the reply claims."""

    act: str
    target: int = 0
    where: str = ""
    quote: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def label(self) -> str:
        what = {"hold": "a hold", "release": "a hold's release", "disposition": "a disposition",
                "relation": "a relation", "accept": "an acceptance", "reserve": "a reservation",
                "receipt": "a receipt", "send": "a post"}[self.act]
        on = f" #{self.target}" if self.target else ""
        where = f" in {self.where}" if self.where and self.act == "send" else ""
        return f"{what}{on}{where}"


class ReaderError(RuntimeError):
    """The reader gave no usable answer: the serving is unchecked."""


class Reader(Protocol):
    def read(self, text: str) -> list[Claim]: ...


READER_SYSTEM = """You read one reply an agent posted and list the acts the reply says its author performed.
The acts (use exactly these names):
- hold: placed a hold on work (a person keeps a decision)
- release: released a hold
- disposition: recorded a disposition (withdrawn, completed, cancelled, suppressed) or reversed one
- relation: recorded a conversation's relation (work or reference)
- accept: recorded the acceptance of a mission or task
- reserve: reserved the final approval for a person
- receipt: repaired or recorded a receipt for an answer
- send: posted or sent a message or request to another agent or conversation
List an act only when the reply says it was done now, by the author (e.g. "Released hold #12", "I asked autolab in pj-x", "記録しました", "依頼しました").
Do NOT list: something that already existed or was done earlier ("hold #12 is still in force", "my previous reply said…"), a command quoted or suggested but not said to be run, something planned, offered or asked about ("shall I release it?"), something another agent did. Posting this reply itself is not a send.
Give the target id when the reply names one (the hold id, the unit or request id, the mission id, the answer id), else 0. For send give the channel/topic or agent in "where".
Answer with JSON only: {"acts": [{"act": "...", "target": 0, "where": "", "quote": "the words that say it"}]}"""


def parse_reader_output(content: str) -> list[Claim]:
    """The reader's JSON, fences allowed; unknown acts dropped; anything
    unreadable is a `ReaderError`, never an empty list."""
    text = _FENCE.sub("", str(content or "").strip())
    try:
        document = json.loads(text)
    except ValueError as error:
        raise ReaderError(f"the reader's answer is not JSON: {text[:200]!r}") from error
    rows = document.get("acts") if isinstance(document, dict) else None
    if not isinstance(rows, list):
        raise ReaderError(f"the reader's answer has no acts list: {text[:200]!r}")
    found = []
    for row in rows:
        if not isinstance(row, dict) or row.get("act") not in ACTS:
            continue
        try:
            target = int(str(row.get("target") or 0).lstrip("#am") or 0)
        except ValueError:
            target = 0
        found.append(Claim(str(row["act"]), target, str(row.get("where") or ""), str(row.get("quote") or "")[:300]))
    return found


@dataclass
class OllamaReader:
    """One chat call to the host's local model (ollama's `/api/chat`),
    JSON-formatted, temperature 0, no thinking."""

    url: str
    model: str
    timeout: float = 60.0

    def read(self, text: str) -> list[Claim]:
        body = {"model": self.model, "stream": False, "format": "json", "think": False,
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": READER_SYSTEM},
                             {"role": "user", "content": f"The reply:\n\n{text}"}]}
        request = urllib.request.Request(self.url.rstrip("/") + "/api/chat", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                answer = json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as error:
            raise ReaderError(f"the reader at {self.url} did not answer: {error!r}") from error
        return parse_reader_output(str((answer.get("message") or {}).get("content") or ""))


def reply_words(text: str) -> str:
    """What the reader is given: the words, without the handoff mention in
    front and the `ag-post` line under them."""
    words = _POST_LINE.sub("", str(text or ""))
    return _LEADING_MENTIONS.sub("", words).strip()


# --- the judgment ------------------------------------------------------------------


def _where_matches(where: str, channel: str, topic: str) -> bool:
    said = str(where or "").lower()
    return bool(said) and any(part and part.lower() in said for part in (channel, _bare(topic)))


def _found_on_record(mirror, claim: Claim, self_id: int, home: tuple[str, str], before: int) -> Record | None:
    """A record of the claimed act made before the reply, outside the
    window: the reply restates it (report1, rule 2)."""
    if claim.act == "send":
        for message in reversed(mirror.store.messages_by_sender(int(self_id), upto_id=int(before) - 1, limit=2000)):
            for record in records_of(message, home=home):
                if record.act != "send":
                    continue
                if claim.target and claim.target in record.ids:
                    return record
                if claim.where and _where_matches(claim.where, record.channel, record.topic):
                    return record
                if claim.target:
                    target = mirror.message(claim.target)
                    if target is not None and (target.channel, _bare(target.topic)) == (record.channel, _bare(record.topic)):
                        return record
        return None
    target_place = None
    if claim.target and claim.act not in PRECISE:
        found = mirror.message(int(claim.target))
        if found is not None:
            target_place = (found.channel, _bare(found.topic))
    for tag in NOTE_TAGS[claim.act]:
        for row in mirror.notes(tag=tag):
            if row.message_id >= before:
                continue
            message = mirror.message(row.message_id)
            if message is None:
                continue
            for record in records_of(message):
                if record.act != claim.act:
                    continue
                place = (record.channel, _bare(record.topic))
                if claim.target:
                    if claim.target in record.ids or (target_place is not None and place == target_place):
                        return record
                elif place == (home[0], _bare(home[1])):
                    return record
    return None


def judge(claims: list[Claim], window: list[Record], *, mirror=None, self_id: int = 0,
          home: tuple[str, str] = ("", ""), before: int = 0) -> tuple[list[dict], list[dict]]:
    """`(missing, found)`: each claim with the record that shows it, or
    none. A record of the claimed kind in the window counts whatever target
    the reply named — the serving did an act of that kind — and otherwise a
    record already made (`_found_on_record`)."""
    missing, found = [], []
    for claim in claims:
        same = [r for r in window if r.act == claim.act]
        # A send is matched to the conversation the reply names when it can
        # be; when it cannot, any send the serving made stands for it
        # (report2: a named place is prose, and a false alarm buys a run).
        match = next((r for r in same if claim.act == "send" and _where_matches(claim.where, r.channel, r.topic)),
                     same[0] if same else None)
        how = "window"
        if match is None and mirror is not None:
            match = _found_on_record(mirror, claim, self_id, home, before)
            how = "on record"
        if match is None:
            missing.append(claim.as_dict())
        else:
            found.append({**claim.as_dict(), "record": match.id, "how": how})
    return missing, found


# --- the notes ---------------------------------------------------------------------


def claim_note(document: dict) -> str:
    return note(CLAIM_TAG, json.dumps(document, ensure_ascii=False, separators=(",", ":")))


def parse_claim(content) -> dict | None:
    value = parse_note(content, CLAIM_TAG)
    if value is None:
        return None
    try:
        document = json.loads(value)
    except ValueError:
        return None
    return document if isinstance(document, dict) and isinstance(document.get("missing"), list) else None


SETTLED_HOW = ("recorded", "corrected", "dismissed")
_SETTLED = re.compile(r"^#(?P<claim>\d+) (?P<how>[a-z]+)(?: #(?P<by>\d+))?(?: — (?P<why>.*))?$", re.S)


def settled_note(claim_id: int, how: str, by: int = 0, why: str = "") -> str:
    if how not in SETTLED_HOW:
        raise ValueError(f"a claim is settled as one of {', '.join(SETTLED_HOW)}")
    reason = f" — {' '.join(str(why).split())}" if str(why).strip() else ""
    return note(SETTLED_TAG, f"#{int(claim_id)} {how}" + (f" #{int(by)}" if by else "") + reason)


def parse_settled(content) -> dict | None:
    value = parse_note(content, SETTLED_TAG)
    match = _SETTLED.match(value.strip()) if value is not None else None
    if match is None or match.group("how") not in SETTLED_HOW:
        return None
    return {"claim": int(match.group("claim")), "how": match.group("how"), "by": int(match.group("by") or 0),
            "why": (match.group("why") or "").strip()}


def claims_of(messages: Iterable, self_id: int | None = None) -> list[dict]:
    """Every claim in a conversation, oldest first, with its state:
    `repairing` (attempt 1, open), `escalated` (attempt 2, open),
    `recorded`/`corrected`/`dismissed` (settled), `superseded` (an attempt 1
    whose repeat is attempt 2). `self_id` keeps only that bot's claims."""
    rows: dict[int, dict] = {}
    settled: dict[int, dict] = {}
    for message in messages:
        if hasattr(message, "as_zulip"):
            message = message.as_zulip()
        content = message.get("content")
        if self_id is not None and message.get("sender_id") != self_id:
            if parse_settled(content) is None:
                continue
        document = parse_claim(content)
        if document is not None:
            attempt = int(document.get("attempt") or 1)
            rows[int(message.get("id") or 0)] = {
                **document, "id": int(message.get("id") or 0), "at": int(message.get("timestamp") or 0),
                "by": int(message.get("sender_id") or 0), "attempt": attempt,
                "state": "repairing" if attempt < 2 else "escalated",
            }
            continue
        done = parse_settled(content)
        if done is not None:
            settled[done["claim"]] = {**done, "id": int(message.get("id") or 0)}
    for row in rows.values():
        if row["id"] in settled:
            row["state"] = settled[row["id"]]["how"]
            row["settled_by"] = settled[row["id"]]["id"]
        of = int(row.get("of") or 0)
        if of in rows and rows[of]["state"] == "repairing":
            rows[of]["state"] = "superseded"
    return [rows[i] for i in sorted(rows)]


def open_claims(messages: Iterable, self_id: int | None = None) -> list[dict]:
    return [row for row in claims_of(messages, self_id) if row["state"] in ("repairing", "escalated")]


def notice(claims: list[dict]) -> str:
    """What a serving is told about the open claims in its conversation.
    Empty when there are none."""
    if not claims:
        return ""
    lines = ["# A reply of yours said something was done that is not on record", ""]
    for row in claims:
        lines.append(f"Your reply #{row.get('reply')} in this conversation said:")
        for item in row.get("missing") or []:
            claim = Claim(item.get("act", "send"), int(item.get("target") or 0), str(item.get("where") or ""))
            quote = str(item.get("quote") or "").strip()
            lines.append(f"- \"{quote}\" — {claim.label()}: no such record exists (`{TOOL[claim.act]}` makes it)."
                         if quote else f"- {claim.label()}: no such record exists (`{TOOL[claim.act]}` makes it).")
        found = row.get("found") or []
        if found:
            lines.append("What that serving did record: " + ", ".join(
                f"{f.get('act')} #{f.get('record')}" for f in found) + ".")
        if int(row.get("attempt") or 1) >= 2:
            lines.append("This was pointed out once already and the reply after it said it again; the owners "
                         "have been told.")
    lines += [
        "",
        "The listener checked the records the serving wrote and found none of the above. This serving was "
        "started for it. Either do it now with the tool that makes the record (its `--help` says how; it "
        "refuses a record without the person's own words, which is right), or, if it should not or cannot be "
        "done, say plainly in your reply that the earlier reply was wrong and what is actually on record. "
        "Either answer settles it. Say only what this serving really did: this reply is checked the same way.",
    ]
    return "\n".join(lines)


# --- the checker --------------------------------------------------------------------


def config_path(environ=None) -> Path:
    environ = os.environ if environ is None else environ
    named = str(environ.get(CONFIG_VARIABLE, "")).strip()
    if named:
        return Path(named).expanduser()
    base = str(environ.get("XDG_CONFIG_HOME", "")).strip()
    return (Path(base) if base else Path.home() / ".config") / "agag" / "claims.toml"


@dataclass
class ClaimCheck:
    """What a listener needs to check its replies: the reader (None when the
    host names none, with why), how often an unchecked serving is tried, and
    how long to wait for the mirror to hold the reply."""

    reader: Reader | None
    problem: str = ""
    attempts: int = 3
    retry_seconds: float = 60.0
    mirror_wait: float = 15.0
    #: Sleep between looks at the mirror (a test replaces it).
    sleep: object = field(default=time.sleep)

    @classmethod
    def from_host(cls, environ=None) -> "ClaimCheck":
        path = config_path(environ)
        try:
            import tomllib

            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(None, f"no reader configured ({path} does not exist)")
        except (OSError, ValueError) as error:
            return cls(None, f"{path} could not be read: {error}")
        reader = document.get("reader") or {}
        url, model = str(reader.get("url") or "").strip(), str(reader.get("model") or "").strip()
        if not url or not model:
            return cls(None, f"{path} names no [reader] url and model")
        check = document.get("check") or {}
        return cls(OllamaReader(url, model, float(reader.get("timeout") or 60)),
                   attempts=int(check.get("attempts") or 3),
                   retry_seconds=float(check.get("retry_seconds") or 60),
                   mirror_wait=float(check.get("mirror_wait") or 15))


def notice_for_current() -> str:
    """The notice for the serving bound to this thread: the open claims in
    its home, read off the running listener's mirror. Empty outside a
    listener or when there are none; never fails a serving."""
    from . import serving as serving_record
    from .listen import current_mirror

    try:
        journal = serving_record.current()
        record = journal.serving() if journal is not None and hasattr(journal, "serving") else None
        mirror = current_mirror()
        if record is None or mirror is None or not record.home_channel:
            return ""
        self_id = _listener_self_id()
        found = open_claims(mirror.messages(record.home_channel, record.home_topic, across_resolve=True), self_id)
        return notice(found)
    except Exception:  # noqa: BLE001 - a notice never fails a serving
        return ""


def _listener_self_id() -> int | None:
    from .listen import current_listener

    listener = current_listener()
    return getattr(listener, "self_id", None) if listener is not None else None


# --- the operator's door -------------------------------------------------------------


def main(argv=None) -> int:
    """`python -m agag.claims <reply id>` reads one delivered reply the way
    the listener does and prints the judgment (no write); `--settle <claim>
    dismissed "why"` closes a claim a person decided to leave, under the
    credential in `AGENTCHAT_ZULIP_ENV`."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m agag.claims", description=main.__doc__)
    parser.add_argument("reply", nargs="?", type=int, help="a reply's message id to check (dry run)")
    parser.add_argument("--mirror", help="a mirror store to read (a copy; it is opened read-write)")
    parser.add_argument("--after", type=int, default=0, help="the window's start (the ack's id)")
    parser.add_argument("--settle", type=int, help="a claim note's id to close as dismissed")
    parser.add_argument("why", nargs="*", help="why (with --settle)")
    args = parser.parse_args(argv)
    if args.settle:
        from .zulip import ZulipClient, channel_name

        client = ZulipClient.from_env_file(os.environ.get("AGENTCHAT_ZULIP_ENV", ""))
        found = client.message(int(args.settle))
        if not found or parse_claim(found.get("content")) is None:
            print(f"#{args.settle} is not a claim note", file=sys.stderr)
            return 1
        written = client.send_to_channel(channel_name(found), str(found.get("subject") or ""),
                                         settled_note(int(args.settle), "dismissed", 0, " ".join(args.why)))
        print(f"settled #{args.settle} as dismissed: #{written}")
        return 0
    if not args.reply or not args.mirror:
        parser.error("a reply id and --mirror are needed for a check")
    from .mirror.store import Store

    check = ClaimCheck.from_host()
    if check.reader is None:
        print(f"unchecked: {check.problem}", file=sys.stderr)
        return 2

    class _M:
        def __init__(self, store):
            self.store = store

        def message(self, i):
            return self.store.message(int(i))

        def notes(self, **kw):
            return self.store.notes(**kw)

    mirror = _M(Store(Path(args.mirror)))
    reply = mirror.message(args.reply)
    if reply is None:
        print(f"#{args.reply} is not in {args.mirror}", file=sys.stderr)
        return 1
    home = (reply.channel, reply.topic)
    claims = check.reader.read(reply_words(reply.content))
    window = window_records(mirror, reply.sender_id, args.after, reply.id, home=home) if args.after else []
    missing, found = judge(claims, window, mirror=mirror, self_id=reply.sender_id, home=home, before=reply.id)
    print(json.dumps({"reply": reply.id, "claims": [c.as_dict() for c in claims], "window": [r.as_dict() for r in window],
                      "missing": missing, "found": found}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
