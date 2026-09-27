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
        r.post("pj-x", self.plan_topic, f"[selfnote][rootchat] front/{self.desk} #{self.origin}", FRONT)
        r.post("pj-x", self.plan_topic, f"@**autolab-agstudio1** mission: build {name}.", FRONT)
        r.post("pj-x", self.plan_topic, ACK, AUTOLAB)
        self.mission = r.post("pj-x", self.plan_topic, f"[selfnote][mission] {name}", AUTOLAB)
        r.post("pj-x", self.plan_topic, "[selfnote][state] started", AUTOLAB)
        self.task_topic = f"workrun-task1-m{self.mission}"
        self.channel = f"work-m{self.mission}"
        self.task = r.post(self.channel, self.task_topic, f"[selfnote][task] {self.mission}#1", AUTOLAB)
        r.post(self.channel, self.task_topic, f"[selfnote][rootchat] pj-x/{self.plan_topic} #{self.mission}",
               AUTOLAB)
        self.plan_answer = r.post("pj-x", self.plan_topic, "@**Front** planned; task 1 is queued.\n\n"
                                  + line("report", end=0), AUTOLAB)
        r.post("front", self.desk, f"[selfnote][served] pj-x/{self.plan_topic} {self.plan_answer}", FRONT)
        r.post(self.channel, self.task_topic, f"[selfnote][rootchat] front/{self.desk} #{self.origin}", FRONT)
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
    note = realm.post("front", m.desk, f"[selfnote][rootchat] front/{desk.desk} #{desk.origin}", FRONT)
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
    realm.post("archsage-agstudio1", "study-a", f"[selfnote][rootchat] front/front-a #{origin}", FRONT)
    realm.post("archsage-agstudio1", "study-a", "@**archsage** what is known?", FRONT)
    # …and one Front began with speech, before its note (the order some sends take).
    realm.post("archsage-agstudio1", "study-b", "@**archsage** and this?", FRONT)
    realm.post("archsage-agstudio1", "study-b", f"[selfnote][rootchat] front/front-a #{origin}", FRONT)
    topics = {n.topic for n in tracing.trace(realm, origin, now=realm.clock + 60).nodes()}
    assert {"study-a", "study-b"} <= topics


def test_a_deliberate_move_adopts_even_a_conversation_somebody_else_began():
    realm = Realm()
    origin = realm.post("front", "front-a", "Run it.", DEV)
    realm.post("front", "front-a", ACK, FRONT)
    realm.post("pj-x", "workplan-side", "@**autolab-agstudio1** plan this.", DEV)
    realm.post("pj-x", "workplan-side", f"[selfnote][rootchat-moved] front/front-a #{origin}", FRONT)
    assert "workplan-side" in {n.topic for n in tracing.trace(realm, origin, now=realm.clock + 60).nodes()}
