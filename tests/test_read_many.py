"""agent_guide p2 step 5: several conversations in one `agentchat read`, a
channel's latest ones, `topics --prefix`, and reads answered from a mirror.

p1's trials: runs 0170 and 0171 each lost turns to a shell loop over topics
that the harness refused ("Contains simple_expansion"), and run-0170 guessed
`agentchat topics --prefix`, which did not exist.
"""

from __future__ import annotations

import io

import pytest

from agag import chat
from agag.mirror.reads import FIXTURE_META, FixtureRefused, MirrorReads
from agag.mirror.store import Store

CHANNEL = "pj-growbox"


def message(id, topic, content, sender="autolab-agstudio1", sender_id=11, stream_id=7, channel=CHANNEL):
    return {"id": id, "type": "stream", "stream_id": stream_id, "display_recipient": channel, "subject": topic,
            "sender_id": sender_id, "sender_full_name": sender, "sender_realm_str": "", "timestamp": 1790500000 + id,
            "content": content}


def build_store(path, *, fixture: str = "", queue: str | None = "q1"):
    store = Store(path)
    with store.transaction():
        store.put_channels([{"stream_id": 7, "name": CHANNEL, "description": "study: grow box"},
                            {"stream_id": 8, "name": "routine-study-growbox", "description": "Routine"}])
        rows = [message(101, "workplan-growbox-r1", "round 1 plan"),
                message(102, "workplan-growbox-r1", "round 1 done"),
                message(103, "✔ workplan-growbox-r0", "round 0, finished"),
                message(104, "researchplan-growbox", "the plan"),
                message(105, "workplan-growbox-r2", "round 2 started")]
        for row in rows:
            store.put_message(row)
        store.put_topics(7, [{"name": "workplan-growbox-r1", "max_id": 102},
                             {"name": "✔ workplan-growbox-r0", "max_id": 103},
                             {"name": "researchplan-growbox", "max_id": 104},
                             {"name": "workplan-growbox-r2", "max_id": 105}])
        for topic, ids in (("workplan-growbox-r1", (101, 102)), ("✔ workplan-growbox-r0", (103, 103)),
                           ("researchplan-growbox", (104, 104)), ("workplan-growbox-r2", (105, 105))):
            store.set_coverage(7, topic, complete=True, oldest_id=ids[0], newest_id=ids[1], at=0)
        if queue:
            store.set_checkpoint(queue, 10)
        if fixture:
            store.set_meta(FIXTURE_META, fixture)
            store.set_meta("self_id", "15")
            store.set_meta("full_name", "Front")
    store.close()
    return path


def run(monkeypatch, argv, environ):
    for key in (chat.ENV_VARIABLE, chat.MIRROR_VARIABLE):
        monkeypatch.delenv(key, raising=False)
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    code = chat.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def fixture_board(tmp_path):
    return build_store(tmp_path / "mirror.sqlite", fixture="unit")


# --- several topics in one call -------------------------------------------------


def test_read_takes_several_topics_and_heads_each(monkeypatch, fixture_board):
    code, out, err = run(monkeypatch, ["read", CHANNEL, "workplan-growbox-r1", "workplan-growbox-r2"],
                         {chat.MIRROR_VARIABLE: str(fixture_board)})
    assert code == 0, err
    assert out.index("== #pj-growbox › workplan-growbox-r1 ==") < out.index("round 1 done")
    assert out.index("== #pj-growbox › workplan-growbox-r2 ==") < out.index("round 2 started")


def test_one_topic_prints_as_before_without_a_heading(monkeypatch, fixture_board):
    code, out, _ = run(monkeypatch, ["read", CHANNEL, "workplan-growbox-r1"], {chat.MIRROR_VARIABLE: str(fixture_board)})
    assert code == 0 and "==" not in out and "round 1 plan" in out


def test_latest_reads_the_newest_topics_and_prefix_narrows_them(monkeypatch, fixture_board):
    env = {chat.MIRROR_VARIABLE: str(fixture_board)}
    code, out, _ = run(monkeypatch, ["read", CHANNEL, "--latest", "2"], env)
    assert code == 0
    assert "workplan-growbox-r2 ==" in out and "researchplan-growbox ==" in out and "round 1" not in out
    code, out, _ = run(monkeypatch, ["read", CHANNEL, "--latest", "5", "--prefix", "workplan-"], env)
    assert code == 0 and "researchplan" not in out
    # A ✔ conversation is listed too, under its ✔ name.
    assert "✔ workplan-growbox-r0 ==" in out and "round 0, finished" in out


def test_read_refuses_topics_and_latest_together_and_prefix_alone(monkeypatch, fixture_board):
    env = {chat.MIRROR_VARIABLE: str(fixture_board)}
    code, _, err = run(monkeypatch, ["read", CHANNEL, "workplan-growbox-r1", "--latest", "2"], env)
    assert code == 1 and "not both" in err
    code, _, err = run(monkeypatch, ["read", CHANNEL, "--prefix", "workplan-"], env)
    assert code == 1 and "--prefix goes with --latest" in err
    code, _, err = run(monkeypatch, ["read", CHANNEL], env)
    assert code == 1 and "name a topic" in err


