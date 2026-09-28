"""failsafe p6: completion, receipts and holds read the same way everywhere.

The realms are synthetic (`study_realm.Realm`), shaped like m8519 (o8512):
a request in `#front`, the workplan Front opened for it, a task autolab
opened and Front started, a result shown for review, the requester's
agreement, autolab's close-out naming Front — and Front's receipt for it
lost to the ✔ that followed in the same second.
"""

from __future__ import annotations

from agag import progress
from agag import trace as tracing
from agag.selfnote import Conversation, receipt_note, served_note

from study_realm import ACK, AUTOLAB, DEV, FRONT, NAMES, OMNI, Realm, line


def node(result, topic_part):
    return next(n for n in result.nodes() if topic_part in n.topic)


class Mission:
    """One request, one mission, one task — m8519's shape."""

    def __init__(self, realm: Realm, name: str = "x", *, asker: int = OMNI):
        self.realm = realm
        self.desk = f"front-{name}"
        self.plan_topic = f"workplan-{name}"
        r = realm
        self.origin = r.post("front", self.desk, f"Please build {name}.", asker)
        r.post("front", self.desk, ACK, FRONT)
        r.post("front", self.desk, f"@**{NAMES[asker]}** asked autolab.\n\n" + line("report", end=self.origin + 1),
               FRONT)
        r.post("pj-x", self.plan_topic, f"[selfnote][rootchat] front/{self.desk} #{self.origin} rel=work", FRONT)
        r.post("pj-x", self.plan_topic, f"@**autolab-agstudio1** mission: build {name}.", FRONT)
        r.post("pj-x", self.plan_topic, ACK, AUTOLAB)
        self.mission = r.post("pj-x", self.plan_topic, f"[selfnote][mission] {name}", AUTOLAB)
        r.post("pj-x", self.plan_topic, "[selfnote][state] started", AUTOLAB)
        self.task_topic = f"workrun-task1-m{self.mission}"
        self.channel = f"work-m{self.mission}"
        self.task = r.post(self.channel, self.task_topic, f"[selfnote][task] {self.mission}#1", AUTOLAB)
        r.post(self.channel, self.task_topic, f"[selfnote][rootchat] pj-x/{self.plan_topic} #{self.mission} rel=work",
               AUTOLAB)
        self.plan_answer = r.post("pj-x", self.plan_topic, "@**Front** planned; task 1 is queued.\n\n"
                                  + line("report", end=0), AUTOLAB)
        r.post("front", self.desk, f"[selfnote][served] pj-x/{self.plan_topic} {self.plan_answer}", FRONT)
        r.post(self.channel, self.task_topic, f"[selfnote][rootchat] front/{self.desk} #{self.origin} rel=work", FRONT)
        r.post(self.channel, self.task_topic, "Start task 1.", FRONT)
        r.post(self.channel, self.task_topic, ACK, AUTOLAB)
        self.shown = r.post(self.channel, self.task_topic, "@**Front** built it; commit it?", AUTOLAB)

    def front_serves(self, answer: int) -> int:
        return self.realm.post("front", self.desk, served_note(Conversation(self.channel, self.task_topic), answer),
                               FRONT)

    def front_agrees(self) -> int:
        r = self.realm
        self.front_serves(self.shown)
        self.agreement = r.post(self.channel, self.task_topic, "Accepted: commit it.", FRONT)
        r.post(self.channel, self.task_topic, ACK, AUTOLAB)
        return self.agreement

    def autolab_closes(self, *, bound: bool = False) -> int:
        """The close-out: the record, `completed`, the report naming Front and
        the ✔ — Front's receipt for it is not written (the p1 race)."""
        r = self.realm
        if bound:
            r.post(self.channel, self.task_topic,
                   f"[selfnote][change] accepted #{self.agreement} +gen=1 +shown={self.shown}", AUTOLAB)
        r.post(self.channel, self.task_topic, "## Result\n\ncommitted abc1234", AUTOLAB)
        r.post(self.channel, self.task_topic, "[selfnote][state] completed", AUTOLAB)
        self.closeout = r.post(self.channel, self.task_topic, "@**Front** task 1 is closed: committed abc1234.",
                               AUTOLAB)
        r.resolve(self.channel, self.task_topic, AUTOLAB)
        return self.closeout

    def mission_accepted(self, *, by: int = FRONT, after: int | None = None) -> int:
        """`agentchat accept`'s writes: the task's `accepted`, the acceptance
        record and the mission's `done`."""
        r = self.realm
        evidence = r.post("front", self.desk, "The mission is accepted.", OMNI)
        ack = r.post("front", self.desk, ACK, FRONT)
        r.post("front", self.desk, "@**Omni Agent** recorded.\n\n" + line("report", end=ack), FRONT)
        r.post(self.channel, self.task_topic, "[selfnote][state] accepted", by)
        shown = self.closeout if after is None else after
        record = r.post("pj-x", self.plan_topic, f"[selfnote][acceptance] #{evidence} by {OMNI} (Omni Agent) "
                        f"after=#{shown}", by)
        r.post("pj-x", self.plan_topic, "[selfnote][state] done", by)
        return record


