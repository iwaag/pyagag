"""`agag.acceptance`: a mission's acceptance is one evidenced record
(`robust_workflow` p3 step 3).

The realm is a two-task mission as autolab leaves it after its last
close-out: both tasks `completed` and ✔, the plan `started`, and the
requester's acceptance of the last task — "that completes this mission" —
sitting in the requester's own conversation (p2 C3, #9804). What is pinned:
the record is written once, with whose words it rests on; the trace reads the
mission `done`; a repeat and an interrupted attempt converge on the same
record; and every refusal writes nothing.
"""

from __future__ import annotations

import pytest

from agag.acceptance import AcceptanceRefused, accept_mission, parse_acceptance
from agag.mirror.testing import FakeRealm
from agag.selfnote import parse_note
from agag.trace import trace
from agag.zulip import RESOLVED_TOPIC_PREFIX, ZulipError

DEV, FRONT, AUTOLAB, OMNI = 8, 15, 11, 9
NAMES = {DEV: "Developer", FRONT: "Front", AUTOLAB: "autolab-agstudio1", OMNI: "Omni Agent"}


class Client:
    """The `ZulipClient` calls `accept_mission` and the trace make, over the
    fake realm, speaking as `me`."""

    def __init__(self, realm: FakeRealm, me: int, *, is_bot: bool = True):
        self.realm = realm
        self.me = me
        self.is_bot = is_bot
        self.fail_on = None  # a note text whose send raises, once

    def whoami(self, refresh=False):
        return {"user_id": self.me, "full_name": NAMES[self.me], "is_bot": self.is_bot}

    def _rows(self):
        return sorted(self.realm.messages.values(), key=lambda m: m["id"])

    def message(self, message_id, strict=False):
        found = self.realm.messages.get(int(message_id))
        return dict(found) if found else None

    def topic_history(self, channel, topic, num_before=50):
        return [dict(m) for m in self._rows()
                if m["display_recipient"] == channel and m["subject"] == topic][-num_before:]

    def public_notes(self, tag, num_before=1000):
        return [dict(m) for m in self._rows() if f"[selfnote][{tag}]" in m["content"]][-num_before:]

    def send_to_channel(self, channel, topic, content):
        if self.fail_on is not None and self.fail_on in content:
            self.fail_on = None
            raise ZulipError("connection dropped")
        return self.realm.post(channel, topic, content, sender_id=self.me, sender_name=NAMES[self.me])

    def resolve_topic(self, message_id, topic):
        if not topic.startswith(RESOLVED_TOPIC_PREFIX):
            self.realm.resolve(self.realm.messages[message_id]["display_recipient"], topic)


def post(realm, channel, topic, text, sender):
    return realm.post(channel, topic, text, sender_id=sender, sender_name=NAMES[sender])


@pytest.fixture
def world():
    realm = FakeRealm()
    realm.add_channel(3, "front")
    realm.add_channel(6, "pj-x")
    realm.add_channel(7, "work-m1")
    ask = post(realm, "front", "front-c", "Add --average, two tasks.", OMNI)
    post(realm, "pj-x", "workplan-average", f"[selfnote][rootchat] front/front-c #{ask} rel=work", FRONT)
    post(realm, "pj-x", "workplan-average", "@**autolab-agstudio1** Plan and start --average.", FRONT)
    mission = post(realm, "pj-x", "workplan-average", "[selfnote][mission] x", AUTOLAB)
    post(realm, "pj-x", "workplan-average", "[selfnote][state] started", AUTOLAB)
    for serial in (1, 2):
        topic = f"workrun-task{serial}-m{mission}"
        post(realm, "work-m1", topic, f"[selfnote][task] {mission}#{serial}", AUTOLAB)
        post(realm, "work-m1", topic, f"[selfnote][rootchat] pj-x/workplan-average #{mission} rel=work", AUTOLAB)
        post(realm, "work-m1", topic, f"# Task {serial}", AUTOLAB)
        post(realm, "work-m1", topic, f"@**Front** task {serial} done; please accept", AUTOLAB)
    accepted = post(realm, "front", "front-c", "Task 2 is accepted. That completes this mission from my side.", OMNI)
    for serial in (1, 2):
        topic = f"workrun-task{serial}-m{mission}"
        post(realm, "work-m1", topic, "[selfnote][state] completed", AUTOLAB)
        realm.resolve("work-m1", topic)
    return realm, mission, accepted


def notes(realm, channel, topic, tag):
    return [m["content"] for m in sorted(realm.messages.values(), key=lambda m: m["id"])
            if m["display_recipient"] == channel and m["subject"].endswith(topic) and parse_note(m["content"], tag)]


def test_the_requester_s_words_make_the_mission_done_once(world):
    realm, mission, accepted = world
    client = Client(realm, FRONT)
    done = accept_mission(client, mission, evidence=accepted)
    assert (done.evidence, done.by_id, done.by_name) == (accepted, OMNI, "Omni Agent")
    assert done.tasks == ["1", "2"] and done.resolved
    (note,) = notes(realm, "pj-x", "workplan-average", "acceptance")
    assert parse_acceptance(note) == (accepted, OMNI, "Omni Agent")
    root = trace(client, mission).root
    assert root.state == "done" and root.topic.startswith(RESOLVED_TOPIC_PREFIX)
    assert all(child.note_state == "accepted" for child in root.children)
    before = len(realm.messages)
    again = accept_mission(client, mission, evidence=accepted)
    assert again.already and len(realm.messages) == before, "a repeat wrote something"
    assert "already done" in again.summary()


