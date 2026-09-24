"""An explicit non-answer (`clearer_chat_ui` ex1 step 2).

The Front Desk's "not an answer" cleared the picked request and sent the post
with no reference, and `read_requests` read an unreferenced post by the
recipient as the answer to their one pending request: the aside cleared the
wait. A post now has three correlation choices — none given (the next-post
rule), `re=` (answers exactly those) and `answer=none` (answers nothing) —
and the last is carried in the post itself, so a redelivery, a restart and
a mirror rebuilt from nothing read the same thing.
"""

from __future__ import annotations

import io
import shutil

import pytest

from agag import serving, topics
from agag.agent import is_ack
from agag.chat import AgentChatError, _run, format_messages, send_meta
from agag.outstanding import ANSWERED, OVERTAKEN, PENDING, read_requests
from agag.post import NONE, REPORT, PostMeta, compose, describe, label, parse_attributes, parse_post

from test_outstanding import ask, dev, open_mirror
from test_post import Users, parsed_send
from test_serving_lifecycle import ScriptedClient, human

ASIDE = PostMeta(answer=NONE)


# --- the wire line ----------------------------------------------------------------------


def test_the_non_answer_round_trips_alone_and_with_an_intent():
    for meta in (ASIDE, PostMeta(intent=REPORT, answer=NONE)):
        content = compose("By the way, the build is green.", meta)
        assert parse_post(content).meta == meta
    assert compose("aside", ASIDE).endswith("`ag-post answer=none`")


@pytest.mark.parametrize("words,why", [
    ("answer=none re=5", "contradict"),
    ("answer=yes", "unknown answer"),
    ("answer=5", "unknown answer"),
])
def test_contradicting_or_unknown_choices_are_malformed(words, why):
    _, problem = parse_attributes(words)
    assert problem is not None and why in problem
    parsed = parse_post(f"text\n\n`ag-post {words}`")
    assert parsed.meta is None and parsed.text == "text", "removed from the text, read as unclassified"
    with pytest.raises(ValueError):
        compose("x", PostMeta(re=(5,), answer=NONE))


def test_readers_are_told_what_it_means():
    assert describe(ASIDE) == "not an answer to any request"
    text, prefix = label(compose("aside", ASIDE))
    assert (text, prefix) == ("aside", "(not an answer to any request) ")


# --- what is still asked ---------------------------------------------------------------------


def aside(mid, text="unrelated: the staging box is back"):
    return dev(mid, text, ASIDE)


def test_a_non_answer_leaves_the_one_pending_request_open():
    result = read_requests([dev(1, "go"), ask(2), aside(3)])
    assert result.by_id()[2].state == PENDING and result.unmatched == {}


def test_after_a_non_answer_the_next_plain_post_still_answers_and_a_reference_answers_explicitly():
    plain = read_requests([dev(1, "go"), ask(2), aside(3), dev(4, "blue")]).by_id()[2]
    assert (plain.state, plain.how, plain.settled_by) == (ANSWERED, "next_post", 4)
    named = read_requests([dev(1, "go"), ask(2), aside(3), dev(4, "blue", PostMeta(re=(2,)))]).by_id()[2]
    assert (named.state, named.how, named.settled_by) == (ANSWERED, "reference", 4)


def test_with_two_pending_a_non_answer_is_not_an_unmatched_reply():
    result = read_requests([dev(1, "go"), ask(2), ask(3, "And the font?"), aside(4)])
    assert [r.state for r in result.requests] == [PENDING, PENDING]
    assert result.unmatched == {}
    result = read_requests([dev(1, "go"), ask(2), ask(3, "And the font?"), dev(4, "B")])
    assert result.unmatched == {4: [2, 3]}, "the ordinary rule is unchanged"


def test_a_non_answer_that_quotes_the_request_still_answers_nothing():
    quoted = "[said](https://zulip.example/#narrow/channel/1-front/topic/t/near/2):\n```quote\nWhich one?\n```\nnoted"
    result = read_requests([dev(1, "go"), ask(2), dev(3, quoted, ASIDE)])
    assert result.by_id()[2].state == PENDING


def test_a_non_answer_is_still_input_the_asker_had_not_read():
    result = read_requests([dev(1, "go"), aside(2), ask(3, seen=1)])
    assert result.by_id()[3].state == OVERTAKEN


def test_the_same_history_gives_the_same_answer_through_a_mirror_restart_and_rebuild(tmp_path):
    from agag.mirror.testing import FakeRealm

    history = [dev(1, "go"), ask(2), aside(3)]
    realm = FakeRealm()
    realm.add_channel(24, "front")
    for message in history:
        realm.next_message_id = message["id"]
        realm.post("front", "desk", message["content"], sender_id=message["sender_id"],
                   sender_name=message["sender_full_name"], quiet=True)

    def through(mirror):
        return read_requests(mirror.history("front", "desk", num_before=100), is_ack=is_ack).as_dict()

    first = open_mirror(realm, tmp_path)
    answer = through(first)
    first.stop()
    assert answer["pending"] == [2]
    reopened = open_mirror(realm, tmp_path, start=False)
    assert through(reopened) == answer
    reopened.close()
    shutil.rmtree(tmp_path / "mirror")
    rebuilt = open_mirror(realm, tmp_path)
    assert through(rebuilt) == answer
    rebuilt.stop()


# --- producing it ------------------------------------------------------------------------------


def test_agentchat_send_marks_a_non_answer_and_refuses_it_with_a_reference():
    assert send_meta(Users(), parsed_send("--not-answer", "aside")) == ASIDE
    assert send_meta(Users(), parsed_send("--intent", "report", "--not-answer", "x")) == \
        PostMeta(intent=REPORT, answer=NONE)
    with pytest.raises(AgentChatError) as caught:
        send_meta(Users(), parsed_send("--not-answer", "--re", "5", "x"))
    assert "contradict" in str(caught.value)


def test_agentchat_send_posts_the_choice_in_the_same_message(monkeypatch):
    from agag import chat

    sent = []

    class Client(Users):
        def send_to_channel(self, channel, topic, content):
            sent.append(content)
            return 77

    monkeypatch.setattr(chat, "refuse_resolved", lambda *a: None)
    monkeypatch.setattr(chat, "join_and_record", lambda *a: False)
    monkeypatch.setattr(chat, "ensure_rootchat", lambda *a: None)
    _run(parsed_send("--not-answer", "the staging box is back"), Client(), io.StringIO())
    assert sent == ["the staging box is back\n\n`ag-post answer=none`"]


def test_read_shows_the_meaning_not_the_line():
    out = format_messages([{**dev(5, "aside", ASIDE), "timestamp": 1_790_000_000}])
    assert "not an answer to any request" in out and "ag-post" not in out


def test_a_reply_declaring_a_non_answer_keeps_it_through_a_dropped_send_and_redelivery():
    client = ScriptedClient([human("build it", 1)],
                            trouble=lambda c: "error" if "ag-post" in c else None)
    journal = serving.NullJournal()
    with pytest.raises(Exception):
        topics.serve_topic(client, "c", "t",
                           lambda ctx: topics.TopicResult(output="```ag-reply intent=report answer=none\nNoted.\n```"),
                           ack_text="ack", journal=journal, log=lambda t: None, delivery={"sleep": lambda s: None})
    prepared = journal.serving().reply_text
    assert parse_post(prepared).meta == PostMeta(intent=REPORT, answer=NONE)
    client.trouble = lambda c: None
    topics.resume_prepared(client, journal.serving(), journal, log=lambda t: None)
    assert client.sent.count(prepared) == 1
    assert parse_post(client.sent[-1]).meta.not_answer