def closed(**kwargs) -> tuple[Realm, Mission]:
    realm = Realm()
    m = Mission(realm, **kwargs)
    m.front_agrees()
    m.autolab_closes()
    return realm, m


# --- step 2: completion, acceptance and receipt are separate facts -------------------------


def test_a_producer_s_completion_alone_leaves_its_unreceived_result_owed():
    realm, m = closed()
    result = tracing.trace(realm, m.origin, now=realm.clock + 600)
    task = node(result, m.task_topic)
    assert task.state == "awaiting_delivery" and task.note_state == "completed"
    assert task.owed_to == ["Front"]
    assert task.receipt == {**task.receipt, "answer": m.closeout, "state": "missing", "settled_by": None}
    card = progress.card(result, now=realm.clock + 600)
    assert card["state"] == "waiting"
    assert card["reason"] == f"answer #{m.closeout} is not yet taken up by Front" and card["next"] == "Front"
    assert [c.kind for c in tracing.stall_candidates(result, now=realm.clock + 600)] == ["undelivered"]


def test_a_valid_acceptance_after_the_result_settles_it_and_the_receipt_is_bookkeeping():
    realm, m = closed()
    record = m.mission_accepted()
    now = realm.clock + 600
    result = tracing.trace(realm, m.origin, now=now)
    task = node(result, m.task_topic)
    assert task.state == "done"
    assert task.receipt["state"] == "settled" and task.receipt["answer"] == m.closeout
    assert "has no receipt" in task.detail and "settled by" in task.detail
    assert not [c for c in tracing.stall_candidates(result, now=now) if c.kind == "undelivered"]
    card = progress.card(result, now=now)
    assert card["state"] == "completed"
    assert [r["answer"] for r in card["settled_receipts"]] == [m.closeout]
    assert record  # the mission's own acceptance is on record


def test_the_mission_s_acceptance_alone_settles_its_tasks_answers_up_to_the_shown_result():
    realm, m = closed()
    evidence = realm.post("front", m.desk, "Accepted.", OMNI)
    ack = realm.post("front", m.desk, ACK, FRONT)
    realm.post("front", m.desk, "@**Omni Agent** recorded.\n\n" + line("report", end=ack), FRONT)
    realm.post("pj-x", m.plan_topic, f"[selfnote][acceptance] #{evidence} by {OMNI} after=#{m.closeout}", FRONT)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "done" and task.receipt["settled_by"]["kind"] == "accepted"


def test_the_same_request_reads_the_same_from_the_mission_and_from_the_request():
    realm, m = closed()
    now = realm.clock + 600
    before = [node(tracing.trace(realm, start, now=now), m.task_topic) for start in (m.origin, m.mission)]
    assert [n.state for n in before] == ["awaiting_delivery", "awaiting_delivery"]
    assert before[1].owed_to == ["Front"]
    m.mission_accepted()
    after = [node(tracing.trace(realm, start, now=now), m.task_topic) for start in (m.origin, m.mission)]
    assert [(n.state, n.receipt["state"]) for n in after] == [("done", "settled"), ("done", "settled")]


def test_an_acceptance_bound_to_the_shown_result_does_not_cover_the_later_close_out():
    realm = Realm()
    m = Mission(realm)
    m.front_agrees()
    m.autolab_closes(bound=True)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 600), m.task_topic)
    assert task.state == "awaiting_delivery" and task.receipt["answer"] == m.closeout
    m.front_serves(m.closeout)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 600), m.task_topic)
    assert task.state == "done" and not task.receipt


