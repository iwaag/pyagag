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


# --- step 3: monitoring suppressed, or the request ended -----------------------------------------

from agag import dispositions as disposing
from agag import progress
from agag.selfnote import served_note


def unfinished() -> tuple[Realm, Mission]:
    """A request whose task showed a result nobody has taken up: open work,
    and an answer without its receipt."""
    realm = Realm()
    m = Mission(realm, "open")
    return realm, m


def decide(realm: Realm, m: Mission, kind: str, unit: int = 0, why: str = "the developer's cleanup call") -> int:
    evidence = realm.post("front", "front-desk-cleanup", f"Please mark {m.desk} {kind}.", DEV)
    written, _, _, _ = disposing.record(realm.speaking_as(FRONT), m.origin, kind, unit=unit, evidence=evidence,
                                        why=why)
    return written


def look(realm: Realm, m: Mission):
    result = tracing.trace(realm, m.origin, now=realm.clock + 60)
    return result, progress.card(result, now=realm.clock + 60)


def test_suppressed_monitoring_keeps_the_work_open_and_explains_it_on_every_reader():
    realm, m = unfinished()
    before, card_before = look(realm, m)
    decide(realm, m, "suppressed", why="the trial waits for the developer's return; do not chase it")
    result, card = look(realm, m)
    assert [n.state for n in result.nodes()] == [n.state for n in before.nodes()], "nothing is ended"
    assert card["state"] == card_before["state"] != "completed"
    task = next(n for n in result.nodes() if n.identity.startswith("task"))
    assert int(task.anchor) in disposing.suppressed_anchors(result), "Observer's scope"
    assert "monitoring suppressed by Developer" in progress.card(result, now=realm.clock + 60)["reason"] \
        or any("monitoring suppressed" in u["display"]["reason"] for u in progress._walk(card["root"]))
    assert any("monitoring suppressed" in line for line in tracing.trace_lines(result))


def test_a_suppression_on_one_unit_leaves_the_rest_of_the_request_monitored():
    realm, m = unfinished()
    task = next(n for n in look(realm, m)[0].nodes() if n.identity.startswith("task"))
    decide(realm, m, "suppressed", unit=int(task.anchor))
    result, _ = look(realm, m)
    assert disposing.suppressed_anchors(result) == {int(task.anchor)}


@pytest.mark.parametrize("kind,state,card_state", [("completed", "done", "completed"),
                                                   ("cancelled", "cancelled", "cancelled"),
                                                   ("withdrawn", "cancelled", "cancelled")])
def test_an_ended_request_reads_its_actual_outcome_and_lists_what_ended_with_it(kind, state, card_state):
    realm, m = unfinished()
    decide(realm, m, kind, why="the trial is over")
    result, card = look(realm, m)
    assert result.root.state == state
    task = next(n for n in result.nodes() if n.identity.startswith("task"))
    assert task.state == "cancelled", "work below an ended request is not completed by it"
    assert "ended with" in task.detail and "its own record: awaiting delivery" in task.detail
    (found,) = result.dispositions
    assert {r["label"] for r in found.remaining} >= {task.identity}
    assert card["state"] == card_state and f"ended: {kind} by Developer" in card["reason"]
    assert "ended with it:" in card["reason"]
    assert task.receipt.get("state") == "settled", "its missing receipt is bookkeeping now"
    assert not tracing.stall_candidates(result, now=realm.clock + 3600), "nothing to chase"


def test_new_substantive_activity_is_not_covered_but_bookkeeping_changes_nothing():
    realm, m = unfinished()
    decide(realm, m, "withdrawn")
    # bookkeeping: a receipt repair, a served mark, a ✔ — all after the decision
    realm.post("front", m.desk, f"[selfnote][receipt] #{m.shown} by #{m.shown} (bookkeeping) in "
                                f"{m.channel}/{m.task_topic}", FRONT)
    realm.post("front", m.desk, served_note(Conversation(m.channel, m.task_topic), m.shown), FRONT)
    realm.resolve("front", m.desk, DEV)
    result, card = look(realm, m)
    assert result.root.state == "cancelled" and card["state"] == "cancelled", "bookkeeping is not activity"
    # a new request in the same conversation is new activity
    realm.post("front", m.desk, "One more thing: please also check the logs.", DEV)
    result, card = look(realm, m)
    assert result.root.state != "cancelled" and card["state"] not in ("cancelled", "completed")
    assert result.root.disposition["covered"] is False
    task = next(n for n in result.nodes() if n.identity.startswith("task"))
    assert task.state == "cancelled", "the part nothing new happened in stays ended"


def test_a_new_result_under_a_suppression_is_monitored_again():
    realm, m = unfinished()
    decide(realm, m, "suppressed")
    result, _ = look(realm, m)
    task = next(n for n in result.nodes() if n.identity.startswith("task"))
    assert int(task.anchor) in disposing.suppressed_anchors(result)
    realm.post(m.channel, m.task_topic, "@**Front** a second result, please look.", AUTOLAB)
    result, _ = look(realm, m)
    assert int(task.anchor) not in disposing.suppressed_anchors(result)


