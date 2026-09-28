"""What a conversation is to the conversation a root note names (failsafe p6 ex1).

A root note (`[selfnote][rootchat] <home>`) has always said two things at
once: **where answers go** — a post naming the agent in that conversation is
served in `home` — and **whose work the conversation is** — the trace hangs it
under `home`, and `home`'s request owns its work, its waits and its
acceptance. Most of the time both are true. They are not when an agent posts
into a conversation that is somebody else's request while serving its own:
Front cleaning up o11711 posted into m8519's request (#15357), and the trace,
the panel and the acceptance holders all read m8519 as o11711's work. p6
guessed the second meaning at read time from the conversation's first post,
which a long conversation or a bounded read does not show — so at 200 posts a
citation became an adoption, and a truncated read could drop a real task.

Now the second meaning is recorded, not guessed:

- **the note says it**: `[selfnote][rootchat] <home> rel=work` (a delegation:
  the conversation is work done for `home`) or `rel=reference` (a citation or
  a comment: answers still come back to `home`, and nothing else changes
  hands). A deliberate move (`[selfnote][rootchat-moved]`, `agentchat anchor`,
  `agrun adopt`) is always work.
- **a correction**, by the note's author: `[selfnote][relation] #<note>
  <work|reference> — <why>` (`agentchat relation`). The newest one wins.
- **the legacy record**, once per author, for notes written before notes said
  it: `[selfnote][relation] legacy upto=#<id> reference=#<a>,#<b>
  unknown=#<c> — <why>` — every note of that author up to `upto` without a
  word is work unless listed, classified from the conversation's real
  beginning (`python -m agag.relations legacy`).
- otherwise the relation is **unknown**: it adopts nothing, and readers list
  it with the command that resolves it. Missing evidence never makes an
  ownership edge, and never hides a conversation's own work — that stays
  traced from its own request.

The return address is never affected: callbacks follow the note's home
whatever its relation (`agag.zulip.rootchat_home`).

The default a writer picks (`agentchat send`) is decided from the
conversation's **beginning**, read oldest-first (`beginning_relation`): work
for a new conversation, one the writer began, or one opened for work (an
identity note, or a note first); reference for one that began as somebody
else's request.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .selfnote import (
    RELATIONS,
    is_speech,
    note,
    parse_note,
    parse_rootchat,
    parse_rootchat_moved,
    rootchat_relation,
)

__all__ = [
    "RELATION_TAG",
    "UNKNOWN",
    "Relation",
    "Relations",
    "beginning_relation",
    "correction_note",
    "legacy_note",
    "load",
    "parse_correction",
    "parse_legacy",
]

RELATION_TAG = "relation"
UNKNOWN = "unknown"
#: Notes that say "this conversation is a unit of work" (`agag.trace.IDENTITY_TAGS`).
IDENTITY_TAGS = ("mission", "task", "asset", "assetrun", "change")
_CORRECTION = re.compile(r"^#(?P<note>\d+) (?P<kind>[a-z]+)(?: — (?P<why>.*))?$", re.S)
_LEGACY = re.compile(r"^legacy upto=#(?P<upto>\d+)(?P<rest>(?: [a-z]+=[#\d,]*)*)(?: — (?P<why>.*))?$", re.S)
_LIST = re.compile(r" (?P<kind>[a-z]+)=(?P<ids>[#\d,]*)")


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def correction_note(note_id: int, kind: str, why: str = "") -> str:
    if kind not in RELATIONS:
        raise ValueError(f"a relation is one of {', '.join(RELATIONS)}, not {kind!r}")
    reason = f" — {_one_line(why)}" if str(why).strip() else ""
    return note(RELATION_TAG, f"#{int(note_id)} {kind}{reason}")


def legacy_note(upto: int, references=(), unknown=(), why: str = "") -> str:
    ids = lambda rows: ",".join(f"#{int(i)}" for i in sorted(set(rows)))
    reason = f" — {_one_line(why)}" if str(why).strip() else ""
    return note(RELATION_TAG, f"legacy upto=#{int(upto)} reference={ids(references)} unknown={ids(unknown)}{reason}")


def parse_correction(content) -> dict | None:
    value = parse_note(content, RELATION_TAG)
    match = _CORRECTION.match(value.strip()) if value is not None else None
    if match is None or match.group("kind") not in RELATIONS:
        return None
    return {"note": int(match.group("note")), "kind": match.group("kind"), "why": (match.group("why") or "").strip()}


def parse_legacy(content) -> dict | None:
    value = parse_note(content, RELATION_TAG)
    match = _LEGACY.match(value.strip()) if value is not None else None
    if match is None:
        return None
    lists: dict[str, set[int]] = {"reference": set(), "unknown": set()}
    for row in _LIST.finditer(match.group("rest") or ""):
        if row.group("kind") in lists:
            lists[row.group("kind")] = {int(i) for i in re.findall(r"\d+", row.group("ids"))}
    return {"upto": int(match.group("upto")), "reference": lists["reference"], "unknown": lists["unknown"],
            "why": (match.group("why") or "").strip()}


@dataclass
class Relation:
    """What one root note's conversation is to the note's home."""

    note: int
    author: int
    kind: str
    #: `note` (the note says it), `move`, `record` (a correction), `legacy`
    #: (the legacy record) or `` (nothing decides it: unknown).
    decided_by: str = ""
    record: int = 0
    why: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def adopts(self) -> bool:
        return self.kind == "work"

    def describe(self) -> str:
        where = {"note": "stated on the note", "move": "a deliberate move",
                 "record": f"corrected by #{self.record}", "legacy": f"classified by the legacy record #{self.record}",
                 "": "nothing records it"}[self.decided_by]
        return f"{self.kind} ({where}{': ' + self.why if self.why else ''})"


class Relations:
    """The realm's relation records, read once, answering for any root note."""

    def __init__(self, records=()):
        #: note id → [(record id, author, kind, why)], oldest first.
        self.corrections: dict[int, list[tuple[int, int, str, str]]] = {}
        #: author → [(record id, parsed)], oldest first.
        self.legacy: dict[int, list[tuple[int, dict]]] = {}
        for message in sorted(records or (), key=lambda m: int(m.get("id") or 0)):
            sender = int(message.get("sender_id") or 0)
            mid = int(message.get("id") or 0)
            fixed = parse_correction(message.get("content"))
            if fixed is not None:
                self.corrections.setdefault(fixed["note"], []).append((mid, sender, fixed["kind"], fixed["why"]))
                continue
            legacy = parse_legacy(message.get("content"))
            if legacy is not None:
                self.legacy.setdefault(sender, []).append((mid, legacy))

    def of(self, message: dict) -> Relation:
        """The relation of the root note (or move) `message`."""
        mid = int(message.get("id") or 0)
        author = int(message.get("sender_id") or 0)
        content = message.get("content")
        if parse_rootchat_moved(content) is not None:
            return Relation(mid, author, "work", "move")
        own = [row for row in self.corrections.get(mid, []) if row[1] == author]
        if own:
            record, _, kind, why = own[-1]
            return Relation(mid, author, kind, "record", record, why)
        stated = rootchat_relation(content)
        if stated is not None:
            return Relation(mid, author, stated, "note")
        for record, legacy in reversed(self.legacy.get(author, [])):
            if mid <= legacy["upto"]:
                if mid in legacy["unknown"]:
                    return Relation(mid, author, UNKNOWN, "", record, "the legacy record could not read its beginning")
                kind = "reference" if mid in legacy["reference"] else "work"
                return Relation(mid, author, kind, "legacy", record)
        return Relation(mid, author, UNKNOWN, "")


