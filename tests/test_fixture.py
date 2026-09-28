"""agent_guide p2 step 6: the fixture board a guide trial reads."""

from __future__ import annotations

import io

import pytest

from agag import chat
from agag.fixture import PROBES, build_store, judge
from agag.fixture.board import (AUTOLAB, BOARD_VERSION, DEV, FRONT, M_CONTROL_LOOP, M_GERMINATION, Board,
                                build_board, line)
from agag.fixture.consistency import problems
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
    assert any(f"m{M_CONTROL_LOOP}" in m["content"] for m in running)
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
    assert environment["AGENTCHAT_JOURNAL"] == str(store.parent / "listener.sqlite")
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


def test_a_send_into_a_named_topic_is_judged_but_a_read_of_it_is_not():
    """agent_guide p3's fd-wr guard: a second start into a ✔ topic's bare name."""
    probe = PROBES["guard-finished"]
    read = "Bash: agentchat read pj-growbox workplan-growbox-germination-days"
    assert judge(probe, "終わっています（9d34067f5c0a）", [read])["passed"]
    assert judge(probe, "9d34067f5c0a", [read, "Bash: agentchat send pj-growbox workplan-growbox-germination-days-recap "
                                                "'何か残っていますか'"])["passed"]
    verdict = judge(probe, "9d34067f5c0a", [read, "Bash: agentchat send pj-growbox workplan-growbox-germination-days "
                                                  "--intent report '続きをお願いします'"])
    assert not verdict["passed"] and verdict["against"] == ["sent into: workplan-growbox-germination-days"]
    assert not judge(PROBES["guard-status"], f"m{M_CONTROL_LOOP}: 14 件中 9 件", ["Bash: agentchat send autolab-agstudio1 x 'どう？'"])["passed"]


def test_judge_names_what_is_missing():
    probe = PROBES["growbox-thing"]
    good = "#pj-growbox: 発芽(germination) と food safety は完了、control loop が進行中です。"
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


def test_agproject_status_reads_the_fixtures_repositories(store, monkeypatch):
    """agent_guide p2 ex1: a fixture study named like a real one got the real
    Gitea's facts; the fixture answers for its own repositories now."""
    from agag import project

    def no_gitea(*_, **__):
        raise AssertionError("the host's Gitea was asked")

    monkeypatch.setattr(project, "gitea_head", no_gitea)
    monkeypatch.setenv(chat.MIRROR_VARIABLE, str(store))
    monkeypatch.delenv(chat.ENV_VARIABLE, raising=False)
    out = io.StringIO()
    assert project.run(["status", "aisvgs"], out=out, err=io.StringIO()) == 0
    assert "state: ready" in out.getvalue() and "f57eed1a27de" in out.getvalue()
    assert "gitea.fixture.invalid" in out.getvalue()
    out = io.StringIO()
    assert project.run(["status", "worldtrend"], out=out, err=io.StringIO()) == 0
    assert '"exists": false' in out.getvalue()


# --- agent_guide p3 ex1: the board records what it says ------------------------------


def test_the_board_records_what_it_says(store):
    assert problems(store) == []


def test_a_finished_mission_traces_done_and_its_name_is_its_note(store):
    from agag.trace import trace

    board = client(store)
    root = trace(board, M_GERMINATION).root
    assert root.identity == f"mission m{M_GERMINATION} (growbox)" and root.state == "done"
    assert [c.state for c in root.children] == ["done"]
    history = board.topic_history("pj-growbox", "✔ workplan-growbox-germination-days", 200)
    assert any(m["content"].startswith("[selfnote][acceptance] #") and " after=#" in m["content"] for m in history)
    assert trace(board, M_CONTROL_LOOP).root.children[0].state == "executing"


def _small_board(done_line: str, *, mission_note: bool) -> Board:
    b = Board()
    for name in ("agents", "pj-x"):
        b.channel(name, name)
    b.post("agents", "intro-autolab-agstudio1", AUTOLAB, "# autolab")
    b.post("agents", "intro-front-agstudio1", FRONT, "# Front")
    b.post("pj-x", "workplan-x", FRONT, "@**autolab-agstudio1** one mission.\n\n" + line("response_request", to=AUTOLAB))
    if mission_note:
        b.post("pj-x", "workplan-x", AUTOLAB, "[selfnote][mission] x", ident=20390)
    b.post("pj-x", "workplan-x", AUTOLAB, done_line)
    b.resolve("pj-x", "workplan-x", FRONT)
    return b


def test_the_contradiction_p3_found_fails_the_check(tmp_path):
    """p3: "m20390 is done: accepted by Front" over a ✔ topic trace read as queued."""
    from agag.fixture import build_store

    bare = build_store(tmp_path / "bare", _small_board("m20390 is done: accepted by Front.", mission_note=False))
    assert problems(bare) == ["m20390 (named in #20004) has no [mission] note at #20390"]
    noted = build_store(tmp_path / "noted", _small_board("m20390 is done: accepted by Front.", mission_note=True))
    found = problems(noted)
    assert len(found) == 1 and found[0].startswith("#20391 says m20390 is done, and its trace reads ")


def test_an_agent_named_without_an_introduction_fails_the_check(tmp_path):
    from agag.fixture import build_store

    b = _small_board("nothing to report", mission_note=False)
    b.post("pj-x", "✔ workplan-x", FRONT, "@**agforge-agstudio1** could you draw it?")
    assert problems(build_store(tmp_path / "b", b)) == [
        "agforge-agstudio1 is named on the board and has no introduction in #agents"]


def test_a_mission_note_cannot_go_back_in_time():
    b = Board()
    b.channel("pj-x", "x")
    b.post("pj-x", "t", DEV, "hi", ident=20100)
    with pytest.raises(ValueError):
        b.post("pj-x", "t", DEV, "again", ident=20050)


def test_the_store_carries_the_board_version(store, tmp_path):
    from agag.fixture.run import board_version
    from agag.mirror.store import Store

    assert board_version(store) == BOARD_VERSION == 2
    old = build_store(tmp_path / "old")
    handle = Store(old)
    with handle.transaction():
        handle.set_meta("fixture_version", "")
    handle.close()
    assert board_version(old) == 1  # a store from before the stamp