def test_a_result_after_the_acceptance_it_did_not_see_stays_owed():
    realm, m = closed()
    m.mission_accepted()
    later = realm.post(m.channel, m.task_topic, "@**Front** one more thing changed: abc9999.", AUTOLAB)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 600), m.task_topic)
    assert task.state == "awaiting_delivery" and task.receipt["answer"] == later


def test_another_request_s_acceptance_settles_nothing_here():
    realm, m = closed()
    other = Mission(realm, "y")
    other.front_agrees()
    other.autolab_closes()
    other.mission_accepted()
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 600), m.task_topic)
    assert task.state == "awaiting_delivery"


def test_a_cancellation_on_a_holder_s_request_settles_results_before_it_not_after():
    realm, m = closed()
    realm.post("pj-x", m.plan_topic, "@**autolab-agstudio1** cancel it; nobody will accept it.", OMNI)
    cancelled = realm.post("pj-x", m.plan_topic, "[selfnote][state] cancelled", AUTOLAB)
    result = tracing.trace(realm, m.origin, now=realm.clock + 600)
    task = node(result, m.task_topic)
    assert node(result, m.plan_topic).state == "cancelled"
    assert task.state == "done" and task.receipt["settled_by"]["id"] == cancelled
    assert task.note_state == "completed", "the task's own record is kept, as p3 corrected it"


def test_a_reconciled_receipt_covers_exactly_the_answer_it_names():
    realm, m = closed()
    home = Conversation(m.channel, m.task_topic)
    realm.post("front", m.desk, receipt_note(home, m.shown, m.agreement, "relayed"), FRONT)
    assert node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic).state == "awaiting_delivery"
    realm.post("front", m.desk, receipt_note(home, m.closeout, m.agreement, "relayed"), OMNI)
    assert node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic).state == "awaiting_delivery", \
        "only the agent the answer is owed to can write its receipt"
    realm.post("front", m.desk, receipt_note(home, m.closeout, m.agreement, "relayed"), FRONT)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "done" and not task.receipt


def test_speech_at_home_is_no_receipt_for_any_reader():
    realm, m = closed()
    realm.post("front", m.desk, "@**Omni Agent** task 1 is committed (abc1234).", FRONT)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "awaiting_delivery"


def test_citing_another_request_during_cleanup_adopts_nothing():
    realm, m = closed()
    m.mission_accepted()
    desk = Mission(realm, "desk")  # another request, whose run cleans up
    note = realm.post("front", m.desk, f"[selfnote][rootchat] front/{desk.desk} #{desk.origin} rel=reference", FRONT)
    realm.post("front", m.desk, "For the record: task 1's report was taken up here.", FRONT)
    cleanup = tracing.trace(realm, desk.origin, now=realm.clock + 60)
    assert m.desk not in {n.topic for n in cleanup.nodes()}, "the cited request is not the desk's work"
    assert m.task_topic not in {n.topic for n in cleanup.nodes()}
    own = tracing.trace(realm, m.origin, now=realm.clock + 60)
    assert node(own, m.task_topic).state == "done"
    assert note


def test_a_conversation_opened_for_the_work_is_still_adopted():
    realm = Realm()
    origin = realm.post("front", "front-a", "Ask archsage about it.", DEV)
    realm.post("front", "front-a", ACK, FRONT)
    realm.post("archsage-agstudio1", "study-a", f"[selfnote][rootchat] front/front-a #{origin} rel=work", FRONT)
    realm.post("archsage-agstudio1", "study-a", "@**archsage** what is known?", FRONT)
    # …and one Front began with speech, before its note (the order some sends take).
    realm.post("archsage-agstudio1", "study-b", "@**archsage** and this?", FRONT)
    realm.post("archsage-agstudio1", "study-b", f"[selfnote][rootchat] front/front-a #{origin} rel=work", FRONT)
    topics = {n.topic for n in tracing.trace(realm, origin, now=realm.clock + 60).nodes()}
    assert {"study-a", "study-b"} <= topics


def test_a_deliberate_move_adopts_even_a_conversation_somebody_else_began():
    realm = Realm()
    origin = realm.post("front", "front-a", "Run it.", DEV)
    realm.post("front", "front-a", ACK, FRONT)
    realm.post("pj-x", "workplan-side", "@**autolab-agstudio1** plan this.", DEV)
    realm.post("pj-x", "workplan-side", f"[selfnote][rootchat-moved] front/front-a #{origin}", FRONT)
    assert "workplan-side" in {n.topic for n in tracing.trace(realm, origin, now=realm.clock + 60).nodes()}