def load(client, num_before: int = 1000) -> Relations:
    """The relation records, from the realm-wide note search (a mirror
    answers it for free). A search that got no answer is an empty book: every
    note without its own word then reads unknown, never work."""
    try:
        return Relations(client.public_notes(RELATION_TAG, num_before))
    except Exception:  # noqa: BLE001 - an unread book decides nothing
        return Relations()


def beginning_relation(beginning: list[dict] | None, author: int, messages: list[dict] | None = None) -> str | None:
    """The relation a writer's note should state, from the conversation's
    real beginning (its oldest posts, oldest first): `work` when there is
    none yet, when `author` began it, when it was opened by a note (a root
    note, an identity note), or when it carries an identity note anywhere
    in `messages`; `reference` when it began as somebody else's speech;
    None when the beginning could not be read."""
    if beginning is None:
        return None
    rows = sorted(beginning, key=lambda m: int(m.get("id") or 0))
    if not rows:
        return "work"
    first = rows[0]
    if int(first.get("sender_id") or 0) == int(author) or not is_speech(first):
        return "work"
    if any(parse_note(m.get("content"), tag) is not None for m in [*rows, *(messages or ())] for tag in IDENTITY_TAGS):
        return "work"
    return "reference"


# --- the legacy classification (`python -m agag.relations legacy`) ------------------------


