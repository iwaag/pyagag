"""agent_guide p2 step 6: the fixture board a guide trial reads."""

from __future__ import annotations

import io

import pytest

from agag import chat
from agag.fixture import PROBES, build_store, judge
from agag.fixture.board import FRONT, build_board
from agag.fixture.run import client, fixture_environment, outcome, probe_history
from agag.mirror.reads import FixtureRefused


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    return build_store(tmp_path_factory.mktemp("fixture"))


def test_the_board_is_the_same_every_time(tmp_path):
    first, second = build_board(), build_board()
    assert [(r["id"], r["topic"], r["content"]) for r in first.rows] == \
        [(r["id"], r["topic"], r["content"]) for r in second.rows]


def test_what_the_probes_need_is_on_the_board(store):
    board = client(store)
    assert {c["name"] for c in board.channels()} >= {"pj-aisvgs", "pj-growbox", "pj-protoprey", "agents",
                                                     "routine-study-growbox", "archsage-agstudio1"}
    decision = board.topic_history("archsage-agstudio1", "✔ study-aisvgs-round3")
    assert any("round 3 は自分で" in m["content"] for m in decision)
    running = board.topic_history("pj-growbox", "workplan-growbox-control-loop")
    assert any("m20402" in m["content"] for m in running)
    assert "✔ front-desk-20260926-1200" in board.channel_topics(board.stream_id("front"))


def test_the_intros_and_the_reader_identity(store, monkeypatch):
    monkeypatch.setenv(chat.MIRROR_VARIABLE, str(store))
    monkeypatch.delenv(chat.ENV_VARIABLE, raising=False)
    out = io.StringIO()
    assert chat.main(["intro"], out=out, err=io.StringIO()) == 0
    assert len(out.getvalue().splitlines()) == 6
    assert client(store).whoami()["user_id"] == FRONT


def test_the_fixture_takes_no_post(store):
    with pytest.raises(FixtureRefused):
        client(store).send_to_channel("front", "x", "hi")


def test_a_run_started_inside_reads_the_fixture(store):
    from agag import agent

    with fixture_environment(store):
        environment = agent.chat_environment(_Spec())
    assert environment["AGENTCHAT_MIRROR"] == str(store)
    assert agent.chat_environment(_Spec())["AGENTCHAT_MIRROR"].endswith("mirror/mirror.sqlite")


class _Spec:
    from pathlib import Path

    zulip_env = Path("/nonexistent/zulip.env")
    local = Path("/nonexistent/.local")


def test_every_probe_has_a_rule_and_a_home(store):
    board = client(store)
    for probe in PROBES.values():
        assert (probe.must or probe.tools_must) and probe.channel and probe.topic
        assert probe_history(probe, board)[-1]["subject"].endswith(probe.topic)


def test_a_probe_without_text_is_served_its_conversation_from_the_board(store):
    history = probe_history(PROBES["receipt-owed"], client(store))
    assert len(history) > 3 and "[Observer]" in history[-1]["content"]


def test_tool_rules_judge_acts():
    probe = PROBES["receipt-owed"]
    assert judge(probe, "done", ["Bash: agentchat receipt 20110", "Bash: agentchat receipt 20110 --repair --because 20111"])["passed"]
    assert not judge(probe, "done", ["Bash: agentchat receipt 20110"])["passed"]
    assert not judge(probe, "done", ["Bash: agentchat receipt 20110 --repair",
                                     "Bash: agentchat send front x '[selfnote][served] a 1'"])["passed"]


def test_judge_names_what_is_missing():
    probe = PROBES["growbox-thing"]
    good = "#pj-growbox: 発芽(germination) と food safety は完了、control loop (m20402) が進行中です。"
    assert judge(probe, good)["passed"]
    verdict = judge(probe, "pj-growbox の発芽は終わっています。")
    assert not verdict["passed"] and any("control loop" in m for m in verdict["missing"])
    assert not judge(PROBES["forge-protoprey"], "hero, meadow, footsteps… and a birthday card")["passed"]


def test_outcome_takes_the_marked_reply(tmp_path):
    result = outcome(PROBES["triage-unopened"], "thinking…\n<ag-reply intent=report>\nlegit\n</ag-reply>", tmp_path)
    assert result["passed"] and result["marked"] and (tmp_path / "outcome.json").is_file()


def test_a_trace_runs_on_the_fixture(store):
    """`agag.trace` asks `hasattr(client, "roster_owner")`: a fixture must
    answer it, not refuse it (agent_guide p2 step 7, the triage probe)."""
    from agag.trace import trace, trace_lines

    board = client(store)
    first = board.topic_history("pj-protoprey", "workplan-protoprey-locations", 50)[0]
    assert trace_lines(trace(board, int(first["id"])))
    assert board.roster_owner("pj-protoprey", "workplan-protoprey-locations") == ""
