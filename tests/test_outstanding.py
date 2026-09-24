"""Outstanding response requests (`clearer_chat_ui` step 2, `agag.outstanding`).

The recorded desk conversation (`fixtures/outstanding/desk.json`) walks
every transition once: two questions pending at the same time, a
third-party interruption, progress and acks that settle nothing, an
unreferenced reply that settles neither of two, an explicit reference, a
Zulip quote-and-reply, a question overtaken by input that arrived during
the run, and a withdrawal. The same history read through a mirror, a
reopened mirror and a mirror rebuilt from nothing gives the same answer.
The inline cases pin what the fixture does not: supersession, closure and
reopening, incomplete and stale histories, edits and deletions, and the
trace states built on the model.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from agag import trace as tracing
from agag.agent import is_ack
from agag.mirror import Mirror
from agag.mirror.testing import FakeRealm
from agag.outstanding import (
    ANSWERED, CLOSED, OVERTAKEN, PENDING, SUPERSEDED, WITHDRAWN, read_requests,
)
from agag.post import PostMeta, compose

DESK = json.loads((Path(__file__).parent / "fixtures" / "outstanding" / "desk.json").read_text("utf-8"))
DEV, FRONT, AUTOLAB, REVIEWER = 8, 15, 11, 30


def upto(last: int) -> list[dict]:
    return [dict(m, timestamp=1_790_000_000 + m["id"]) for m in DESK["messages"] if m["id"] <= last]


def states(result) -> dict[str, str]:
    return {str(r.id): r.state for r in result.requests}


# --- the recorded conversation, step by step ----------------------------------------------


@pytest.mark.parametrize("last", sorted(DESK["states_after"], key=int))
def test_each_transition_of_the_recorded_desk(last):
    result = read_requests(upto(int(last)), is_ack=is_ack)
    expected = DESK["states_after"][last]
    assert {k: v for k, v in states(result).items() if k in expected} == expected


def test_two_outstanding_questions_are_not_both_settled_by_one_unreferenced_reply():
    result = read_requests(upto(107), is_ack=is_ack)
    assert [r.id for r in result.pending] == [103, 104]
    assert result.unmatched == {107: [103, 104]}, "the reply is kept so a room can ask which one it answered"


def test_a_third_party_progress_and_acks_settle_nothing():
    before = read_requests(upto(104), is_ack=is_ack)
    after = read_requests(upto(106), is_ack=is_ack)
    assert states(before) == states(after) == {"103": PENDING, "104": PENDING}
    assert after.unmatched == {}, "autolab speaking is not the recipient speaking"


def test_the_explicit_reference_and_the_quote_say_how_they_answered():
    result = read_requests(upto(119), is_ack=is_ack).by_id()
    assert (result[104].state, result[104].settled_by, result[104].how) == (ANSWERED, 108, "reference")
    assert (result[103].state, result[103].settled_by, result[103].how) == (ANSWERED, 111, "quote")


def test_an_answered_question_keeps_what_it_was():
    request = read_requests(upto(119), is_ack=is_ack).by_id()[103]
    assert request.ask == "question" and request.to == DEV and request.to_name == "Developer"
    assert request.text.endswith("Which auth provider should the login use?") and "ag-post" not in request.text


def test_a_question_written_before_reading_newer_input_is_overtaken_until_the_asker_speaks_again():
    overtaken = read_requests(upto(115), is_ack=is_ack).by_id()[115]
    assert overtaken.state == OVERTAKEN and overtaken.overtaken_by == [114]
    assert read_requests(upto(115), is_ack=is_ack).pending == [], "nobody is told to reply while their input is owed"
    # The next serving's ack is transport, not the asker's word.
    assert read_requests(upto(116), is_ack=is_ack).by_id()[115].state == OVERTAKEN
    # The serving that read #114 answered and did not withdraw the question: it stands.
    assert read_requests(upto(117), is_ack=is_ack).by_id()[115].state == PENDING


def test_the_asker_withdraws_its_own_request_by_reference():
    request = read_requests(upto(119), is_ack=is_ack).by_id()[115]
    assert (request.state, request.settled_by) == (WITHDRAWN, 119)
    assert [r.id for r in read_requests(upto(119), is_ack=is_ack).pending] == [118]


# --- restart and reconstruction -------------------------------------------------------------


def realm_holding(messages) -> FakeRealm:
    realm = FakeRealm()
    realm.add_channel(24, DESK["channel"])
    for message in messages:
        realm.next_message_id = message["id"]
        realm.post(DESK["channel"], DESK["topic"], message["content"], sender_id=message["sender_id"],
                   sender_name=message["sender_full_name"], quiet=True)
    return realm


def open_mirror(realm, root, *, start=True):
    mirror = Mirror.open(root / "zulip.env", root / "mirror", client_factory=lambda: realm,
                         log=lambda line: None, start=start, resync_backoff=0.05)
    deadline = time.time() + 6
    while start and not mirror.live and time.time() < deadline:
        time.sleep(0.02)
    return mirror


def through(mirror) -> dict:
    history = mirror.history(DESK["channel"], DESK["topic"], num_before=1000)
    return read_requests(history, is_ack=is_ack).as_dict()


def test_the_same_recorded_history_gives_the_same_answer_after_a_restart_and_a_rebuild(tmp_path):
    realm = realm_holding(DESK["messages"])
    direct = read_requests([dict(m, timestamp=1000 + m["id"]) for m in DESK["messages"]], is_ack=is_ack).as_dict()
    first = open_mirror(realm, tmp_path)
    answer = through(first)
    first.stop()
    assert answer == direct
    reopened = open_mirror(realm, tmp_path, start=False)
    assert through(reopened) == answer, "a restart reads the persisted copy and concludes the same"
    reopened.close()
    shutil.rmtree(tmp_path / "mirror")
    rebuilt = open_mirror(realm, tmp_path)
    assert through(rebuilt) == answer, "a mirror rebuilt from nothing concludes the same"
    rebuilt.stop()
    assert answer["pending"] == [118]


# --- what the fixture does not show -----------------------------------------------------------


def post(mid, sender, name, text, meta=None, **extra):
    return {"id": mid, "sender_id": sender, "sender_full_name": name, "content": compose(text, meta), **extra}


def ask(mid, text="Which one?", *, to=DEV, seen=None, re=()):
    return post(mid, FRONT, "Front", text, PostMeta(intent="response_request", to=to, ask="question",
                                                     seen=seen, re=tuple(re)))


def dev(mid, text="B", meta=None):
    return post(mid, DEV, "Developer", text, meta)


def test_a_newer_request_that_references_the_old_one_supersedes_it():
    result = read_requests([dev(1, "go"), ask(2, "Blue or green?"), ask(3, "Blue, green or red?", re=(2,))])
    assert states(result) == {"2": SUPERSEDED, "3": PENDING}
    assert result.by_id()[2].settled_by == 3


def test_a_single_pending_request_is_answered_by_the_next_post():
    result = read_requests([dev(1, "go"), ask(2), dev(3, "blue")])
    request = result.by_id()[2]
    assert (request.state, request.how, request.settled_by, request.certain) == (ANSWERED, "next_post", 3, True)


def test_the_recipients_progress_is_not_an_answer():
    result = read_requests([ask(1, to=AUTOLAB), post(2, AUTOLAB, "autolab", "working", PostMeta(intent="progress")),
                            post(3, AUTOLAB, "autolab", "🔧 Bash: make")])
    assert states(result) == {"1": PENDING}


def test_a_reference_by_somebody_else_does_not_answer_for_the_recipient():
    result = read_requests([ask(1), post(2, REVIEWER, "Reviewer", "I'd say B", PostMeta(re=(1,)))])
    assert states(result) == {"1": PENDING}


def test_a_reference_that_names_nothing_pending_falls_back_to_nothing():
    # One pending question, and the Developer references an unrelated post:
    # the explicit reference is about something else, so it settles nothing
    # rather than being guessed into an answer.
    result = read_requests([ask(1), dev(2, "about #999", PostMeta(re=(999,)))])
    assert states(result) == {"1": PENDING}


def test_closing_the_conversation_closes_what_is_still_pending_and_reopening_restores_it():
    history = [ask(1), ask(2, "Also this?"), dev(3, "yes", PostMeta(re=(2,)))]
    closed = read_requests(history, closed=True)
    assert states(closed) == {"1": CLOSED, "2": ANSWERED} and closed.closed and closed.pending == []
    assert states(read_requests(history)) == {"1": PENDING, "2": ANSWERED}


def test_an_incomplete_history_makes_a_next_post_answer_uncertain_and_says_so():
    result = read_requests([ask(5), dev(6)], complete=False)
    assert result.by_id()[5].certain is False and result.by_id()[5].state == ANSWERED
    assert any("incomplete" in why for why in result.uncertain)


def test_a_stale_source_is_said_once():
    result = read_requests([ask(5)], stale=True)
    assert states(result) == {"5": PENDING} and any("stale" in why for why in result.uncertain)


def test_an_edit_is_read_as_the_post_is_now():
    edited_away = [dict(ask(1), content="Which one? (never mind)", last_edit_timestamp=5)]
    assert read_requests(edited_away).requests == []
    edited_in = [ask(1), dict(dev(2, "B"), content=compose("B", PostMeta(re=(1,))), last_edit_timestamp=9)]
    assert read_requests(edited_in).by_id()[1].how == "reference"
    assert read_requests([dict(ask(1), last_edit_timestamp=3)]).by_id()[1].edited


def test_a_deleted_answer_returns_its_request_to_pending():
    history = [ask(1), dev(2)]
    assert states(read_requests(history)) == {"1": ANSWERED}
    assert states(read_requests(history[:1])) == {"1": PENDING}


def test_an_unclassified_question_is_never_a_request():
    assert read_requests([post(1, FRONT, "Front", "@**Developer** which one?")]).requests == []


def test_a_request_to_oneself_is_not_a_request():
    assert read_requests([ask(1, to=FRONT)]).requests == []


# --- trace reads the explicit model -----------------------------------------------------------

ACK = "Message received. Please wait for the reply."


@pytest.mark.parametrize("history,state", [
    ([dev(1, "build it"), post(2, FRONT, "Front", ACK), ask(3, seen=2)], "awaiting_human"),
    ([dev(1, "build it"), post(2, FRONT, "Front", ACK), post(3, FRONT, "Front", "Built.", PostMeta(intent="report"))],
     "answered"),
    ([dev(1, "build it"), post(2, FRONT, "Front", ACK), post(3, FRONT, "Front", "Built, no label.")], "answered"),
    ([dev(1, "build it"), post(2, FRONT, "Front", ACK), dev(3, "and fast"), ask(4, seen=2)], "queued"),
])
def test_trace_distinguishes_a_confirmed_question_from_an_answer(history, state):
    stamped = [dict(m, timestamp=1_790_000_000 + m["id"]) for m in history]
    found = tracing.classify(stamped, human=True, now=1_790_000_100)
    assert found[0] == state
    if state == "awaiting_human":
        assert found[4] == [3] and "asks Developer (question)" in found[1]