def classify_legacy(mirror_path: str, author: int, client=None) -> dict:
    """Every root note of `author` in the mirror without a relation word,
    classified from its conversation's beginning: `{upto, work, reference,
    unknown, rows}`. A conversation the mirror does not hold from its
    beginning is read oldest-first from Zulip (`client.topic_beginning`),
    else it is unknown."""
    import shutil
    import sqlite3
    import tempfile
    from pathlib import Path

    work = Path(tempfile.mkdtemp()) / "mirror.sqlite"
    shutil.copy(mirror_path, work)
    db = sqlite3.connect(work)
    db.row_factory = sqlite3.Row
    streams = {r["stream_id"]: r["name"] for r in db.execute("SELECT stream_id, name FROM channels")}
    notes = db.execute(
        "SELECT m.* FROM notes n JOIN messages m ON m.id = n.message_id WHERE n.tag = 'rootchat' AND m.deleted = 0 "
        "AND m.sender_id = ? ORDER BY m.id", (int(author),)).fetchall()
    found = {"upto": 0, "work": [], "reference": [], "unknown": [], "rows": []}
    for row in notes:
        if rootchat_relation(row["content"]) is not None:
            continue
        found["upto"] = max(found["upto"], int(row["id"]))
        channel = streams.get(row["stream_id"], "")
        coverage = db.execute("SELECT complete FROM coverage WHERE stream_id = ? AND topic = ?",
                              (row["stream_id"], row["topic"])).fetchone()
        messages = [dict(m) for m in db.execute(
            "SELECT * FROM messages WHERE stream_id = ? AND topic = ? AND deleted = 0 ORDER BY id",
            (row["stream_id"], row["topic"]))]
        for m in messages:
            m["sender_realm_str"] = m.get("sender_realm") or ""
        beginning: list[dict] | None = messages[:5] if coverage is not None and coverage["complete"] else None
        if beginning is None and client is not None and hasattr(client, "topic_beginning"):
            try:
                beginning = list(client.topic_beginning(channel, row["topic"], 5))
            except Exception:  # noqa: BLE001 - unreadable is unknown
                beginning = None
        kind = beginning_relation(beginning, int(author), messages) or UNKNOWN
        found[kind].append(int(row["id"]))
        first = sorted(beginning or [{}], key=lambda m: int(m.get("id") or 0))[0]
        found["rows"].append({"note": int(row["id"]), "kind": kind, "channel": channel, "topic": row["topic"],
                              "home": str(parse_rootchat(row["content"]) or ""),
                              "first": int(first.get("id") or 0),
                              "first_by": first.get("sender_name") or first.get("sender_full_name") or ""})
    return found


def main(argv=None) -> int:
    """`python -m agag.relations legacy --mirror <sqlite> [--apply] [--why …]`,
    run with the author's own credential (`AGENTCHAT_ZULIP_ENV`): classify
    this author's legacy root notes and, with `--apply`, write its legacy
    record once (a repeat that would say the same writes nothing)."""
    import argparse
    import os
    import sys

    from .zulip import ZulipClient

    parser = argparse.ArgumentParser(prog="python -m agag.relations")
    parser.add_argument("command", choices=("legacy",))
    parser.add_argument("--mirror", required=True)
    parser.add_argument("--channel", required=True, help="where the record is written (the author's own channel)")
    parser.add_argument("--topic", default="relations")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--why", default="classified from each conversation's beginning (failsafe p6 ex1)")
    args = parser.parse_args(argv)
    client = ZulipClient.from_env(os.environ.get("AGENTCHAT_ZULIP_ENV") or ".local/zulip.env")
    me = client.whoami()
    author = int(me["user_id"])
    found = classify_legacy(args.mirror, author, client)
    for row in found["rows"]:
        if row["kind"] != "work":
            print(f"{row['kind']:9} #{row['note']} in {row['channel']}/{row['topic']} -> {row['home']} "
                  f"(began #{row['first']} by {row['first_by']})")
    print(f"{me.get('full_name')}: {len(found['work'])} work, {len(found['reference'])} reference, "
          f"{len(found['unknown'])} unknown, up to #{found['upto']}")
    if not found["upto"]:
        print("no legacy notes: nothing to record")
        return 0
    book = load(client)
    same = [r for r, legacy in book.legacy.get(author, []) if legacy["upto"] == found["upto"]
            and legacy["reference"] == set(found["reference"]) and legacy["unknown"] == set(found["unknown"])]
    if same:
        print(f"already recorded: #{same[-1]}; nothing written")
        return 0
    if not args.apply:
        print("dry run: --apply writes the record")
        return 0
    written = client.send_to_channel(args.channel, args.topic,
                                     legacy_note(found["upto"], found["reference"], found["unknown"], args.why))
    print(f"recorded #{written} in {args.channel}/{args.topic}", file=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
