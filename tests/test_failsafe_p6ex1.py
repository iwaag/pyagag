"""failsafe p6 ex1: explicit work relations and shared dispositions.

Step 2 — a root note says what its conversation is to its home: work
(delegation, adoption) or a reference (a citation, a comment). The return
address is the same either way; only work hangs the conversation, its waits
and its acceptance under the home. Nothing is decided by how much history a
reader happened to get.
"""

from __future__ import annotations

import io

import pytest

from agag import acceptance, chat, receipt as receipts, relations, selfnote
from agag import trace as tracing
from agag import zulip
from agag.selfnote import Conversation, rootchat_note

from study_realm import ACK, AUTOLAB, DEV, FRONT, OMNI, Realm
from test_failsafe_p6 import Mission, closed


def topics(result) -> set[str]:
    return {n.topic for n in result.nodes()}


def cite(realm: Realm, m: Mission, desk: Mission, relation: str | None = "reference") -> int:
    """Front, serving `desk`, comments in `m`'s request: its root note, then its post."""
    home = Conversation("front", desk.desk, desk.origin)
    note = realm.post("front", m.desk, rootchat_note(home, relation), FRONT)
    realm.post("front", m.desk, "For the record: task 1's report was taken up here.", FRONT)
    return note


def pad(realm: Realm, channel: str, topic: str, total: int, sender: int = OMNI) -> None:
    have = len(realm.topic_history(channel, topic, num_before=10 ** 6))
    for i in range(max(0, total - have)):
        realm.post(channel, topic, f"chatter {i}", sender)


# --- the relation is recorded, never guessed from a history length ------------------------------


@pytest.mark.parametrize("total", [199, 200, 201, 450])
def test_a_citation_adopts_nothing_below_at_or_above_the_trace_s_history(total):
    realm, m = closed()
    m.mission_accepted()
    desk = Mission(realm, "desk")
    pad(realm, "front", m.desk, total - 2)
    cite(realm, m, desk)
    assert len(realm.topic_history("front", m.desk, num_before=10 ** 6)) >= total
    result = tracing.trace(realm, desk.origin, now=realm.clock + 60)
    assert m.desk not in topics(result) and m.task_topic not in topics(result)
    root = result.root
    assert [r["relation"] for r in root.relations] == ["reference"]
    assert tracing.trace(realm, m.origin, now=realm.clock + 60).root.children, "its own request still has its work"


@pytest.mark.parametrize("total", [199, 200, 201])
def test_a_legacy_note_nothing_classifies_is_unknown_and_says_how_to_resolve_it(total):
    realm, m = closed()
    desk = Mission(realm, "desk")
    pad(realm, "front", m.desk, total - 2)
    cite(realm, m, desk, relation=None)  # written before notes said it
    result = tracing.trace(realm, desk.origin, now=realm.clock + 60)
    assert m.desk not in topics(result), "missing evidence makes no ownership edge"
    assert [r["relation"] for r in result.unknown_relations] == ["unknown"]
    assert "agentchat relation front" in result.unknown_relations[0]["resolve"]
    assert any("relation unknown" in line for line in tracing.trace_lines(result))


def test_a_truncated_read_neither_adopts_a_citation_nor_drops_a_real_task():
    realm, m = closed()
    m.mission_accepted()
    desk = Mission(realm, "desk")
    for i in range(50):
        realm.post("front", m.desk, f"front relay {i}", FRONT)  # the window begins with the citer's own posts
    cite(realm, m, desk)
    pad(realm, m.channel, "✔ " + m.task_topic, 0)
    for i in range(60):
        realm.post(m.channel, m.task_topic, f"chatter {i}", OMNI)  # the task's own notes fall outside the window
    original = realm.topic_history
    realm.topic_history = lambda c, t, num_before=50: original(c, t, num_before=min(num_before, 40))
    assert m.desk not in topics(tracing.trace(realm, desk.origin, now=realm.clock + 60))
    assert any(m.task_topic in t for t in topics(tracing.trace(realm, m.origin, now=realm.clock + 60)))


