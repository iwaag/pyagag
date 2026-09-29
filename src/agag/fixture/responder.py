"""The fixture's scripted responder (`agent_guide` p2 ex1 step 3).

The fixture refuses every write, so no probe could delegate: the callback
path — a serving ends, the answer arrives, the conversation is served again —
was covered only by p1's live mission. A trial that needs it works on an
**overlay**: its own copy of the board (`<out>/overlay/mirror.sqlite`),
marked with the probe's script. On an overlay, and only there:

- `agentchat send` (`MirrorReads.send_to_channel`) records the post in the
  overlay — root note and message, as the realm would hold them — and the
  fixture store the trial started from is never opened for writing;
- each post the served agent sends through one of a scripted agent's
  **doors** (`Doors`: where its live listener serves that post the way the
  script means) takes that agent's next scripted line — a `to=<id>` alone
  does not: it reaches no listener (a first version counted it, and passed
  a run whose question the real agent would never have seen): a canned
  answer, or a question that asks for a decision. A post through a door
  that live starts something else (autolab's new `workplan-` topic is a new
  mission to plan) takes that door's own line and no script line. The line is
  posted by that agent in the same topic **when the serving is over**
  (`deliver`), never during it, because a real answer arrives after the
  asking serving has ended (a first version answered at once, and the run
  read the answer in the same serving);
- every other write is still refused, so the board stays the same between
  runs.

A script line is text with two fields: `{ask}` is the id of the post it
answers, `{asker}` the mention of whoever sent it. The trial kit
(`agag.fixture.run.Trial.converse`) serves the conversation again whenever a
scripted line names the served agent, exactly as the listener's callback
does: home again, with the conversation that called placed beside it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from agag.mirror.store import Store

__all__ = ["AUTOLAB_DOORS", "DOORS", "OVERLAY_META", "Canned", "Doors", "deliver", "door_log", "is_overlay",
           "make_overlay", "post", "posts_since", "record_send", "replay"]

#: The store meta key an overlay carries: its script and how far it got.
OVERLAY_META = "fixture_overlay"


@dataclass(frozen=True)
class Canned:
    """One scripted line: what `agent` (a board name, e.g.
    `autolab-agstudio1`) posts when the served agent next addresses it."""

    agent: str
    text: str


@dataclass(frozen=True)
class Doors:
    """Where an agent's listener serves a post the way a script line means:
    as a question or answer about work the agent already has (`agent_guide`
    p3 ex2). Every other post takes no script line.

    - `channel`: any topic of the agent's own channel (named as the agent);
    - `mentions`: `@**agent**` anywhere. autolab's live listener serves a
      mention elsewhere only as a callback to its own task (`handle_mention`),
      so its scripted lines are not reached that way;
    - `existing`: topics under these prefixes that were **on the board when
      the trial began** (a mission's own plan and task topics);
    - `new`: prefixes where a topic the board did not have is a new request.
      It takes `opened` (a new mission planned) instead of a script line,
      and the topic stays that request's: nothing scripted answers there
      again. `new_in` are the channel prefixes where such a topic is acted
      on at all (autolab: a `workplan-` topic outside a `pj-` channel is one
      it will not act on)."""

    channel: bool = True
    mentions: bool = True
    existing: tuple[str, ...] = ()
    new: tuple[str, ...] = ()
    new_in: tuple[str, ...] = ()
    opened: str = ""


#: autolab's live contract, as its introduction on the board states it
#: (`board.INTROS`): questions about its work in `autolab-agstudio1`; a
#: mission's own `workplan-`/`workrun-` topics for that mission; a new
#: `workplan-` topic in a project channel is a new mission to plan. p3 ex1's
#: responder answered any `workplan-` post, and four delegate-decision
#: passes asked about m20510 in a new one (ctl 2, 5, 12; fix 11).
AUTOLAB_DOORS = Doors(
    mentions=False, existing=("workplan-", "workrun-"), new=("workplan-",), new_in=("pj-",),
    opened=("Message received. Please wait for the reply.\n\n{asker} I read #{ask} as a new request and planned "
            "it as a new mission here: one task, from what this topic asks. Nothing runs until you say start "
            "in this topic.\n\n`ag-post intent=response_request to={asker_id} ask=decision`"))

#: Each scripted agent's doors; an agent not named here is served by a
#: mention or in its own channel.
DOORS: dict[str, Doors] = {"autolab-agstudio1": AUTOLAB_DOORS}


def make_overlay(store: Path, into: Path, script: tuple[Canned, ...], agents: dict[str, int],
                 doors: dict[str, Doors] | None = None) -> Path:
    """A copy of the fixture at `store` as `<into>/mirror.sqlite`, carrying
    the script, the agents it may speak for (name → user id) and their
    doors (`DOORS` by default); the topics on the board now are the ones a
    door's `existing` prefixes serve."""
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    path = into / "mirror.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    source = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    target = sqlite3.connect(str(path))
    with target:
        source.backup(target)
    source.close()
    target.close()
    doors = DOORS if doors is None else doors
    overlay = Store(path)
    named = {c.agent for c in script}
    overlay.set_meta(OVERLAY_META, json.dumps({
        "script": [asdict(c) for c in script], "used": [], "agents": agents,
        "doors": {name: asdict(doors.get(name, Doors())) for name in sorted(named)},
        "topics": sorted({f"{m.channel}\x1f{m.topic}" for m in overlay.iter_all()}),
        "opened": [], "log": []}, ensure_ascii=False))
    overlay.close()
    return path


def is_overlay(store: Store) -> bool:
    return bool(store.get_meta(OVERLAY_META))


def post(path: Path, channel: str, topic: str, sender: int, content: str) -> int:
    """Append one post to the overlay at `path` as `sender`; its id."""
    store = Store(Path(path))
    try:
        with store.transaction():
            return _append(store, channel, topic, sender, content)
    finally:
        store.close()


def record_send(path: Path, channel: str, topic: str, content: str) -> int:
    """`agentchat send` on an overlay: the post, as the store's own reader,
    then the scripted answer it asks for, if any. Returns the post's id."""
    store = Store(Path(path))
    try:
        with store.transaction():
            self_id = int(store.get_meta("self_id") or 0)
            ident = _append(store, channel, topic, self_id, content)
            state = json.loads(store.get_meta(OVERLAY_META) or "{}")
            step = _next_step(state, channel, topic, content)
            if step is not None:
                door, agent, index, text = step
                asker = f"@**{store.get_meta('full_name') or self_id}**"
                if index is not None:
                    state["used"].append(index)
                if door == "new":
                    state.setdefault("opened", []).append(f"{channel}\x1f{topic}")
                state.setdefault("pending", []).append({
                    "channel": channel, "topic": topic, "sender": int(state["agents"][agent]),
                    "content": text.format(ask=ident, asker=asker, asker_id=self_id)})
            if not content.startswith("[selfnote]"):
                state.setdefault("log", []).append({"id": ident, "channel": channel, "topic": topic,
                                                    "door": step[0] if step else "none",
                                                    "agent": step[1] if step else "",
                                                    "line": step[2] if step else None})
            store.set_meta(OVERLAY_META, json.dumps(state, ensure_ascii=False))
            return ident
    finally:
        store.close()


def deliver(path: Path) -> list[int]:
    """Post the scripted answers the last serving's sends asked for. They
    wait until the serving is over, as a real answer does: an agent asked
    during a serving answers after it has ended, and the answer brings the
    asker back as a new serving (the callback)."""
    store = Store(Path(path))
    try:
        with store.transaction():
            state = json.loads(store.get_meta(OVERLAY_META) or "{}")
            posted = [_append(store, line["channel"], line["topic"], line["sender"], line["content"])
                      for line in state.get("pending") or []]
            state["pending"] = []
            store.set_meta(OVERLAY_META, json.dumps(state, ensure_ascii=False))
            return posted
    finally:
        store.close()


def posts_since(path: Path, after_id: int) -> list[dict]:
    """The overlay's posts above `after_id`, oldest first, as Zulip dicts."""
    store = Store.open_readonly(Path(path))
    try:
        return [m.as_zulip() for m in store.iter_all() if m.id > int(after_id)]
    finally:
        store.close()


def door_log(path: Path) -> list[dict]:
    """Every post the served agent sent on the overlay at `path`, with the
    door it went through: `answer` (took script line `line`), `new` (a new
    request: the door's own line), or `none` (nothing scripted)."""
    store = Store.open_readonly(Path(path))
    try:
        return list(json.loads(store.get_meta(OVERLAY_META) or "{}").get("log") or [])
    finally:
        store.close()


def replay(out: Path, script: tuple[Canned, ...]) -> list[dict]:
    """A saved trial's sends (`<out>/overlay` over `<out>/board`) put
    through today's doors: per send, its place, whether an agent answered
    it then (a scripted agent's post in that topic before the served
    agent's next send) and the door it would take now. A door change
    changes what a run sees after it, so this says which runs **would have
    been** treated differently, not what they would then have done."""
    out = Path(out)
    base = Store.open_readonly(out / "board" / "mirror.sqlite")
    try:
        start = base.newest_id()
        topics = sorted({f"{m.channel}\x1f{m.topic}" for m in base.iter_all()})
    finally:
        base.close()
    overlay = Store.open_readonly(out / "overlay" / "mirror.sqlite")
    try:
        saved = json.loads(overlay.get_meta(OVERLAY_META) or "{}")
        self_id = int(overlay.get_meta("self_id") or 0)
        posts = [m for m in overlay.iter_all() if m.id > start]
    finally:
        overlay.close()
    agents = set(saved.get("agents", {}).values())
    state = {"script": [asdict(c) for c in script], "used": [], "opened": [], "topics": topics,
             "doors": {c.agent: asdict(DOORS.get(c.agent, Doors())) for c in script}}
    home = (posts[0].channel, posts[0].topic) if posts else ("", "")
    found = []
    for i, m in enumerate(posts):
        if m.sender_id != self_id or m.content.startswith("[selfnote]") or (m.channel, m.topic) == home:
            continue
        after = []  # up to the served agent's next send: its reply home comes before a delivered answer
        for n in posts[i + 1:]:
            if n.sender_id == self_id and (n.channel, n.topic) != home and not n.content.startswith("[selfnote]"):
                break
            after.append(n)
        answered = any(n.sender_id in agents and n.sender_id != self_id and (n.channel, n.topic) == (m.channel, m.topic)
                       for n in after)
        step = _next_step(state, m.channel, m.topic, m.content)
        if step is not None:
            if step[2] is not None:
                state["used"].append(step[2])
            if step[0] == "new":
                state["opened"].append(f"{m.channel}\x1f{m.topic}")
        now = step[0] if step else "none"
        found.append({"id": m.id, "channel": m.channel, "topic": m.topic, "then": "answer" if answered else "none",
                      "now": now, "changed": answered != (now == "answer")})
    return found


def _next_step(state: dict, channel: str, topic: str, content: str) -> tuple[str, str, int | None, str] | None:
    """Which agent this post reaches, and how: `("answer", agent, index,
    text)` for its next unused script line, `("new", agent, None, text)`
    for a new request, None for nothing scripted."""
    if content.startswith("[selfnote]"):
        return None
    addressed = {name.casefold() for name in re.findall(r"@\*\*([^*|]+)(?:\|\d+)?\*\*", content)}
    known = set(state.get("topics") or ())
    here = f"{channel}\x1f{topic}"
    for name, spec in (state.get("doors") or {}).items():
        doors = Doors(**{k: tuple(v) if isinstance(v, list) else v for k, v in spec.items()})
        # What serves a listener (`agag.listen`): its channel, a mention, a
        # topic it owns. A `to=` in the post's `ag-post` line alone reaches
        # nobody, so it takes no line either.
        if here in (state.get("opened") or ()):
            continue  # a new request's own topic: its planning, not the script
        if (doors.channel and channel == name) or (doors.mentions and name.casefold() in addressed) \
                or (here in known and any(topic.startswith(p) for p in doors.existing)):
            line = next(((i, c) for i, c in enumerate(state.get("script") or [])
                         if c["agent"] == name and i not in state.get("used", [])), None)
            if line is not None:
                return "answer", name, line[0], line[1]["text"]
            continue
        if here not in known and any(topic.startswith(p) for p in doors.new) \
                and any(channel.startswith(p) for p in doors.new_in) and doors.opened:
            return "new", name, None, doors.opened
    return None


def _append(store: Store, channel: str, topic: str, sender: int, content: str) -> int:
    found = store.channel(channel)
    if found is None:
        raise ValueError(f"no channel {channel!r} on this board")
    newest = store.newest_id()
    last = store.message(newest)
    ident = newest + 1
    stamp = max(int(time.time()), (last.timestamp if last is not None else 0) + 60)
    store.put_message({"id": ident, "type": "stream", "stream_id": found.stream_id, "display_recipient": channel,
                       "subject": topic, "sender_id": int(sender), "sender_full_name": _name(store, sender),
                       "sender_realm_str": "", "timestamp": stamp, "content": content.strip()})
    return ident


def _name(store: Store, user: int) -> str:
    users = json.loads(store.get_meta("fixture_users") or "[]")
    return next((u["full_name"] for u in users if int(u["user_id"]) == int(user)), str(user))