def test_repeats_reversals_and_restarts_converge():
    realm, m = unfinished()
    first = decide(realm, m, "withdrawn")
    assert first
    evidence = int(disposing.read_dispositions(realm, m.origin)[0][0].evidence)
    again = disposing.record(realm.speaking_as(FRONT), m.origin, "withdrawn", evidence=evidence)
    assert again[0] == 0 and again[1].id == first, "a repeat of the same decision writes nothing"
    # A fresh reader (a restart) reads the same outcome from the same records.
    assert look(realm, m)[1]["state"] == look(realm, m)[1]["state"] == "cancelled"
    undo = realm.post("front", "front-desk-cleanup", "Actually, keep it open.", DEV)
    written, _ = disposing.reverse(realm.speaking_as(FRONT), first, evidence=undo, why="reopened")
    assert written and disposing.reverse(realm.speaking_as(FRONT), first, evidence=undo)[0] == 0
    result, card = look(realm, m)
    assert card["state"] != "cancelled" and result.dispositions[0].state == "reversed"


def test_agentchat_disposition_lists_records_and_refuses_without_evidence():
    realm, m = unfinished()
    front = realm.speaking_as(FRONT)
    out = io.StringIO()
    args = chat.build_parser().parse_args(["disposition", str(m.origin), "completed", "done", "here"])
    assert disposing.command(front, args, out) == 1 and "--evidence" in out.getvalue()
    evidence = realm.post("front", "front-desk-cleanup", "It is complete for me.", DEV)
    out = io.StringIO()
    args = chat.build_parser().parse_args(["disposition", str(m.origin), "completed", "--evidence", str(evidence),
                                           "requested", "outcome", "reached"])
    assert disposing.command(front, args, out) == 0
    assert "recorded #" in out.getvalue() and "ended with it:" in out.getvalue()
    out = io.StringIO()
    assert disposing.command(front, chat.build_parser().parse_args(["disposition", str(m.origin)]), out) == 0
    assert "IN_FORCE — ended: completed" in out.getvalue()


def test_the_recorder_s_own_reply_reporting_the_decision_is_not_new_activity():
    realm, m = unfinished()
    ask = realm.post("front", m.desk, "This trial is finished; please close it out.", DEV)
    ack = realm.post("front", m.desk, ACK, FRONT)
    written, _, _, _ = disposing.record(realm.speaking_as(FRONT), m.origin, "completed", evidence=ask,
                                        why="the developer closed it")
    realm.post("front", m.desk, f"@**Developer** recorded it as completed.\n\n`ag-post intent=report end={ack}`",
               FRONT)
    result, card = look(realm, m)
    assert result.root.state == "done" and card["state"] == "completed"
    again = disposing.record(realm.speaking_as(FRONT), m.origin, "completed", evidence=ask)
    assert again[0] == 0, "the same decision, still covering, converges"
    realm.post("front", m.desk, "@**Developer** by the way, a new finding.\n\n`ag-post intent=report`", FRONT)
    assert look(realm, m)[0].root.disposition["covered"] is False, "anything else Front says later is new"


def test_a_task_its_owner_started_names_its_real_requester_for_agreement():
    realm = Realm()
    m = Mission(realm, "self-started")
    # autolab starts task 2 itself: only its own root note is in the topic
    topic = f"workrun-task2-m{m.mission}"
    realm.post(m.channel, topic, f"[selfnote][task] {m.mission}#2", AUTOLAB)
    realm.post(m.channel, topic, f"[selfnote][rootchat] pj-x/{m.plan_topic} #{m.mission} rel=work", AUTOLAB)
    realm.post(m.channel, topic, f"[selfnote][start] #{m.plan_answer} for {FRONT} Front", AUTOLAB)
    realm.post(m.channel, topic, ACK, AUTOLAB)
    realm.post(m.channel, topic, "@**Front** task 2 is built; agree to it?", AUTOLAB)
    result, card = look(realm, m)
    task2 = next(n for n in result.nodes() if n.identity.endswith("#2"))
    assert "Front" in task2.requesters
    unit = next(u for u in progress._walk(card["root"]) if u["label"].endswith("#2"))
    assert "autolab-agstudio1's agreement" not in unit["display"]["reason"]


def test_a_closed_plain_exchange_under_a_request_is_not_resolved_live():
    """p6 ex1 step 5: once o14251's retirement stopped hiding it, Observer
    asked about archsage's refresh topic — answered, taken up, ✔ — as `✔
    while awaiting requester`. A plain exchange that complete is closed."""
    realm = Realm()
    m = Mission(realm, "exchange")
    realm.post("archsage-agstudio1", "refresh-x", f"[selfnote][rootchat] front/{m.desk} #{m.origin} rel=work", FRONT)
    realm.post("archsage-agstudio1", "refresh-x", "@**archsage** please refresh.", FRONT)
    realm.post("archsage-agstudio1", "refresh-x", ACK, 24)
    answer = realm.post("archsage-agstudio1", "refresh-x", "@**Front** refreshed.\n\n`ag-post intent=report end=0`", 24)
    realm.post("front", m.desk, served_note(Conversation("archsage-agstudio1", "refresh-x"), answer), FRONT)
    realm.resolve("archsage-agstudio1", "refresh-x", FRONT)
    result = tracing.trace(realm, m.origin, now=realm.clock + 3600)
    node = next(n for n in result.nodes() if "refresh-x" in n.topic)
    assert node.state == "awaiting_requester" and node.taken_up
    assert not [c for c in tracing.stall_candidates(result, now=realm.clock + 3600)
                if c.kind == "resolved_live" and "refresh-x" in c.topic]
