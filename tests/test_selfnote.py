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
    is_speech,
    is_system_notice,
    last_real_message,
    last_real_sender,
    note,
    own_rootchat,
    parse_conversation,
    parse_note,
    parse_rootchat,
    effective_rootchat,
    own_rootchat_moved,
    parse_replaces,
    parse_rootchat_moved,
    parse_served,
    replaced_anchor,
    replaces_note,
    rootchat_moved_note,
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


# --- Zulip's own notices are not speech either -----------------------------


def notice(text="@_**Developer|8** has marked this topic as unresolved.", id=1):
    """What Zulip's Notification Bot posts: a cross-realm system bot, not a
    realm member, recognisable only by its realm."""
    return {
        "id": id, "sender_id": 6, "sender_email": "notification-bot@zulip.com",
        "sender_full_name": "Notification Bot", "sender_realm_str": "zulipinternal",
        "content": text,
    }


def test_a_system_notice_is_recognised_by_its_realm_not_its_name():
    assert is_system_notice(notice())
    assert not is_speech(notice())
    # A realm member calling itself "Notification Bot" is still somebody speaking.
    impostor = {**message(13, "hi"), "sender_full_name": "Notification Bot", "sender_realm_str": "agdev"}
    assert not is_system_notice(impostor) and is_speech(impostor)
    # Messages without the field (older fixtures, DM payloads) are speech.
    assert is_speech(message(13, "hi"))


def test_an_unresolve_notice_does_not_hand_the_owner_a_turn():
    """`operation_room` p8: the Developer un-✔'d a run topic Front had
    answered; Zulip's "marked as unresolved" line was read as the Developer
    speaking again and Front bought a run to say nothing was new. The last
    real speaker in that topic is Front, and the topic awaits nobody."""
    history = [
        message(8, "Routine `ghtrends`, run of 2026-09-07T15:14Z", id=1),
        message(15, "Message received. Please wait for the reply.", id=2),
        message(15, "@**Developer** Verification-only run confirmed.", id=3),
        notice("@_**Developer|8** has marked this topic as resolved.", id=4),
        notice("@_**Developer|8** has marked this topic as unresolved.", id=5),
    ]
    assert last_real_sender(history) == 15
    assert last_real_message(history)["id"] == 3
    assert [m["id"] for m in without_selfnotes(history)] == [1, 2, 3]


def test_a_topic_holding_only_notices_awaits_nobody():
    assert last_real_sender([notice(id=1)]) is None
    assert last_real_message([notice(id=1)]) is None


# --- the replacement relation (routine_tests p2 ex1) ------------------------


def test_a_replaces_note_names_an_anchor_by_id():
    assert replaces_note(6371) == "[selfnote][replaces] 6371"
    assert parse_replaces(replaces_note(6371)) == 6371


@pytest.mark.parametrize(
    "content",
    [
        "[selfnote][replaces]",
        "[selfnote][replaces] workplan-contributions",
        "[selfnote][replaces] 63 71",
        "[selfnote][rootchat] front/front-a",
        "replaces 6371",
        "",
    ],
)
def test_anything_else_is_not_a_replaces_note(content):
    assert parse_replaces(content) is None


def test_the_relation_is_read_whoever_wrote_it():
    """It is written by the agent that opens the replacement — autolab — and
    read by every third party that was anchored in the conversation whose
    name the replacement took. Filtering it to the reader's own id would
    leave exactly the agent it exists for unable to read it."""
    history = [message(11, replaces_note(6371), id=6401)]
    assert replaced_anchor(history) == 6371


def test_the_earliest_valid_relation_wins():
    """Identity-shaped: a conversation replaces one thing, decided when it
    was opened. A later note is a repeat, not a redirection."""
    history = [
        message(11, "opening the replacement", id=1),
        message(11, replaces_note(6371), id=2),
        message(11, replaces_note(9999), id=3),
    ]
    assert replaced_anchor(history) == 6371