# --- step 3: inspecting and repairing a receipt ---------------------------------------------

import json
import sqlite3

from agag import receipt as receipts
from agag.selfnote import parse_receipt, parse_served


def journal(tmp_path, *servings) -> str:
    """A listener journal holding delivered servings, as `agag.listen` writes them."""
    path = tmp_path / f"listener-{len(list(tmp_path.glob('listener-*')))}.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE servings (id INTEGER PRIMARY KEY, route TEXT, channel TEXT, topic TEXT, trigger_id "
               "INTEGER, state TEXT, delivered_id INTEGER, extra TEXT)")
    for number, (route, channel, topic, trigger, delivered, inputs) in enumerate(servings, start=1):
        db.execute("INSERT INTO servings VALUES (?, ?, ?, ?, ?, 'delivered', ?, ?)",
                   (number, route, channel, topic, trigger, delivered, json.dumps({"inputs": inputs})))
    db.commit()
    db.close()
    return str(path)


def test_an_accepted_answer_without_a_receipt_is_reconciled_on_its_decision_and_repair_converges(tmp_path):
    realm, m = closed()
    m.mission_accepted()
    found = receipts.inspect(realm, m.closeout, journal=journal(tmp_path))
    assert (found.state, found.action, found.named) == ("missing", "reconciled", True)
    assert found.home == f"front/{m.desk}" and found.decision["kind"] == "accepted"
    repaired = receipts.repair(realm, found)
    written = realm.written[-1]
    assert (written["channel"], written["topic"]) == ("front", m.desk)
    remote, answer, evidence, why = parse_receipt(written["content"])
    assert (answer, evidence, why) == (m.closeout, found.decision["id"], "accepted")
    assert repaired.state == "reconciled"
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "done" and not task.receipt, "the bookkeeping is done"
    again = receipts.repair(realm, receipts.inspect(realm, m.closeout, journal=journal(tmp_path)))
    assert again.state == "reconciled" and realm.written[-1] == written, "a repeat writes nothing"


def test_journal_evidence_writes_the_mark_the_listener_would_have_written(tmp_path):
    """Crash after the reply was delivered, before its receipt: the journal
    shows the serving was handed the thread holding the close-out."""
    realm, m = closed()
    path = journal(tmp_path, ("mention", m.channel, m.task_topic, m.closeout, m.closeout + 10,
                              [{"channel": m.channel, "topic": m.task_topic, "first": m.task,
                                "last": m.closeout, "complete": True}]))
    found = receipts.inspect(realm, m.closeout, journal=path)
    assert found.action == "served" and found.journal[0]["serving"] == 1
    receipts.repair(realm, found)
    remote, marked = parse_served(realm.written[-1]["content"])
    assert marked == m.closeout and remote.topic == m.task_topic
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "done" and not task.receipt


def test_a_mark_is_never_written_over_an_earlier_answer_nobody_was_given(tmp_path):
    realm, m = closed()  # the shown result was served; nothing else below
    extra = realm.post(m.channel, m.task_topic, "@**Front** by the way, a note.", AUTOLAB)
    last = realm.post(m.channel, m.task_topic, "@**Front** and the final word.", AUTOLAB)
    path = journal(tmp_path, ("mention", m.channel, m.task_topic, last, last + 10,
                              [{"channel": m.channel, "topic": m.task_topic, "first": last, "last": last,
                                "complete": False}]))
    found = receipts.inspect(realm, last, journal=path)
    assert found.journal and extra in found.unmarked_before
    assert found.action == "reconciled", "only this answer's own receipt; #extra and the close-out stay owed"
    receipts.repair(realm, found)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "awaiting_delivery", "the earlier, ungiven answers are still owed"


def test_no_evidence_writes_nothing_and_because_names_the_agent_s_own_relay(tmp_path):
    realm, m = closed()
    found = receipts.repair(realm, receipts.inspect(realm, m.closeout, journal=journal(tmp_path)))
    assert found.action == "refuse" and found.state == "missing" and not realm.written
    relay = realm.post("front", m.desk, "@**Omni Agent** task 1 is committed as abc1234.", FRONT)
    stranger = realm.post("front", m.desk, "I read it too.", OMNI)
    assert receipts.inspect(realm, m.closeout, journal=journal(tmp_path), because=stranger).action == "refuse"
    found = receipts.repair(realm, receipts.inspect(realm, m.closeout, journal=journal(tmp_path), because=relay))
    assert parse_receipt(realm.written[-1]["content"])[2:] == (relay, "relayed")
    assert node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic).state == "done"


