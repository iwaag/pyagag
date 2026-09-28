"""Does the fixture board record what it says? (`agent_guide` p3 ex1)

p3 found the board saying "m20390 is done: accepted by Front" over a
✔ topic that `agentchat trace` read as `queued`: the builder wrote the done
line and no record behind it, and one careful run believed the trace. The
board is only worth measuring on if its words and its records agree, so
this reads the built store the way a run does (`MirrorReads`, `agag.trace`)
and lists every disagreement:

- a mission named on the board (`m<id>`) has a `[selfnote][mission]` note
  at that id — the mission's name *is* its note's id;
- a mission a post calls done or accepted traces as done;
- a ✔ task conversation (`workrun-…`) traces as done, and its mission is
  not `queued`;
- every agent the board names (a sender or a mention) has an introduction
  in `#agents`.

    python -m agag.fixture consistency <dir>     # <dir>/mirror.sqlite; exit 1 on any problem
"""

from __future__ import annotations

import re
from pathlib import Path

from agag.mirror.reads import MirrorReads
from agag.selfnote import parse_note
from agag.trace import trace

__all__ = ["problems"]

#: `m20420`, also inside `work-m20420`; not `sm20420` or a longer number.
_MISSION_NAME = re.compile(r"(?<![A-Za-z0-9_])m(\d{5})(?!\d)")
#: A post saying a mission finished.
_SAYS_DONE = re.compile(r"(?<![A-Za-z0-9_])m(\d{5})(?!\d)[^.\n]{0,40}?\b(?:is done|accepted|done)\b")
_MENTION = re.compile(r"@_?\*\*(?P<name>[^*|]+?)(?:\|\d+)?\*\*")
AGENTS_CHANNEL = "agents"


def _every_message(board: MirrorReads) -> list[dict]:
    messages: list[dict] = []
    for channel in board.channels():
        for topic in board.channel_topics(int(channel["stream_id"])):
            messages += board.topic_history(channel["name"], topic, 10_000)
    return sorted(messages, key=lambda m: int(m["id"]))


def _introduced(board: MirrorReads) -> set[str]:
    return {topic[len("intro-"):] for topic in board.channel_topics(board.stream_id(AGENTS_CHANNEL))
            if topic.startswith("intro-")}


def _has_intro(name: str, instances: set[str]) -> bool:
    folded = name.casefold()
    return any(instance == folded or instance.startswith(folded + "-") for instance in instances)


def problems(store: Path) -> list[str]:
    """Every disagreement between what the board says and what it records;
    empty when it holds what it claims."""
    board = MirrorReads(Path(store))
    messages = _every_message(board)
    found: list[str] = []

    missions = {int(m["id"]) for m in messages if parse_note(m.get("content"), "mission") is not None}
    named: dict[int, int] = {}
    for message in messages:
        for match in _MISSION_NAME.finditer(f"{message.get('display_recipient')} {message.get('subject')} "
                                            f"{message.get('content')}"):
            named.setdefault(int(match.group(1)), int(message["id"]))
    for mission, where in sorted(named.items()):
        if mission not in missions:
            found.append(f"m{mission} (named in #{where}) has no [mission] note at #{mission}")

    states: dict[int, str] = {}

    def state(mission: int) -> str:
        if mission not in states:
            root = trace(board, mission).root
            states[mission] = root.state if root is not None else "unreadable"
        return states[mission]

    for message in messages:
        if str(message.get("content") or "").startswith("[selfnote]"):
            continue
        for match in _SAYS_DONE.finditer(str(message.get("content") or "")):
            mission = int(match.group(1))
            if mission in missions and state(mission) != "done":
                found.append(f"#{message['id']} says m{mission} is done, and its trace reads {state(mission)}")

    for message in messages:
        task = parse_note(message.get("content"), "task")
        if task is None or not str(message.get("subject") or "").startswith("✔ "):
            continue
        mission = int(task.split("#")[0])
        node = trace(board, int(message["id"])).root
        task_state = node.state if node is not None and node.identity == f"task {task}" else None
        if task_state not in ("done", "cancelled"):
            found.append(f"task {task} is ✔ and its trace reads {task_state or 'nothing'}")
        if state(mission) == "queued":
            found.append(f"task {task} is ✔ and its mission m{mission} reads queued")

    bots = {str(u["full_name"]) for u in board.users() if u.get("is_bot")}
    named_agents = {str(m.get("sender_full_name")) for m in messages} | {
        match.group("name") for m in messages for match in _MENTION.finditer(str(m.get("content") or ""))}
    instances = _introduced(board)
    for name in sorted(named_agents & bots):
        if not _has_intro(name, instances):
            found.append(f"{name} is named on the board and has no introduction in #{AGENTS_CHANNEL}")
    return found
