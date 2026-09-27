"""failsafe p4 step 3: one post holds what the server advertises, nothing is
cut, and a reply over it is a repairable failure, never a false delivery."""

from __future__ import annotations

import json
import time

import pytest

from agag import zulip as zulip_module
from agag.reply import resolve_reply
from agag.zulip import DEFAULT_MAX_MESSAGE_LENGTH, MessageTooLong, ZulipClient, ZulipError


class Probe(ZulipClient):
    """A client whose transport is a list: what was called, what it answers."""

    def __init__(self, limit=100000, fail=False):
        super().__init__("https://zulip.invalid", "bot@invalid", "key")
        self.sent, self.registers, self.limit, self.fail = [], 0, limit, fail

    def call(self, method, path, params=None, timeout=30):
        if path == "register":
            self.registers += 1
            if self.fail:
                raise ZulipError("down")
            return {"queue_id": "q1", "max_message_length": self.limit}
        if path == "events":
            return {}
        self.sent.append((path, params))
        return {"id": 99}


def env_file(tmp_path):
    path = tmp_path / "bot.env"
    path.write_text("ZULIP_URL=https://zulip.invalid\nZULIP_EMAIL=bot@invalid\nZULIP_API_KEY=key\n")
    return path


def test_the_limit_is_asked_once_and_shared_through_the_credentials_file(tmp_path, monkeypatch):
    path = env_file(tmp_path)
    first = Probe()
    first.limits_file = path.with_name("bot.env.limits")
    assert first.max_message_length() == 100000
    assert first.max_message_length() == 100000 and first.registers == 1
    # Another process on the same credential reads it, and asks nothing.
    second = Probe(limit=5)
    second.limits_file = first.limits_file
    assert second.max_message_length() == 100000 and second.registers == 0
    # Stale after the TTL: asked again, and the new value is kept.
    data = json.loads(first.limits_file.read_text())
    data["at"] = time.time() - zulip_module.LIMITS_TTL_SECONDS - 1
    first.limits_file.write_text(json.dumps(data))
    third = Probe(limit=50000)
    third.limits_file = first.limits_file
    assert third.max_message_length() == 50000 and third.registers == 1


def test_an_unreadable_limit_keeps_the_last_learnt_or_the_smallest_default(tmp_path):
    down = Probe(fail=True)
    assert down.max_message_length() == DEFAULT_MAX_MESSAGE_LENGTH
    stale = {"max_message_length": 100000, "at": 0, "url": "https://zulip.invalid"}
    (tmp_path / "x.limits").write_text(json.dumps(stale))
    down.limits_file = tmp_path / "x.limits"
    down._limits = None
    assert down.max_message_length() == 100000


def test_from_env_keeps_the_limits_beside_the_credentials(tmp_path):
    client = ZulipClient.from_env(env_file(tmp_path))
    assert client.limits_file == tmp_path / "bot.env.limits"


def test_an_over_long_post_is_refused_before_it_is_sent_never_cut():
    client = Probe(limit=1000)
    client.send_to_channel("c", "t", "x" * 1000)
    with pytest.raises(MessageTooLong) as refused:
        client.send_to_channel("c", "t", "x" * 1001)
    assert refused.value.length == 1001 and refused.value.limit == 1000
    with pytest.raises(MessageTooLong):
        client.send_dm([1], "y" * 1001)
    assert len(client.sent) == 1


def test_a_reply_over_the_room_is_repaired_to_fit_or_fails_whole():
    long_reply = "<ag-reply intent=report>\n" + "a" * 500 + "\n```\ncode\n```\ntrailing summary\n</ag-reply>"
    asked = []

    def repair(reason):
        asked.append(reason)
        return "<ag-reply intent=report>\nshort; the whole is in out.md\n</ag-reply>"

    text, split, repaired = resolve_reply(long_reply, repair, limit=200)
    assert repaired and split.ok and text == "short; the whole is in out.md"
    assert "is 5" in asked[0] and "holds at most 200" in asked[0] and "nothing is cut" in asked[0]

    text, split, repaired = resolve_reply(long_reply, lambda reason: long_reply, limit=200)
    assert not split.ok and "holds at most 200" in split.error
    assert split.reply.endswith("trailing summary"), "the words are kept whole for the owed retry"
    assert text.startswith("(this run produced no reply:")

    # Within the room it is posted as written, fences and all.
    text, split, _ = resolve_reply(long_reply, None, limit=1000)
    assert split.ok and text.endswith("trailing summary")
