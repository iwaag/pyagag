"""The fixture's scripted responder (`agent_guide` p2 ex1 step 3).

The fixture refuses every write, so no probe could delegate: the callback
path — a serving ends, the answer arrives, the conversation is served again —
was covered only by p1's live mission. A trial that needs it works on an
**overlay**: its own copy of the board (`<out>/overlay/mirror.sqlite`),
marked with the probe's script. On an overlay, and only there:

- `agentchat send` (`MirrorReads.send_to_channel`) records the post in the
  overlay — root note and message, as the realm would hold them — and the
  fixture store the trial started from is never opened for writing;
- each post the served agent sends that addresses a scripted agent the way
  a listener is served (names it with `@**name**`, or is posted in its
  channel or under one of its topic prefixes) takes that agent's next
  scripted line — a `to=<id>` alone does not: it reaches no listener (a
  first version counted it, and passed a run whose question the real agent
  would never have seen): a canned answer, or a question that asks for a decision. The line is
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

__all__ = ["OVERLAY_META", "Canned", "deliver", "is_overlay", "make_overlay", "post", "posts_since", "record_send"]

#: The store meta key an overlay carries: its script and how far it got.
OVERLAY_META = "fixture_overlay"


@dataclass(frozen=True)
class Canned:
    """One scripted line: what `agent` (a board name, e.g.
    `autolab-agstudio1`) posts when the served agent next addresses it."""

    agent: str
    text: str
    #: Topic prefixes that address the agent too (autolab's `workplan-`):
    #: a post there needs no mention to be the agent's work.
    topics: tuple[str, ...] = ()


def make_overlay(store: Path, into: Path, script: tuple[Canned, ...], agents: dict[str, int]) -> Path:
    """A copy of the fixture at `store` as `<into>/mirror.sqlite`, carrying
    the script (and the agents it may speak for: name → user id)."""
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
    overlay = Store(path)
    overlay.set_meta(OVERLAY_META, json.dumps({"script": [asdict(c) for c in script], "used": [],
                                               "agents": agents}, ensure_ascii=False))
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
                index, canned = step
                asker = f"@**{store.get_meta('full_name') or self_id}**"
                state["used"].append(index)
                state.setdefault("pending", []).append({
                    "channel": channel, "topic": topic, "sender": int(state["agents"][canned["agent"]]),
                    "content": canned["text"].format(ask=ident, asker=asker)})
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


def _next_step(state: dict, channel: str, topic: str, content: str) -> tuple[int, dict] | None:
    """The first unused script line whose agent this post addresses."""
    if content.startswith("[selfnote]"):
        return None
    addressed = {name.casefold() for name in re.findall(r"@\*\*([^*|]+)(?:\|\d+)?\*\*", content)}
    for index, canned in enumerate(state.get("script") or []):
        if index in state.get("used", []):
            continue
        # What serves a listener (`agag.listen`): a mention, or a topic it
        # owns — its channel or one of its prefixes. A `to=` in the post's
        # `ag-post` line alone reaches nobody, so it takes no line either.
        name = canned["agent"]
        if name.casefold() in addressed or channel == name \
                or any(topic.startswith(prefix) for prefix in canned.get("topics") or ()):
            return index, canned
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
