"""agent_guide p2 ex1 step 3: the fixture answers a delegation, on an overlay."""

from __future__ import annotations

import hashlib
import io

import pytest

from agag import chat
from agag.fixture import PROBES, build_store, judge
from agag.fixture.board import AUTOLAB, FRONT, NAMES
from agag.fixture.responder import Canned, make_overlay, posts_since
from agag.fixture.run import client
from agag.mirror.reads import FixtureRefused

SCRIPT = (Canned("autolab-agstudio1", "{asker} Twenty seconds. Answers #{ask}.", topics=("workplan-",)),
          Canned("autolab-agstudio1", "{asker} Done. Answers #{ask}."))


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
    sent = client(overlay).send_to_channel("pj-growbox", "workplan-growbox-pump", "how long does the pump run?")
    after = posts_since(overlay, newest)
    assert [m["id"] for m in after] == [sent, sent + 1]
    assert after[1]["sender_id"] == AUTOLAB and after[1]["content"] == f"@**Front** Twenty seconds. Answers #{sent}."
    assert _digest(store) == before
    with pytest.raises(FixtureRefused):
        client(store).send_to_channel("pj-growbox", "workplan-growbox-pump", "x")


def test_only_a_post_addressing_the_agent_takes_its_next_line(boards):
    _, overlay = boards
    board = client(overlay)
    newest = board.store.newest_id()
    board.send_to_channel("pj-aisvgs", "notes", "nobody is asked here")
    board.send_to_channel("pj-aisvgs", "notes", "[selfnote][rootchat] front/x #1 rel=work")
    assert [m["sender_id"] for m in posts_since(overlay, newest)] == [FRONT, FRONT]
    board.send_to_channel("autolab-agstudio1", "q", "a question in autolab's own channel")
    board.send_to_channel("pj-aisvgs", "notes", "@**autolab-agstudio1** and you?")
    senders = [m["sender_id"] for m in posts_since(overlay, newest)]
    assert senders == [FRONT, FRONT, FRONT, AUTOLAB, FRONT, AUTOLAB]
    board.send_to_channel("autolab-agstudio1", "q", "the script is spent")
    assert posts_since(overlay, newest)[-1]["sender_id"] == FRONT


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
    assert chat.main(["send", "pj-growbox", "workplan-growbox-pump", "--intent", "response_request", "--to",
                      "autolab-agstudio1", "--ask", "question", "the pump?"], out=out, err=io.StringIO()) == 0
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
