"""Notices: a fixed first line a program writes, and a wait that ends only on it.

The failure this pins is `observer` p2 ex1's: a waiter that read "any output"
as arrival woke on `agentchat read`'s own "nothing newer" line. `agag wait`
ends on exactly one thing — a notice for the watch it was given.
"""

import argparse
import io

import pytest

from agag import cli, notice


def test_the_line_round_trips():
    line = notice.notice_line("w6917")
    assert line == "[notice][watch] w6917 met"
    assert notice.parse_notice(line + "\n\n**Watch `w6917` met** — prose") == notice.Notice("watch", "w6917", "met")


@pytest.mark.parametrize("content", [
    "nothing newer than message 6914 in #sandbox > omni-wake-test",
    "**Watch `w6917` met** — the old human line alone",
    "as Observer said: [notice][watch] w6917 met",
    "[notice][watch] w6917 met and more on the same line",
    "[selfnote][state] met",
    "",
    None,
])
def test_only_an_exact_first_line_is_a_notice(content):
    assert notice.parse_notice(content) is None


def test_a_malformed_name_is_refused_when_building():
    with pytest.raises(ValueError):
        notice.notice_line("6917")


def test_find_notice_picks_the_named_watch_only():
    messages = [
        {"id": 1, "content": "[notice][watch] w1 met\nother watch"},
        {"id": 2, "content": "unrelated"},
        {"id": 3, "content": "[notice][watch] w2 met\nours"},
    ]
    message, found = notice.find_notice(messages, "w2")
    assert message["id"] == 3 and found.outcome == "met"
    assert notice.find_notice(messages, "w9") is None


class Client:
    """Serves a scripted sequence of reads, one per poll."""

    def __init__(self, rounds, newest=10):
        self.rounds = list(rounds)
        self.newest = newest
        self.since_calls = []

    def topic_last_id(self, channel, topic):
        return self.newest if not topic.startswith("✔ ") else 0

    def topic_since(self, channel, topic, after_id, num_after=100):
        if topic.startswith("✔ "):
            return []
        self.since_calls.append(after_id)
        step = self.rounds.pop(0) if self.rounds else []
        if isinstance(step, Exception):
            raise step
        return [m for m in step if m["id"] > after_id]


def args(**over):
    base = dict(channel="sandbox", topic="omni-wake-test", watch="w6917", since=6914, timeout=100.0, interval=10.0)
    base.update(over)
    return argparse.Namespace(**base)


def run(client, clock_step=10.0, **over):
    now = [0.0]
    out, err = io.StringIO(), io.StringIO()
    code = notice.wait_command(
        args(**over), client=client, out=out, err=err,
        clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + clock_step),
    )
    return code, out.getvalue(), err.getvalue()


def test_wait_ignores_other_posts_and_failed_reads_then_ends_on_the_notice():
    client = Client([
        [],
        [{"id": 6915, "sender_full_name": "someone", "timestamp": 0, "content": "unrelated"}],
        ConnectionError("realm down"),
        [{"id": 6921, "sender_full_name": "agobserver", "timestamp": 0,
          "content": "[notice][watch] w6917 met\n\n**Watch `w6917` met** — ..."}],
    ])
    code, out, err = run(client)
    assert code == 0
    assert "(message 6921)" in out
    assert "read failed" in err
    assert len(client.since_calls) == 4


def test_a_notice_for_another_watch_does_not_end_the_wait():
    client = Client([[{"id": 6921, "content": "[notice][watch] w1 met"}]] * 20)
    code, out, err = run(client, timeout=30.0)
    assert code == notice.TIMEOUT_EXIT
    assert out == ""
    assert "no notice for w6917" in err


def test_since_defaults_to_the_newest_message_now():
    client = Client([[{"id": 11, "content": "[notice][watch] w6917 met"}]], newest=10)
    code, _, _ = run(client, since=None)
    assert code == 0
    assert client.since_calls == [10]


def test_missing_credentials_fail_with_the_variable_named(monkeypatch):
    monkeypatch.delenv("AGENTCHAT_ZULIP_ENV", raising=False)
    err = io.StringIO()
    code = notice.wait_command(args(), out=io.StringIO(), err=err)
    assert code == 2
    assert "AGENTCHAT_ZULIP_ENV" in err.getvalue()


def test_agag_has_wait_and_agentchat_does_not():
    from agag import chat

    with pytest.raises(SystemExit):
        chat.build_parser().parse_args(["wait", "c", "t"])
    parsed = argparse.ArgumentParser()
    sub = parsed.add_subparsers(dest="command")
    notice.add_wait_parser(sub)
    assert parsed.parse_args(["wait", "c", "t", "--watch", "w1"]).func is notice.wait_command
    assert cli.main.__module__ == "agag.cli"