def test_topics_prefix_keeps_the_names_that_start_with_it(monkeypatch, fixture_board):
    code, out, _ = run(monkeypatch, ["topics", CHANNEL, "--prefix", "workplan-"],
                       {chat.MIRROR_VARIABLE: str(fixture_board)})
    assert code == 0
    assert out.splitlines() == ["workplan-growbox-r2", "✔ workplan-growbox-r0", "workplan-growbox-r1"]


def test_index_names_both(monkeypatch):
    assert "--latest N" in chat.USAGE_DOC and "--prefix p" in chat.USAGE_DOC


# --- reads from a mirror ------------------------------------------------------


class Live:
    """The live client, recording what reached it."""

    def __init__(self):
        self.calls = []

    def topic_history(self, channel, topic, num_before=50):
        self.calls.append(("history", channel, topic))
        return [message(900, topic, "from the live realm", channel=channel)]

    def stream_id(self, name):
        self.calls.append(("stream_id", name))
        return 99

    def send_to_channel(self, channel, topic, content):
        self.calls.append(("send", channel, topic))
        return 901


def test_a_live_mirror_answers_what_it_holds_and_the_live_client_the_rest(tmp_path):
    live = Live()
    reads = MirrorReads(build_store(tmp_path / "m.sqlite"), live=lambda: live)
    assert [m["content"] for m in reads.topic_history(CHANNEL, "workplan-growbox-r1")] == ["round 1 plan", "round 1 done"]
    assert reads.topic_last_id(CHANNEL, "workplan-growbox-r2") == 105
    assert reads.channel_topics(7)[0] == "workplan-growbox-r2"
    assert live.calls == []
    # A channel the mirror does not hold (a private one) is the live client's.
    assert reads.topic_history("secret", "x")[0]["content"] == "from the live realm"
    # Writes were never the mirror's.
    assert reads.send_to_channel(CHANNEL, "t", "hi") == 901
    assert ("send", CHANNEL, "t") in live.calls


def test_a_mirror_not_yet_built_is_not_used(tmp_path, monkeypatch):
    path = build_store(tmp_path / "m.sqlite", queue=None)
    monkeypatch.setenv(chat.MIRROR_VARIABLE, str(path))
    assert chat._mirror_reads(dict(__import__("os").environ), True) is None


def test_only_look_commands_read_the_mirror(tmp_path):
    path = build_store(tmp_path / "m.sqlite")
    environ = {chat.MIRROR_VARIABLE: str(path)}
    assert isinstance(chat._mirror_reads(environ, True), MirrorReads)
    assert chat._mirror_reads(environ, False) is None
    assert "recheck" not in chat.MIRROR_READS and "send" not in chat.MIRROR_READS


def test_a_fixture_board_answers_everything_and_writes_nothing(monkeypatch, fixture_board):
    env = {chat.MIRROR_VARIABLE: str(fixture_board)}
    code, _, err = run(monkeypatch, ["send", CHANNEL, "workplan-growbox-r1", "hello"], env)
    assert code == 1 and "fixture" in err
    reads = MirrorReads(fixture_board)
    assert reads.whoami()["user_id"] == 15
    assert reads.topic_history(CHANNEL, "nothing-here") == []
    with pytest.raises(FixtureRefused):
        reads.stream_id("not-on-the-board")
    # A fixture answers for its own people (agent_guide p2 ex1: `send --to <name>`); this one holds none.
    assert reads.users() == []


def test_the_store_is_opened_read_only(tmp_path):
    path = build_store(tmp_path / "m.sqlite")
    store = Store.open_readonly(path)
    with pytest.raises(Exception):
        store.set_meta("x", "y")


def test_why_after_the_options_is_the_why():
    """The form every record command's help shows: `hold <id> --for … --evidence <post> "why"`
    (agent_guide p2 step 7: a live run's first try was refused as unrecognized arguments)."""
    parser = chat.build_parser()
    args = chat.parse(parser, ["hold", "15835", "--for", "decision", "--evidence", "15835", "their own call"])
    assert args.message_id == 15835 and args.why == ["their own call"] and args.purpose == "decision"
    args = chat.parse(parser, ["release", "15837", "--evidence", "15842", "trial", "over"])
    assert args.hold_id == 15837 and args.why == ["trial", "over"]
    args = chat.parse(parser, ["disposition", "15835", "withdrawn", "--evidence", "15842", "trial over"])
    assert args.kind == "withdrawn" and args.why == ["trial over"]
    with pytest.raises(SystemExit):
        chat.parse(parser, ["read", "c", "t", "--nope"])