def test_an_interrupted_acceptance_is_finished_by_the_next_attempt(world):
    realm, mission, accepted = world
    client = Client(realm, FRONT)
    client.fail_on = "[selfnote][state] done"
    with pytest.raises(ZulipError):
        accept_mission(client, mission, evidence=accepted)
    # The note that says whose decision it is landed; `done` did not.
    assert len(notes(realm, "pj-x", "workplan-average", "acceptance")) == 1
    # A second attempt — even one quoting another post — finishes the first
    # one's record rather than starting its own.
    other = post(realm, "front", "front-c", "Yes, the mission is accepted.", OMNI)
    done = accept_mission(client, mission, evidence=other)
    assert done.evidence == accepted and not done.tasks
    assert len(notes(realm, "pj-x", "workplan-average", "acceptance")) == 1
    assert len(notes(realm, "work-m1", f"workrun-task1-m{mission}", "state")) == 2  # completed, accepted — once
    assert trace(client, mission).root.state == "done"


def test_a_person_recording_their_own_decision_needs_no_post(world):
    """The operation room's completion door: the Developer presses it."""
    realm, mission, _ = world
    done = accept_mission(Client(realm, OMNI, is_bot=False), mission)
    assert (done.evidence, done.by_id) == (0, OMNI)
    assert trace(Client(realm, OMNI), mission).root.state == "done"


def test_a_person_who_does_not_hold_the_decision_cannot_record_it_in_person(world):
    realm, mission, _ = world
    with pytest.raises(AcceptanceRefused, match="Front or Omni Agent"):
        accept_mission(Client(realm, DEV, is_bot=False), mission)


@pytest.mark.parametrize("case", ["no evidence", "not a holder", "owner's post", "older than the mission",
                                  "unfinished task", "cancelled", "not a mission"])
def test_every_refusal_writes_nothing(world, case):
    realm, mission, accepted = world
    client = Client(realm, FRONT)
    target, evidence = mission, accepted
    if case == "no evidence":
        evidence = None
    elif case == "not a holder":
        evidence = post(realm, "front", "front-c", "Looks accepted to me.", DEV)
    elif case == "owner's post":
        evidence = post(realm, "pj-x", "workplan-average", "@**Front** every task is finished", AUTOLAB)
    elif case == "older than the mission":
        evidence = min(realm.messages)
    elif case == "unfinished task":
        post(realm, "work-m1", f"workrun-task3-m{mission}", f"[selfnote][task] {mission}#3", AUTOLAB)
        post(realm, "work-m1", f"workrun-task3-m{mission}", f"[selfnote][rootchat] pj-x/workplan-average #{mission} rel=work",
             AUTOLAB)
    elif case == "cancelled":
        post(realm, "pj-x", "workplan-average", "[selfnote][state] cancelled", AUTOLAB)
    elif case == "not a mission":
        target = accepted
    before = len(realm.messages)
    with pytest.raises(AcceptanceRefused):
        accept_mission(client, target, evidence=evidence)
    assert len(realm.messages) == before


def test_agentchat_accept_says_what_it_did_and_refuses_out_loud(world, monkeypatch, capsys):
    from agag import chat

    realm, mission, accepted = world
    client = Client(realm, FRONT)
    monkeypatch.setattr(chat, "client_from_environment", lambda: client)
    assert chat.main(["accept", str(mission)]) == 1
    assert "--evidence" in capsys.readouterr().err
    assert chat.main(["accept", str(mission), "--evidence", str(accepted)]) == 0
    assert f"m{mission} is done: accepted by Omni Agent (#{accepted})" in capsys.readouterr().out


def test_naming_the_accepting_post_instead_of_the_mission_is_refused_with_how_to_name_it(world):
    realm, mission, accepted = world
    with pytest.raises(AcceptanceRefused) as refused:
        accept_mission(Client(realm, FRONT), accepted, evidence=accepted)
    assert "Name the mission first" in str(refused.value) and f"--evidence {accepted}" in str(refused.value)


def test_front_s_own_agreement_gives_the_same_record_whoever_records_it(world):
    """failsafe p5 step 3 (step 1's reproduction, progress_panel p1 trial B
    #13798/#13804): the runner (Front) was entrusted with the acceptance and
    agreed. `agentchat accept` as Front refused that post; autolab's close-out
    recorded it. Now the holder decides, not the route: both record the same
    decision, and a repeat by the other writes nothing."""
    realm, mission, _ = world
    agreed = post(realm, "pj-x", "workplan-average", "Both tasks are in; that completes the mission.", FRONT)
    first = accept_mission(Client(realm, FRONT), mission, evidence=agreed, resolve=False)
    assert (first.evidence, first.by_id) == (agreed, FRONT)
    (note,) = notes(realm, "pj-x", "workplan-average", "acceptance")
    before = len(realm.messages)
    again = accept_mission(Client(realm, AUTOLAB), mission, evidence=agreed, resolve=False)
    assert again.already and len(realm.messages) == before
    assert (again.evidence, again.by_id) == (agreed, FRONT)


def test_autolab_recording_front_s_agreement_writes_what_front_would(world):
    realm, mission, _ = world
    agreed = post(realm, "pj-x", "workplan-average", "Both tasks are in; that completes the mission.", FRONT)
    done = accept_mission(Client(realm, AUTOLAB), mission, evidence=agreed, resolve=False)
    assert (done.evidence, done.by_id, done.by_name) == (agreed, FRONT, "Front")
