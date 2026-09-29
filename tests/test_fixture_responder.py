"""agent_guide p2 ex1 step 3: the fixture answers a delegation, on an overlay."""

from __future__ import annotations

import hashlib
import io

import pytest

from agag import chat
from agag.fixture import PROBES, build_store, judge
from agag.fixture.board import AUTOLAB, DEV, FRONT, NAMES
from agag.fixture.responder import Canned, deliver, door_log, make_overlay, posts_since
from agag.fixture.run import client
from agag.mirror.reads import FixtureRefused

SCRIPT = (Canned("autolab-agstudio1", "{asker} Twenty seconds. Answers #{ask}."),
          Canned("autolab-agstudio1", "{asker} Done. Answers #{ask}."))

#: m20510's own topics on board 2: the plan topic and its running task.
PLAN = ("pj-growbox", "workplan-growbox-control-loop")
TASK = ("work-m20510", "workrun-task1-m20510")


@pytest.fixture
def boards(tmp_path):
    store = build_store(tmp_path / "board")
    overlay = make_overlay(store, tmp_path / "overlay", SCRIPT, {name: ident for ident, name in NAMES.items()})
    return store, overlay


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_send_is_recorded_and_answered_on_the_overlay_only(boards):
    store, overlay = boards
    before = _digest(store)
    newest = client(overlay).store.newest_id()
    sent = client(overlay).send_to_channel(*TASK, "how long does the pump run?")
    assert [m["id"] for m in posts_since(overlay, newest)] == [sent]  # answered once the serving is over
    assert deliver(overlay) == [sent + 1] and deliver(overlay) == []
    after = posts_since(overlay, newest)
    assert [m["id"] for m in after] == [sent, sent + 1]
    assert after[1]["sender_id"] == AUTOLAB and after[1]["content"] == f"@**Front** Twenty seconds. Answers #{sent}."
    assert _digest(store) == before
    with pytest.raises(FixtureRefused):
        client(store).send_to_channel(*TASK, "x")


def test_only_a_post_addressing_the_agent_takes_its_next_line(boards):
    _, overlay = boards
    board = client(overlay)
    newest = board.store.newest_id()
    board.send_to_channel("pj-aisvgs", "notes", "nobody is asked here")
    board.send_to_channel("pj-aisvgs", "notes", "[selfnote][rootchat] front/x #1 rel=work")
    assert [m["sender_id"] for m in posts_since(overlay, newest)] == [FRONT, FRONT]
    assert deliver(overlay) == []
    # A mention outside autolab's topics is served live only as a callback to
    # its own task (agautolab `handle_mention`): nothing answers it here.
    board.send_to_channel("pj-aisvgs", "notes", "@**autolab-agstudio1** and you?")
    assert deliver(overlay) == []
    board.send_to_channel("autolab-agstudio1", "q", "a question in autolab's own channel")
    board.send_to_channel(*PLAN, "@**autolab-agstudio1** and in the mission's plan topic?")
    deliver(overlay)
    senders = [m["sender_id"] for m in posts_since(overlay, newest)]
    assert senders == [FRONT, FRONT, FRONT, FRONT, FRONT, AUTOLAB, AUTOLAB]
    board.send_to_channel("autolab-agstudio1", "q", "the script is spent")
    assert deliver(overlay) == []
    assert [d["door"] for d in door_log(overlay)] == ["none", "none", "answer", "answer", "none"]


def test_a_to_line_alone_reaches_nobody(boards):
    """A post outside the agent's topics that only says `to=11` in its
    ag-post line serves no listener, so no scripted line answers it."""
    _, overlay = boards
    board = client(overlay)
    board.send_to_channel("pj-growbox", "growbox-recap", "how did it go?\n\n`ag-post intent=response_request to=11 ask=question`")
    assert deliver(overlay) == []
    board.send_to_channel("autolab-agstudio1", "growbox-recap", "how did it go?")
    assert len(deliver(overlay)) == 1


def test_other_writes_stay_refused_on_the_overlay(boards):
    _, overlay = boards
    board = client(overlay)
    with pytest.raises(FixtureRefused):
        board.add_reaction(1, "eyes")
    with pytest.raises(FixtureRefused):
        board.resolve_topic(1)
    assert board.ensure_subscribed("pj-growbox") is False


def test_agentchat_send_on_the_overlay_writes_the_root_note_first(boards, monkeypatch):
    _, overlay = boards
    monkeypatch.setenv(chat.MIRROR_VARIABLE, str(overlay))
    monkeypatch.delenv(chat.ENV_VARIABLE, raising=False)
    monkeypatch.setenv("AGENTCHAT_HOME", "front/front-desk-fixture-pump")
    newest = client(overlay).store.newest_id()
    out = io.StringIO()
    assert chat.main(["send", "autolab-agstudio1", "growbox-pump", "--intent", "response_request", "--to",
                      "autolab-agstudio1", "--ask", "question", "the pump?"], out=out, err=io.StringIO()) == 0
    deliver(overlay)
    posts = posts_since(overlay, newest)
    assert posts[0]["content"].startswith("[selfnote][rootchat] front/front-desk-fixture-pump")
    assert "to=11" in posts[1]["content"] and posts[2]["sender_id"] == AUTOLAB


