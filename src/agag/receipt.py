"""An answer's receipt: inspect it, and repair it from evidence (failsafe p6).

An answer an agent posts naming its requester is owed until the
requester's listener writes a receipt — the `[served] <remote> <id>` mark,
after a serving whose input held the answer delivered its reply
(`agag.listen`). When that did not happen (p1's ✔ race left seven, m8519's
#8557 among them), nothing short of a hand-written note could say so, and
Front's hand-written one (#15362) was not a note any reader parses.

`agentchat receipt <answer id>` answers, for the agent running it:

- **the answer**: where it is now, who wrote it, whom it names;
- **the receiving agent**: this one, and whether the answer names it;
- **the destination**: this agent's home for that conversation (its root
  note there, else the replaced or parent conversation's — the callback's
  own lookup, `rootchat_home`), live under its current name;
- **the receipt**: `received` (a served mark covers it), `reconciled` (a
  `[selfnote][receipt]` names it) or `missing`;
- **the evidence** a repair would rest on, strongest first:
  1. `journal` — this agent's listener journal (`AGENTCHAT_JOURNAL`, else
     `$AGREFS_HOME/mirror/listener.sqlite`) shows a delivered serving that
     was given the answer: triggered by it, or handed a thread whose span
     holds it;
  2. `decision` — a decision recorded after it covers it
     (`agag.trace.decisions`: the requester's acceptance, the mission's
     acceptance up to its shown result, a cancellation above it);
  3. `--because <post>` — this agent's own later post that took the answer
     up (a relay), named explicitly by whoever repairs.

`--repair` then writes **one note** into home:

- with journal evidence, the served mark a listener would have written —
  `[served] <remote> <answer>` — but only when every earlier post naming
  this agent above the existing mark was given to a delivered serving too
  (a mark covers everything up to its id, and must never cover an answer
  nobody was given);
- otherwise, with a decision or `--because`, a reconciled receipt:
  `[selfnote][receipt] #<answer> by #<evidence> (<why>) in <remote>`,
  covering exactly that answer and claiming no serving;
- with none, nothing: it says what would count.

A note is a selfnote, so it buys nobody a run. Repeating the repair finds
the receipt and writes nothing; an interrupted one wrote one note or none.
An answer that arrived after the one repaired is untouched: served marks
are bounded by the journal's spans, reconciled receipts by their one id.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .selfnote import (
    Conversation,
    is_speech,
    parse_receipt,
    parse_rootchat,
    parse_served,
    receipt_note,
    served_note,
)
from .trace import MENTION, decisions
from .zulip import (
    RESOLVED_TOPIC_PREFIX,
    ZulipClient,
    channel_name,
    locate,
    rootchat_home,
    topic_history_across_resolve,
)

__all__ = ["JOURNAL_VARIABLE", "Receipt", "inspect", "repair", "receipt_lines"]

#: The listener journal of the agent running the command (set for every run
#: by `agag.agent.chat_environment`).
JOURNAL_VARIABLE = "AGENTCHAT_JOURNAL"
HISTORY = 400
#: How far up the owners' root notes decisions are looked for: a task, its
#: mission, the run above it.
UP = 3


@dataclass
class Receipt:
    answer: int
    channel: str = ""
    topic: str = ""
    author: str = ""
    at: int = 0
    to: str = ""
    to_id: int = 0
    named: bool = False
    home: str = ""
    home_live: str = ""
    state: str = "unknown"
    mark: int = 0
    reconciled: dict | None = None
    journal: list[dict] = field(default_factory=list)
    decision: dict | None = None
    because: dict | None = None
    unmarked_before: list[int] = field(default_factory=list)
    later: list[int] = field(default_factory=list)
    action: str = "none"
    why: str = ""
    written: int = 0
    note: str = ""

    def as_dict(self) -> dict:
        return {"schema": "agag.receipt.v1", **asdict(self)}


def _bare(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def _journal_path(journal: str | Path | None) -> Path | None:
    if journal:
        return Path(journal)
    if os.environ.get(JOURNAL_VARIABLE):
        return Path(os.environ[JOURNAL_VARIABLE])
    if os.environ.get("AGREFS_HOME"):
        return Path(os.environ["AGREFS_HOME"]) / "mirror" / "listener.sqlite"
    return None


def journal_evidence(path: Path | None, channel: str, topic: str, ids: list[int]) -> dict[int, dict]:
    """`{answer id: the delivered serving that was given it}` for the ids
    asked about, from a listener journal read-only. An id a serving was
    triggered by, or that lies in a span of this conversation a serving was
    handed, counts; nothing else does."""
    if path is None or not path.is_file() or not ids:
        return {}
    found: dict[int, dict] = {}
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT id, route, channel, topic, trigger_id, delivered_id, extra FROM servings"
                          " WHERE state = 'delivered' AND delivered_id IS NOT NULL").fetchall()
        db.close()
    except sqlite3.Error:
        return {}
    bare = _bare(topic)
    for row in rows:
        try:
            extra = json.loads(row["extra"] or "{}")
        except ValueError:
            extra = {}
        spans = [s for s in extra.get("inputs") or []
                 if s.get("channel") == channel and _bare(str(s.get("topic") or "")) == bare]
        for answer in ids:
            if answer in found:
                continue
            given = (row["channel"] == channel and _bare(row["topic"]) == bare and int(row["trigger_id"] or 0) == answer)
            given = given or any(int(s.get("first") or 0) <= answer <= int(s.get("last") or 0) for s in spans)
            if given:
                found[answer] = {"serving": int(row["id"]), "route": row["route"],
                                 "delivered": int(row["delivered_id"])}
    return found


def _names(content: str) -> set[str]:
    return {m.group("name").strip() for m in MENTION.finditer(str(content or ""))}


def _decision_for(client, history: list[dict], answer: int) -> dict | None:
    """A decision covering the answer: in its own conversation, then in the
    conversations its owner opened it for, up to `UP` hops."""
    owner = int(next((m.get("sender_id") for m in history if m.get("id") == answer), 0) or 0) or None
    own, _ = decisions(history, owner)
    covering = [d for d in own if int(d["covers"]) >= answer]
    seen = set()
    current = history
    for _ in range(UP):
        parent = next((parse_rootchat(m.get("content")) for m in current
                       if owner is not None and int(m.get("sender_id") or 0) == owner
                       and parse_rootchat(m.get("content")) is not None), None)
        if parent is None:
            break
        where = locate(client, parent) or parent
        key = (where.channel, _bare(where.topic))
        if key in seen:
            break
        seen.add(key)
        current = topic_history_across_resolve(client, where.channel, where.topic, HISTORY)
        parent_owner = next((int(m.get("sender_id") or 0) for m in current
                             if any(str(m.get("content") or "").startswith(f"[selfnote][{tag}]")
                                    for tag in ("mission", "asset", "change"))), None)
        _, down = decisions(current, parent_owner)
        covering += [d for d in down if int(d["covers"]) >= answer]
        owner = parent_owner or owner
    return min(covering, key=lambda d: d["id"]) if covering else None


def inspect(client: ZulipClient, answer_id: int, *, journal: str | Path | None = None,
            because: int = 0) -> Receipt:
    """Everything a repair would rest on; writes nothing."""
    me = client.whoami()
    self_id, self_name = int(me["user_id"]), str(me.get("full_name") or "")
    result = Receipt(int(answer_id), to=self_name, to_id=self_id)
    message = client.message(int(answer_id))
    if not message:
        result.state, result.why = "unknown", f"message {answer_id} does not exist or could not be read"
        return result
    result.channel, result.topic = channel_name(message), str(message.get("subject") or "")
    result.author, result.at = str(message.get("sender_full_name") or ""), int(message.get("timestamp") or 0)
    result.named = self_name in _names(message.get("content"))
    history = topic_history_across_resolve(client, result.channel, result.topic, HISTORY)
    remote = Conversation(result.channel, _bare(result.topic))
    if not result.named or int(message.get("sender_id") or 0) == self_id:
        result.state = "not_owed"
        result.why = (f"#{answer_id} is {self_name}'s own post" if int(message.get("sender_id") or 0) == self_id
                      else f"#{answer_id} does not name {self_name}: no receipt of {self_name}'s is owed for it")
        return result
    home = rootchat_home(client, result.channel, result.topic, self_id)
    if home is None:
        result.state = "not_owed"
        result.why = (f"{self_name} has no root note in {remote} or in the conversation it was opened for: "
                      "the answer is not a callback of any conversation of yours")
        return result
    located = locate(client, home)
    result.home = f"{home.channel}/{_bare(home.topic)}"
    result.home_live = located.topic if located is not None else home.topic
    home_history = topic_history_across_resolve(client, home.channel, result.home_live, HISTORY)
    here = {int(m.get("id") or 0) for m in history}
    for note in home_history:
        if int(note.get("sender_id") or 0) != self_id:
            continue
        served = parse_served(note.get("content"))
        if served is not None and (served[1] in here or (served[0].channel, _bare(served[0].topic)) ==
                                   (remote.channel, remote.topic)):
            result.mark = max(result.mark, int(served[1]))
        receipt = parse_receipt(note.get("content"))
        if receipt is not None and receipt[1] == int(answer_id):
            result.reconciled = {"id": int(note.get("id") or 0), "evidence": receipt[2], "why": receipt[3]}
    naming = [int(m.get("id") or 0) for m in history
              if int(m.get("sender_id") or 0) != self_id and is_speech(m) and self_name in _names(m.get("content"))]
    result.later = [i for i in naming if i > int(answer_id) and i > result.mark]
    result.unmarked_before = [i for i in naming if result.mark < i < int(answer_id)]
    if result.mark >= int(answer_id):
        result.state, result.why = "received", f"a served mark in {result.home} covers it (up to #{result.mark})"
        return result
    if result.reconciled is not None:
        result.state = "reconciled"
        result.why = (f"reconciled at #{result.reconciled['id']} on #{result.reconciled['evidence']}"
                      + (f" ({result.reconciled['why']})" if result.reconciled["why"] else ""))
        return result
    result.state = "missing"
    given = journal_evidence(_journal_path(journal), result.channel, result.topic,
                             [*result.unmarked_before, int(answer_id)])
    if int(answer_id) in given:
        result.journal = [{"answer": int(answer_id), **given[int(answer_id)]}]
    result.decision = _decision_for(client, history, int(answer_id))
    if because:
        post = client.message(int(because))
        if post and int(post.get("sender_id") or 0) == self_id and int(because) > int(answer_id) \
                and is_speech(post):
            result.because = {"id": int(because), "where": f"{channel_name(post)}/{_bare(str(post.get('subject') or ''))}"}
        else:
            result.because = None
            result.why = (f"#{because} is not a later post of {self_name}'s: --because names your own post that "
                          f"took #{answer_id} up")
    ungiven = [i for i in result.unmarked_before if i not in given]
    if result.journal and not ungiven:
        result.action = "served"
        result.why = (f"serving {result.journal[0]['serving']} ({result.journal[0]['route']} route, reply "
                      f"#{result.journal[0]['delivered']}) was given it; the listener's mark was not written")
    elif result.decision is not None:
        result.action = "reconciled"
        result.why = f"no serving shows it was given; settled by {result.decision['what']}"
    elif result.because is not None:
        result.action = "reconciled"
        result.why = f"no serving shows it was given; taken up by your post #{result.because['id']}"
    elif result.journal:
        result.action = "reconciled"
        result.why = (f"serving {result.journal[0]['serving']} was given it, but earlier post(s) "
                      f"{', '.join('#' + str(i) for i in ungiven)} naming you were not; a mark up to it would "
                      "cover them, so the receipt names only this answer")
    elif not result.why:
        result.action = "refuse"
        result.why = ("no evidence the answer was dealt with: no serving in your journal was given it, no "
                      "decision recorded after it covers it, and no --because post. Serve it (read it and "
                      "answer at home; your listener writes the receipt) or name the post that took it up")
    else:
        result.action = "refuse"
    return result


def repair(client: ZulipClient, found: Receipt) -> Receipt:
    """Write the one note `inspect` said would settle it (module doc)."""
    if found.state != "missing" or found.action not in ("served", "reconciled"):
        return found
    remote = Conversation(found.channel, _bare(found.topic))
    channel = found.home.split("/", 1)[0]
    if found.action == "served":
        text = served_note(remote, found.answer)
    else:
        evidence, why = ((found.decision["id"], found.decision["kind"]) if found.decision is not None
                         else (found.journal[0]["delivered"], "given") if found.journal and found.because is None
                         else (found.because["id"], "relayed"))
        text = receipt_note(remote, found.answer, evidence, why)
    sent = client.send_to_channel(channel, found.home_live, text)
    found.written = int(sent or 0) if isinstance(sent, int) else 0
    found.note = text
    found.state = "received" if found.action == "served" else "reconciled"
    return found


def receipt_lines(found: Receipt, *, repaired: bool = False) -> list[str]:
    where = f"{found.channel}/{found.topic}" if found.channel else "?"
    lines = [f"receipt of #{found.answer} ({where}, by {found.author or '?'}) for {found.to}: {found.state.upper()}"]
    if found.home:
        lines.append(f"  home: {found.home}" + (f" (now '{found.home_live}')" if found.home_live and
                                                 _bare(found.home_live) != found.home.split('/', 1)[1] else "")
                     + f"; served mark up to #{found.mark or '-'}")
    if found.journal:
        j = found.journal[0]
        lines.append(f"  journal: serving {j['serving']} ({j['route']}) was given it; its reply is #{j['delivered']}")
    if found.decision:
        lines.append(f"  decision: {found.decision['what']} (covers up to #{found.decision['covers']})")
    if found.because:
        lines.append(f"  because: your post #{found.because['id']} in {found.because['where']}")
    if found.unmarked_before:
        lines.append(f"  earlier posts naming you above the mark: {', '.join('#' + str(i) for i in found.unmarked_before)}")
    if found.later:
        lines.append(f"  later posts naming you (untouched): {', '.join('#' + str(i) for i in found.later)}")
    lines.append(f"  {found.why}")
    if repaired and found.note:
        lines.append(f"  wrote #{found.written or '?'} in {found.home}: {found.note}")
    elif found.state == "missing":
        lines.append({"served": "  --repair writes the served mark the listener would have written",
                      "reconciled": "  --repair writes a reconciled receipt for exactly this answer",
                      "refuse": "  --repair writes nothing"}.get(found.action, ""))
    return [line for line in lines if line]