def test_the_legacy_record_classifies_old_notes_and_leaves_later_unstated_ones_unknown():
    realm, m = closed()
    desk = Mission(realm, "desk")
    old = cite(realm, m, desk, relation=None)
    later_desk = Mission(realm, "later")
    later = realm.post("pj-y", "old-style-delegate", rootchat_note(Conversation("front", later_desk.desk,
                                                                                later_desk.origin), None), FRONT)
    realm.post("pj-y", "old-style-delegate", "@**autolab-agstudio1** do this", FRONT)
    realm.post("front", "relations", relations.legacy_note(old, references=[old], why="beginnings read"), FRONT)
    book = relations.load(realm)
    assert book.of({"id": old, "sender_id": FRONT, "content": "[selfnote][rootchat] front/x"}).kind == "reference"
    assert book.of({"id": old - 1, "sender_id": FRONT, "content": "[selfnote][rootchat] front/x"}).kind == "work"
    assert book.of({"id": later, "sender_id": FRONT, "content": "[selfnote][rootchat] front/x"}).kind == "unknown"
    assert book.of({"id": old, "sender_id": AUTOLAB, "content": "[selfnote][rootchat] front/x"}).kind == "unknown", \
        "a legacy record classifies its author's notes only"
    result = tracing.trace(realm, desk.origin, now=realm.clock + 60)
    assert m.desk not in topics(result) and not result.unknown_relations
    assert [r["relation"] for r in tracing.trace(realm, later_desk.origin, now=realm.clock + 60).unknown_relations] \
        == ["unknown"]


def test_only_the_author_corrects_a_relation_and_the_newest_correction_wins():
    realm, m = closed()
    desk = Mission(realm, "desk")
    note = cite(realm, m, desk)
    realm.post("front", m.desk, relations.correction_note(note, "work", "not mine to say"), AUTOLAB)
    assert m.desk not in topics(tracing.trace(realm, desk.origin, now=realm.clock + 60))
    realm.post("front", m.desk, relations.correction_note(note, "work", "Front took the request over"), FRONT)
    assert m.desk in topics(tracing.trace(realm, desk.origin, now=realm.clock + 60))
    realm.post("front", m.desk, relations.correction_note(note, "reference", "back to a citation"), FRONT)
    assert m.desk not in topics(tracing.trace(realm, desk.origin, now=realm.clock + 60))


# --- writers: the default and the explicit word -------------------------------------------------


@pytest.fixture
def serving(monkeypatch):
    chat._ANCHORED.clear()

    def serve(desk: Mission):
        monkeypatch.setenv(selfnote.HOME_VARIABLE, f"front/{desk.desk}")
        monkeypatch.setenv(selfnote.HOME_ANCHOR_VARIABLE, str(desk.origin))
        chat._ANCHORED.clear()

    yield serve
    chat._ANCHORED.clear()


def written(realm: Realm) -> list[str]:
    return [w["content"] for w in realm.written]


def test_send_records_work_for_a_new_conversation_and_reference_for_somebody_else_s_request(serving):
    realm, m = closed()
    desk = Mission(realm, "desk")
    front = realm.speaking_as(FRONT)
    serving(desk)
    out = io.StringIO()
    chat.ensure_rootchat(front, "archsage-agstudio1", "study-new", out)
    chat.ensure_rootchat(front, "front", m.desk, out)  # m's request: begun by the Omni Agent
    realm.post(m.channel, "workrun-task2-x", f"[selfnote][task] {m.mission}#2", AUTOLAB)
    realm.post(m.channel, "workrun-task2-x", "Task 2 is waiting for its start.", AUTOLAB)
    realm.post(m.channel, "workrun-task2-x", "@**autolab-agstudio1** a note from the Developer.", DEV)
    chat.ensure_rootchat(front, m.channel, "workrun-task2-x", out)  # a task: opened for work
    assert [relations.Relations().of({"id": 1, "sender_id": FRONT, "content": c}).kind for c in written(front)] \
        == ["work", "reference", "work"]
    assert "recorded as a reference" in out.getvalue() and "--relation work" in out.getvalue()


def test_send_with_a_relation_overrides_the_default_and_later_corrects_its_own_note(serving):
    realm, m = closed()
    desk = Mission(realm, "desk")
    front = realm.speaking_as(FRONT)
    serving(desk)
    out = io.StringIO()
    chat.ensure_rootchat(front, "front", m.desk, out, "work")
    assert written(front)[-1].endswith("rel=work")
    chat._ANCHORED.clear()
    chat.ensure_rootchat(front, "front", m.desk, out, "reference")
    assert relations.parse_correction(written(front)[-1])["kind"] == "reference"
    chat._ANCHORED.clear()
    chat.ensure_rootchat(front, "front", m.desk, out, "reference")
    assert len(written(front)) == 2, "a repeat writes nothing"
    assert "already reference" in out.getvalue()