def test_a_conversation_with_no_relation_has_none():
    assert replaced_anchor([message(11, "hello")]) is None
    assert replaced_anchor([]) is None


# --- the effective anchor (routine_tests p2 ex1, B2) ------------------------
#
# "A topic is anchored once" is right and is kept. What was missing was any
# way to say a topic was anchored *wrongly*: p2's manual repair wrote a second
# ordinary root note into the delegation topic (message 6500) and the
# completion callback still went to the Front Desk, because a repeat loses —
# correctly. p1 wanted the same capability four times.


DESK = Conversation("front", "front-desk-20260912-1636")
RUN = Conversation("routine-publish", "routinerun-20260912T1636Z")
OTHER_ID = 11


def test_a_move_note_names_a_conversation():
    assert rootchat_moved_note(RUN) == f"[selfnote][rootchat-moved] {RUN}"
    assert parse_rootchat_moved(rootchat_moved_note(RUN)) == RUN
    assert is_selfnote(rootchat_moved_note(RUN))


@pytest.mark.parametrize(
    "content",
    [
        "[selfnote][rootchat-moved]",
        "[selfnote][rootchat-moved] no-slash-here",
        "[selfnote][rootchat-moved] /topic",
        "[selfnote][rootchat] front/front-a",
        "rootchat-moved routine-publish/routinerun-1",
    ],
)
def test_anything_else_is_not_a_move(content):
    assert parse_rootchat_moved(content) is None


def test_the_whole_sequence_p2_would_have_needed():
    """Desk anchor, ordinary run anchor repeat, explicit run correction,
    another ordinary repeat. Only the explicit correction moves the home."""
    desk = message(15, rootchat_note(DESK), id=6482)
    repeat = message(15, rootchat_note(RUN), id=6500)
    correction = message(15, rootchat_moved_note(RUN), id=6520)
    later = message(15, rootchat_note(Conversation("front", "front-other")), id=6540)

    assert effective_rootchat([desk], 15) == DESK
    assert effective_rootchat([desk, repeat], 15) == DESK  # a repeat loses
    assert effective_rootchat([desk, repeat, correction], 15) == RUN
    assert effective_rootchat([desk, repeat, correction, later], 15) == RUN


def test_a_second_correction_supersedes_the_first():
    """A correction is not identity: an agent that got it wrong twice must be
    able to say so twice, and the last thing it said is what it means."""
    history = [
        message(15, rootchat_note(DESK), id=1),
        message(15, rootchat_moved_note(RUN), id=2),
        message(15, rootchat_moved_note(Conversation("front", "front-final")), id=3),
    ]
    assert effective_rootchat(history, 15) == Conversation("front", "front-final")


def test_a_move_written_by_somebody_else_moves_nothing_of_ours():
    """The one place the sender filter is the whole point — unlike the
    `replaces` relation, which exists precisely to be read by others."""
    history = [
        message(15, rootchat_note(DESK), id=1),
        message(OTHER_ID, rootchat_moved_note(RUN), id=2),
    ]
    assert effective_rootchat(history, 15) == DESK
    assert own_rootchat_moved(history, 15) is None


def test_a_malformed_move_is_not_a_move():
    history = [
        message(15, rootchat_note(DESK), id=1),
        message(15, "[selfnote][rootchat-moved] nonsense", id=2),
    ]
    assert effective_rootchat(history, 15) == DESK


def test_a_correction_alone_anchors_a_topic_that_had_no_note():
    assert effective_rootchat([message(15, rootchat_moved_note(RUN), id=1)], 15) == RUN


def test_a_move_is_hidden_from_the_conversation_like_every_other_note():
    """It is a correction between an agent and itself. Nobody is served by
    it, and nobody reads it."""
    history = [
        message(8, "please publish the study", id=1),
        message(15, rootchat_moved_note(RUN), id=2),
    ]
    assert [m["id"] for m in without_selfnotes(history)] == [1]
    assert last_real_sender(history) == 8