def test_an_answer_arriving_after_the_repaired_one_stays_owed(tmp_path):
    realm, m = closed()
    m.mission_accepted()
    found = receipts.inspect(realm, m.closeout, journal=journal(tmp_path))
    newer = realm.post(m.channel, m.task_topic, "@**Front** one more result: abc9999.", AUTOLAB)
    receipts.repair(realm, found)
    task = node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic)
    assert task.state == "awaiting_delivery" and task.receipt["answer"] == newer
    assert receipts.inspect(realm, newer, journal=journal(tmp_path)).action == "refuse"


def test_a_renamed_or_resolved_home_and_an_answer_not_naming_you(tmp_path):
    realm, m = closed()
    m.mission_accepted()
    realm.resolve("front", m.desk, FRONT)
    found = receipts.repair(realm, receipts.inspect(realm, m.closeout, journal=journal(tmp_path)))
    assert found.home_live == f"✔ {m.desk}" and realm.written[-1]["topic"] == m.desk
    assert node(tracing.trace(realm, m.origin, now=realm.clock + 60), m.task_topic).state == "done"
    other = realm.post(m.channel, m.task_topic, "a note to nobody", AUTOLAB)
    assert receipts.inspect(realm, other, journal=journal(tmp_path)).state == "not_owed"


def test_agentchat_receipt_prints_and_repairs(monkeypatch, tmp_path, capsys):
    from agag import chat

    realm, m = closed()
    m.mission_accepted()
    monkeypatch.setattr(chat, "client_from_environment", lambda *a, **k: realm)
    monkeypatch.setenv("AGENTCHAT_JOURNAL", journal(tmp_path))
    assert chat.main(["receipt", str(m.closeout)]) == 0
    printed = capsys.readouterr().out
    assert "MISSING" in printed and "decision:" in printed and "--repair writes a reconciled receipt" in printed
    assert chat.main(["receipt", str(m.closeout), "--repair"]) == 0
    assert "RECONCILED" in capsys.readouterr().out


# --- step 4: a person's hold, from placing to settling ---------------------------------------

from types import SimpleNamespace

from agag import holds as holding


def _hold(realm, m, purpose, unit, evidence, why="theirs to decide"):
    args = SimpleNamespace(message_id=m.origin, purpose=purpose, unit=unit, evidence=evidence, why=why.split(),
                           json=False)
    import io
    out = io.StringIO()
    code = holding.holds_command(realm, args, out)
    return code, out.getvalue()


def test_a_hold_on_acceptance_says_what_it_waits_for_and_settles_with_the_acceptance():
    realm = Realm()
    m = Mission(realm)
    m.front_agrees()
    m.autolab_closes()
    m.front_serves(m.closeout)
    ask = realm.post("front", m.desk, "I will accept this one myself after I try it.", DEV)
    code, printed = _hold(realm, m, "acceptance", m.mission, ask)
    assert code == 0 and "held: #" in printed
    card = progress.card(tracing.trace(realm, m.origin, now=realm.clock + 60), now=realm.clock + 60, viewer_id=DEV)
    assert card["state"] == "awaiting_you" and card["next"] == "you"
    plan = next(u for u in card["root"]["children"] if u["kind"] == "plan")
    assert "held by Developer for acceptance" in plan["display"]["reason"]
    assert "Developer's acceptance of mission" in plan["display"]["reason"]
    assert _hold(realm, m, "acceptance", m.mission, ask)[1] == printed, "placing it again writes nothing new"
    m.mission_accepted()
    result = tracing.trace(realm, m.origin, now=realm.clock + 60)
    (hold,) = holding.holds_of(result)
    assert hold.state == "settled" and hold.ended_by["how"] == "accepted"
    assert [e["event"] for e in hold.history] == ["held", "settled"]
    assert progress.card(result, now=realm.clock + 60, viewer_id=DEV)["state"] == "completed"