def test_scripted_probes_are_judged_over_the_conversation():
    probe = PROBES["delegate-decision"]
    final = "autolab は照明を 1日12時間（06:00–18:00）に設定しました。"
    calls = ["Bash: agentchat send pj-growbox workplan-growbox-lights '@**autolab-agstudio1** …'"]
    assert judge(probe, final, calls, sends=["…", "12 h a day, please"], servings=3)["passed"]
    assert not judge(probe, final, calls, sends=["…"], servings=3)["passed"]
    assert not judge(probe, final, calls, sends=["12 h"], servings=2)["passed"]
    assert judge(PROBES["delegate-answer"], "41分、有料論文で12分止まった", calls, servings=2)["passed"]


# p3 ex1's four wrong-door runs (open finding 2): each asked autolab to decide
# m20510's light hours in a new `workplan-` topic after the plan topic
# refused a second anchor, and the responder answered with the decision
# question. Live autolab plans a new mission there. (run, topic, question)
WRONG_DOOR_RUNS = (
    ("ctl/delegate-decision-2", "workplan-growbox-control-loop-lighting-hours",
     "growbox の制御ループ (m20510) について: 照明を1日何時間点けるべきか決めて教えてください。"),
    ("ctl/delegate-decision-5", "workplan-growbox-lighting-hours",
     "growbox の制御ループ(m20510、strand 3)について: 照明を1日何時間点灯させるか決めてください。"),
    ("ctl/delegate-decision-12", "workplan-growbox-lighting-hours",
     "m20510 (growbox control loop, task 1 in work-m20510 / workrun-task1-m20510): decide the light hours."),
    ("fix/delegate-decision-11", "workplan-growbox-lighting-hours",
     "For m20510 (growbox control loop), please decide how many hours per day the lights stay on."),
)


@pytest.mark.parametrize("run, topic, question", WRONG_DOOR_RUNS, ids=[r[0] for r in WRONG_DOOR_RUNS])
def test_a_new_workplan_topic_is_a_new_request_not_the_answer(tmp_path, monkeypatch, run, topic, question):
    """agent_guide p3 ex2 step 1: the question takes no script line; autolab
    answers as a planner of a new mission, and the choice sent after it into
    the same topic takes none either. The same question in the running
    task's topic takes the scripted decision question."""
    store = build_store(tmp_path / "board")
    overlay = make_overlay(store, tmp_path / "overlay", PROBES["delegate-decision"].script,
                           {name: ident for ident, name in NAMES.items()})
    monkeypatch.setenv(chat.MIRROR_VARIABLE, str(overlay))
    monkeypatch.delenv(chat.ENV_VARIABLE, raising=False)
    monkeypatch.setenv("AGENTCHAT_HOME", "front/front-desk-fixture-lights")
    newest = client(overlay).store.newest_id()
    assert chat.main(["send", "pj-growbox", topic, "--intent", "response_request", "--to", "autolab-agstudio1",
                      "--ask", "question", question], out=io.StringIO(), err=io.StringIO()) == 0
    [planned] = deliver(overlay)
    answer = client(overlay).store.message(planned)
    assert answer.sender_id == AUTOLAB and "new mission" in answer.content and "16 h" not in answer.content
    asked = [d for d in door_log(overlay) if d["topic"] == topic]
    assert [d["door"] for d in asked] == ["new"]
    assert chat.main(["send", "pj-growbox", topic, "--intent", "report", "12時間/日でお願いします。"],
                     out=io.StringIO(), err=io.StringIO()) == 0
    assert deliver(overlay) == []
    assert all(m["sender_id"] == FRONT or m["id"] == planned for m in posts_since(overlay, newest))
    # The running task's own topic is the door for a question about it.
    monkeypatch.setenv("AGENTCHAT_HOME", "front/front-desk-fixture-lights-2")
    assert chat.main(["send", *TASK, "--intent", "response_request", "--to", "autolab-agstudio1",
                      "--ask", "question", question], out=io.StringIO(), err=io.StringIO()) == 0
    [decision] = deliver(overlay)
    assert "16 h a day" in client(overlay).store.message(decision).content


def test_a_workplan_topic_outside_a_project_channel_is_not_acted_on(boards):
    _, overlay = boards
    client(overlay).send_to_channel("front", "workplan-growbox-lights", "@**autolab-agstudio1** decide the hours")
    assert deliver(overlay) == []


def test_replay_names_a_saved_run_the_doors_would_have_treated_differently(tmp_path):
    """`python -m agag.fixture doors`: a saved run whose question was answered
    in a new `workplan-` topic (the old responder) is changed; one answered
    in the task's own topic is not."""
    from agag.fixture.responder import post, replay

    script = PROBES["delegate-decision"].script
    for name, where in (("old", ("pj-growbox", "workplan-growbox-lighting-hours")), ("task", TASK)):
        out = tmp_path / name
        store = build_store(out / "board")
        overlay = make_overlay(store, out / "overlay", script, {n: i for i, n in NAMES.items()})
        post(overlay, "front", "front-desk-fixture-lights", DEV, "decide the hours")
        asked = post(overlay, *where, FRONT, "decide the hours for m20510")
        post(overlay, "front", "front-desk-fixture-lights", FRONT, "asked")
        post(overlay, *where, AUTOLAB, f"@**Front** 16 h or 12 h? Answers #{asked}.")
        [send] = replay(out, script)
        assert send["then"] == "answer"
        assert (send["now"], send["changed"]) == (("new", True) if name == "old" else ("answer", False))