def test_an_unreadable_beginning_writes_an_unknown_relation_and_says_so(serving):
    realm, m = closed()
    desk = Mission(realm, "desk")
    front = realm.speaking_as(FRONT)

    def refuse(*_args, **_kwargs):
        raise zulip.ZulipError("no answer")

    front.topic_beginning = refuse
    serving(desk)
    out = io.StringIO()
    chat.ensure_rootchat(front, "front", m.desk, out)
    assert relations.Relations().of({"id": 1, "sender_id": FRONT, "content": written(front)[-1]}).kind == "unknown"
    assert "could not be read" in out.getvalue()


def test_agentchat_relation_lists_what_decided_each_note(monkeypatch, capsys):
    realm, m = closed()
    desk = Mission(realm, "desk")
    cite(realm, m, desk)
    out = io.StringIO()
    args = chat.build_parser().parse_args(["relation", "front", m.desk])
    assert chat.relation_command(realm, args, out) == 0
    text = out.getvalue()
    assert "returns answers to front/front-desk" in text and "reference (stated on the note)" in text


# --- delegation, citation with a reply, deliberate adoption --------------------------------------


def test_delegation_citation_and_adoption_each_have_their_owner_callback_and_scope():
    realm, m = closed()
    desk = Mission(realm, "desk", asker=DEV)
    plan = realm.topic_history("pj-x", m.plan_topic, 1000)
    before = acceptance.decision(realm, plan, AUTOLAB, "pj-x", m.plan_topic)
    # A citation with a reply: the answer comes back to the citing desk…
    cite(realm, m, desk)
    realm.post("front", m.desk, "@**Front** thanks, noted.", OMNI)
    assert zulip.rootchat_home(realm.speaking_as(FRONT), "front", m.desk, FRONT).topic == desk.desk
    # …and the cited request keeps its owner, its waits and its acceptance.
    after = acceptance.decision(realm, plan, AUTOLAB, "pj-x", m.plan_topic)
    assert after.holders == before.holders == [(FRONT, "Front"), (OMNI, "Omni Agent")]
    assert ("front", desk.desk) not in after.conversations
    assert m.desk not in topics(tracing.trace(realm, desk.origin, now=realm.clock + 60))
    # A delegation hangs its conversation under the desk, and the desk's
    # requester holds the acceptance.
    assert m.plan_topic in topics(tracing.trace(realm, m.origin, now=realm.clock + 60))
    own = acceptance.decision(realm, realm.topic_history("pj-x", desk.plan_topic, 1000), AUTOLAB, "pj-x",
                              desk.plan_topic)
    assert own.holders == [(FRONT, "Front"), (DEV, "Developer")]
    # A deliberate adoption moves the work, the callbacks and the scope.
    realm.post("front", m.desk, selfnote.rootchat_moved_note(Conversation("front", desk.desk, desk.origin)), FRONT)
    assert m.desk in topics(tracing.trace(realm, desk.origin, now=realm.clock + 60))
    assert zulip.rootchat_home(realm.speaking_as(FRONT), "front", m.desk, FRONT).topic == desk.desk


def test_a_receipt_decision_climbs_only_work_relations():
    realm, m = closed()
    desk = Mission(realm, "desk")
    # the desk's cancellation must not settle m's answers through a citation
    cite(realm, m, desk)
    realm.post("pj-x", desk.plan_topic, "[selfnote][state] cancelled", FRONT)
    history = realm.topic_history(m.channel, "✔ " + m.task_topic, 1000)
    assert receipts._decision_for(realm, history, m.closeout) is None


# --- names change; the relation still means the same request ------------------------------------


def test_rename_resolve_and_a_reused_name_keep_the_relation_on_its_request():
    realm, m = closed()
    desk = Mission(realm, "desk")
    cite(realm, m, desk)
    realm.resolve("front", m.desk, DEV)  # the cited request is ✔'d
    realm.resolve("front", desk.desk, DEV)  # so is the citing desk
    for row in realm.rows:  # …and the citing desk renamed
        if row["channel"] == "front" and row["topic"] == desk.desk:
            row["topic"] = desk.desk + "-renamed"
    reused = realm.post("front", m.desk, "A new request under the old name.", DEV)  # a twin reuses the name
    result = tracing.trace(realm, desk.origin, now=realm.clock + 60)
    assert not any(m.desk in t for t in topics(result)), "still a citation, and not of the newcomer"
    assert [r["relation"] for r in result.root.relations] == ["reference"]
    assert not any(n.anchor == reused for n in result.nodes())
    own = tracing.trace(realm, m.origin, now=realm.clock + 60)
    assert any(m.task_topic in t for t in topics(own))