def test_a_hold_on_a_resume_settles_when_the_work_is_served_again_not_on_unrelated_activity():
    realm = Realm()
    m = Mission(realm)  # the task has shown a result and waits
    ask = realm.post("front", m.desk, "Stop there; I decide how it goes on.", DEV)
    _hold(realm, m, "resume", m.task, ask)
    realm.post("front", m.desk, "Unrelated: what time is it?", DEV)
    realm.post("front", m.desk, ACK, FRONT)
    realm.post(m.channel, m.task_topic, "[selfnote][state] started", AUTOLAB)
    realm.resolve(m.channel, m.task_topic, AUTOLAB)
    (hold,) = holding.holds_of(tracing.trace(realm, m.origin, now=realm.clock + 60))
    assert hold.state == "held", "a post, a ✔ or a state word is not the work served again"
    m.front_agrees()  # Front serves the task again on the Developer's word, autolab acks and works
    realm.post(m.channel, m.task_topic, "🔧 Bash: git commit", AUTOLAB)
    (hold,) = holding.holds_of(tracing.trace(realm, m.origin, now=realm.clock + 60))
    assert hold.state == "settled" and hold.ended_by["how"] == "resumed"


def test_an_intentional_hold_stays_until_its_holder_releases_it_and_only_on_their_words():
    realm, m = closed()
    m.mission_accepted()
    ask = realm.post("front", m.desk, "Keep this request with me until I say otherwise.", DEV)
    _hold(realm, m, "indefinite", m.origin, ask, "kept on purpose")
    result = tracing.trace(realm, m.origin, now=realm.clock + 60)
    (hold,) = holding.holds_of(result)
    assert hold.state == "held" and "keeps it on purpose" in hold.waits_for
    assert progress.card(result, now=realm.clock + 60, viewer_id=DEV)["state"] == "awaiting_you", \
        "an intentional hold stays visible on finished work"
    other = realm.post("front", m.desk, "Release it.", OMNI)
    try:
        holding.release(realm, hold.id, other)
        raise AssertionError("released on somebody else's words")
    except holding.HoldRefused as refused:
        assert "Developer" in str(refused)
    words = realm.post("front", m.desk, "You can let it go now.", DEV)
    written, _ = holding.release(realm, hold.id, words, "done with it")
    assert written
    assert holding.release(realm, hold.id, words)[0] == 0, "a repeat writes nothing"
    (hold,) = holding.holds_of(tracing.trace(realm, m.origin, now=realm.clock + 60))
    assert hold.state == "released" and hold.history[-1]["event"] == "released"


def test_a_hold_covers_only_its_work_and_new_obligations_stay_visible():
    realm = Realm()
    m = Mission(realm)
    ask = realm.post("front", m.desk, "I accept m1 myself.", DEV)
    _hold(realm, m, "acceptance", m.mission, ask)
    # New work in the same request: a second plan whose answer is owed.
    realm.post("pj-x", "workplan-second", f"[selfnote][rootchat] front/{m.desk} #{m.origin} rel=work", FRONT)
    realm.post("pj-x", "workplan-second", "@**autolab-agstudio1** and a second thing.", FRONT)
    realm.post("pj-x", "workplan-second", ACK, AUTOLAB)
    second = realm.post("pj-x", "workplan-second", "[selfnote][mission] second", AUTOLAB)
    owed = realm.post("pj-x", "workplan-second", "@**Front** planned the second.", AUTOLAB)
    now = realm.clock + 900
    result = tracing.trace(realm, m.origin, now=now)
    (hold,) = holding.active(holding.holds_of(result))
    plan, task, other = (node(result, t).anchor for t in (m.plan_topic, m.task_topic, "workplan-second"))
    assert holding.covers(hold, result, task) and holding.covers(hold, result, plan)
    assert not holding.covers(hold, result, other)
    found = [c for c in tracing.stall_candidates(result, now=now) if c.anchor == other]
    assert [c.kind for c in found] == ["undelivered"] and found[0].evidence[0] == owed


def test_a_hold_on_work_outside_the_request_or_on_an_agent_s_own_words_is_refused():
    realm, m = closed()
    other = Mission(realm, "y")
    ask = realm.post("front", m.desk, "Mine to accept.", DEV)
    code, printed = _hold(realm, m, "acceptance", other.mission, ask)
    assert code == 1 and "not in this request" in printed
    code, printed = _hold(realm, m, "acceptance", m.mission, m.shown)
    assert code == 1 and "doing that work" in printed
