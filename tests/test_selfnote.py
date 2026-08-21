"""The `[selfnote]` convention: what it parses to, and what it must never do.

The parsing half is a one-line format and its tests read like one. The half
that matters is the last one: **a selfnote must never buy anybody a run.**
That is the p7 ack loop in a new coat — a bot's own bookkeeping line counted
as somebody speaking in a topic, which serves the topic's owner, whose reply
serves the bot back. `last_real_sender` is the single answer every
"who spoke last" check in the listeners goes through, so it is pinned here
and again, live against the sweep, in `test_zulip.py`.
"""

import pytest

from agag.selfnote import (
    Conversation,
    home_from_environment,
    is_selfnote,
    last_real_message,
    last_real_sender,
    note,
    own_rootchat,
    parse_conversation,
    parse_note,
    parse_rootchat,
    parse_served,
    rootchat_note,
    served_note,
    without_selfnotes,
)

HOME = Conversation("front", "front-title-image")


def message(sender_id=15, content="hello", id=1):
    return {"id": id, "sender_id": sender_id, "content": content}


# --- the format ------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    ["[selfnote][rootchat] front/front-x", "  [selfnote][work] p/i", "[selfnote]"],
)
def test_a_selfnote_is_recognized_by_its_first_word(content):
    assert is_selfnote(content)


@pytest.mark.parametrize(
    "content",
    ["hello", "", None, "I wrote a [selfnote] earlier", "@**Forge** [selfnote] no"],
)
def test_anything_else_is_conversation(content):
    """The marker leads, or it is somebody talking about notes."""
    assert not is_selfnote(content)


def test_a_note_is_its_tag_and_its_value():
    assert note("rootchat", "front/front-x") == "[selfnote][rootchat] front/front-x"
    assert parse_note("[selfnote][rootchat] front/front-x", "rootchat") == "front/front-x"


@pytest.mark.parametrize(
    "content",
    ["[selfnote][work] abc", "[selfnote] front/front-x", "front/front-x", "[selfnote][rootchat]"],
)
def test_a_note_of_another_kind_is_not_this_one(content):
    assert parse_note(content, "rootchat") is None


def test_the_root_note_round_trips():
    assert parse_rootchat(rootchat_note(HOME)) == HOME


def test_the_served_note_round_trips():
    remote = Conversation("agforge-agstudio1", "assetplan-x")
    assert served_note(remote, 913) == (
        "[selfnote][served] agforge-agstudio1/assetplan-x 913"
    )
    assert parse_served(served_note(remote, 913)) == (remote, 913)


def test_a_served_topic_may_hold_a_slash_and_the_id_is_the_last_word():
    remote = Conversation("pj-x", "workrun-a/b")
    assert parse_served(served_note(remote, 7)) == (remote, 7)


@pytest.mark.parametrize(
    "content",
    [
        "[selfnote][served] agforge-x/assetplan-a",     # no id
        "[selfnote][served] agforge-x/assetplan-a xyz",  # not a number
        "[selfnote][rootchat] agforge-x/assetplan-a",    # another kind of note
        "served agforge-x/assetplan-a 3",                # not a note at all
    ],
)
def test_anything_that_is_not_a_served_note_parses_to_none(content):
    assert parse_served(content) is None


def test_a_topic_may_hold_a_slash():
    assert parse_conversation("pj-x/workplan-a/b") == Conversation("pj-x", "workplan-a/b")


@pytest.mark.parametrize("value", ["", None, "no-slash", "/topic", "channel/"])
def test_half_a_conversation_is_none(value):
    assert parse_conversation(value) is None


def test_home_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("AGENTCHAT_HOME", "front/front-title-image")
    assert home_from_environment() == HOME
    monkeypatch.delenv("AGENTCHAT_HOME")
    assert home_from_environment() is None


# --- reading a topic's anchor ---------------------------------------------


def test_own_rootchat_finds_this_bots_note():
    history = [
        message(13, "[selfnote][rootchat] agforge-agstudio1/assetplan-x", id=1),
        message(15, rootchat_note(HOME), id=2),
        message(13, "what size?", id=3),
    ]
    assert own_rootchat(history, 15) == HOME
    assert own_rootchat(history, 13) == Conversation(
        "agforge-agstudio1", "assetplan-x"
    )
    assert own_rootchat(history, 8) is None


def test_the_earliest_note_anchors_the_topic():
    """A topic is opened once; a later note is a repeat, not a move."""
    history = [
        message(15, rootchat_note(HOME), id=1),
        message(15, rootchat_note(Conversation("front", "front-other")), id=2),
    ]
    assert own_rootchat(history, 15) == HOME


# --- the crux --------------------------------------------------------------


def test_a_selfnote_is_not_somebody_speaking():
    """The whole reason this module exists.

    Front anchors forge's topic and asks a question; forge answers. Whoever
    reads "who spoke last" must say forge — not Front, whose newest line is
    a note it wrote to itself, and not the note's author either.
    """
    history = [
        message(15, rootchat_note(HOME), id=1),
        message(15, "please draw a title image", id=2),
        message(13, "@**Front** what size?", id=3),
    ]
    assert last_real_sender(history) == 13
    assert last_real_message(history)["id"] == 3


def test_a_topic_holding_only_notes_awaits_nobody():
    """Anchoring a topic must not, by itself, hand anybody a turn."""
    assert last_real_sender([message(15, rootchat_note(HOME), id=1)]) is None
    assert last_real_message([message(15, rootchat_note(HOME), id=1)]) is None
    assert last_real_sender([]) is None


def test_the_note_of_another_agent_is_skipped_too():
    """Forge's own note in its own topic is not Front taking a turn either."""
    history = [
        message(15, "please draw a title image", id=1),
        message(13, "[selfnote][rootchat] agforge-agstudio1/assetplan-x", id=2),
    ]
    assert last_real_sender(history) == 15


def test_without_selfnotes_leaves_the_conversation():
    history = [message(15, rootchat_note(HOME), id=1), message(13, "hi", id=2)]
    assert [m["id"] for m in without_selfnotes(history)] == [2]
