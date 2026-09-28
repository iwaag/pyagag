"""Serving one probe against the fixture board, as a listener would.

An agent's trial driver (`agfront.trial`, …) builds the serving with the
agent's own code — the same chatlog, tools files and prompt its listener
would build — and runs it inside `fixture_environment`, which does two
things for the duration:

- every run started through `agag.agent` gets `AGENTCHAT_MIRROR` set to the
  fixture store instead of the listener's own mirror, so what the run reads
  with `agentchat` (and `agproject status`) is the fixture board, and what it
  tries to post is refused;
- `client()` is a `MirrorReads` over the fixture, the client the serving's
  own reads go through.

`probe_history` is the conversation a probe is served: the person's one
post, with an id above anything on the board. `outcome` pulls the reply the
run marked and judges it (`probes.judge`).
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from agag import agent as agent_module
from agag.mirror.reads import MirrorReads
from agag.reply import split_reply

from .board import DEV, NAMES, OBSERVER
from .probes import Probe, judge

__all__ = ["PROBE_ID", "client", "fixture_environment", "outcome", "probe_history"]

#: The probe post's id: above every id the board holds.
PROBE_ID = 30_001


@contextlib.contextmanager
def fixture_environment(store: Path):
    """Runs started inside read the fixture board and can post nowhere."""
    original = agent_module.chat_environment

    def chat_environment(spec, **kwargs):
        environment = original(spec, **kwargs)
        environment["AGENTCHAT_MIRROR"] = str(store)
        return environment

    agent_module.chat_environment = chat_environment
    try:
        yield
    finally:
        agent_module.chat_environment = original


def client(store: Path) -> MirrorReads:
    return MirrorReads(Path(store))


def probe_history(probe: Probe, board=None) -> list[dict]:
    """The probe's one post; or, for a probe with no text, its conversation
    as it stands on the board (`board`, a fixture client)."""
    if not probe.text and board is not None:
        return board.topic_history(probe.channel, probe.topic, 200)
    sender = OBSERVER if probe.speaker == NAMES[OBSERVER] else DEV
    return [{"id": PROBE_ID, "type": "stream", "display_recipient": probe.channel, "subject": probe.topic,
             "sender_id": sender, "sender_full_name": probe.speaker, "sender_realm_str": "",
             "timestamp": int(time.time()), "content": probe.text}]


def outcome(probe: Probe, output: str, directory: Path, **facts) -> dict:
    """The marked reply, judged, written beside the run as `outcome.json`."""
    split = split_reply(output or "")
    reply = split.reply if split.reply else (output or "")
    verdict = judge(probe, reply, facts.get("tool_calls"))
    result = {**verdict, "marked": bool(split.reply), **facts, "reply": reply}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "outcome.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (directory / "reply.md").write_text(reply + "\n", encoding="utf-8")
    return result
