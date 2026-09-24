"""`agag.trace`: where a request stands, read from its conversations.

The fixture is adventure_game p3's first request as the realm held it
(messages 8280–8460, text shortened): Front's conversation, the mission it
opened, the twin it opened by mistake, and the five task topics. Cut at a
message id, it is the realm as a reader would have found it at that moment —
which is what the tests below ask: would the trace have shown the stalls the
Omni Agent had to find by reading topics one at a time?
"""

import json
from pathlib import Path

import pytest

from agag import trace as tracing
from agag.agent import SWEEP_ACK
from agag.zulip import RESOLVED_TOPIC_PREFIX, ZulipError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "trace_p3.json").read_text("utf-8"))


def _marks_as_written_today(messages):
    """p3's served marks were written by the pre-journal `note_served`,
    which marked the newest *real* post of the remote topic at the time
    (task 2's says 8382, a progress line; the answer naming Front, which that
    serving read, is 8386). Since `explicit_reply` p1 the listener binds the
    mark to the mention the serving processed, and since robust_workflow p2
    step 3 the mark is the only receipt the trace accepts. The replay is
    therefore read with each mark moved to the newest post naming Front in
    that remote topic before the mark was written — what today's writer
    would have recorded for the same serving."""
    import re

    from agag.selfnote import parse_served

    out = []
    for m in messages:
        parsed = parse_served(m["content"])
        if parsed is not None and m["sender_full_name"] == "Front":
            remote, _ = parsed
            named = [x["id"] for x in messages
                     if x["channel"] == remote.channel and x["topic"] == remote.topic and x["id"] < m["id"]
                     and re.search(r"@\*\*Front\*\*", x["content"])]
            if named:
                m = {**m, "content": f"[selfnote][served] {remote.channel}/{remote.topic} {max(named)}"}
        out.append(m)
    return out


FIXTURE["messages"] = _marks_as_written_today(FIXTURE["messages"])


class Realm:
    """A read-only stand-in cut at message `upto`, topics named as they stood."""

    def __init__(self, upto: int, messages=None, failing=()):
        self.upto = upto
        self.all = [m for m in (messages or FIXTURE["messages"]) if m["id"] <= upto]
        self.failing = set(failing)
        self.calls = []

    def _live(self, channel, topic):
        resolved = False
        for m in self.all:
            if m["channel"] == channel and m["topic"] == topic and m["sender_realm_str"]:
                resolved = "marked this topic as resolved" in m["content"]
        return f"{RESOLVED_TOPIC_PREFIX}{topic}" if resolved else topic

    def _shape(self, m):
        return {
            "id": m["id"], "type": "stream", "display_recipient": m["channel"],
            "subject": self._live(m["channel"], m["topic"]), "sender_id": m["sender_id"],
            "sender_full_name": m["sender_full_name"], "sender_realm_str": m["sender_realm_str"],
            "timestamp": m["timestamp"], "content": m["content"],
        }

    def message(self, message_id, strict=False):
        self.calls.append(("message", message_id))
        for m in self.all:
            if m["id"] == message_id:
                return self._shape(m)
        return None

    def topic_history(self, channel, topic, num_before=50):
        self.calls.append(("history", channel, topic))
        if (channel, topic) in self.failing:
            raise ZulipError("timed out")
        bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
        if self._live(channel, bare) != topic:
            return []
        return [self._shape(m) for m in self.all if m["channel"] == channel and m["topic"] == bare][-num_before:]

    def public_notes(self, tag, num_before=1000):
        self.calls.append(("notes", tag))
        marker = f"[selfnote][{tag}]"
        return [self._shape(m) for m in self.all if m["content"].startswith(marker)]


def node(result, topic_suffix):
    for n in result.nodes():
        if n.topic.endswith(topic_suffix):
            return n
    raise AssertionError(f"{topic_suffix} not in the tree")


ORIGIN = 8286
NOW_STALL = 1790171400  # 2026-09-23T13:50:00Z, five minutes into the 24-minute stall


def test_the_claimed_start_shows_as_a_task_nobody_started():
    """F2: at 13:50 Front had reported task 4 as passed on. The trace says
    what the topic says — nobody has posted there — and names it owed."""
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    task4 = node(result, "workrun-task4-m8298")
    assert task4.state == "not_started"
    assert task4.identity == "task 8298#4"
    assert all(not a.startswith("Front") for a in task4.requested_by)
    assert node(result, "workrun-task3-m8298").state == "done"
    owed = tracing.next_actions(result)
    assert any("workrun-task4-m8298" in line and "every task before it is finished" in line for line in owed)


def test_the_unstarted_last_task_is_owed_after_the_one_before_closes():
    """F3: at 14:11 task 4 was closed and task 5 had only its spec."""
    result = tracing.trace(Realm(8441), ORIGIN, now=NOW_STALL + 1260)
    assert node(result, "workrun-task4-m8298").state == "done"
    assert node(result, "workrun-task5-m8298").state == "not_started"
    assert any("workrun-task5-m8298" in line for line in tracing.next_actions(result))


def test_later_tasks_are_not_owed_while_an_earlier_one_is_open():
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    owed = " ".join(tracing.next_actions(result))
    assert "workrun-task5-m8298" not in owed


def test_a_task_being_worked_on_is_executing_not_owed():
    """13:44:00: Front's #8409 was acknowledged in task 3 and the supercoder
    was posting progress — work in flight, not a stall."""
    result = tracing.trace(Realm(8413), ORIGIN, now=NOW_STALL - 360)
    task3 = node(result, "workrun-task3-m8298")
    assert task3.state == "executing"
    assert "workrun-task3" not in " ".join(tracing.next_actions(result))


def test_the_twin_opened_by_mistake_is_visible_as_resolved_unfinished():
    """F1: `workplan-locations-2` was answered and then ✔'d with no state."""
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    twin = node(result, "workplan-locations-2")
    assert twin.topic.startswith(RESOLVED_TOPIC_PREFIX)
    assert "resolved" in twin.detail
    mission = node(result, "pj-protoprey/workplan-locations".split("/")[1])
    assert mission.identity.startswith("mission m8298")
    assert mission.note_state == "started"


def test_each_conversation_appears_once_under_its_most_specific_anchor():
    result = tracing.trace(Realm(8460), ORIGIN, now=NOW_STALL + 1800)
    root = result.root
    assert not any("workrun-" in child.topic for child in root.children)
    mission = next(child for child in root.children if child.identity.startswith("mission"))
    assert [child.identity for child in mission.children] == [f"task 8298#{n}" for n in range(1, 6)]
    task5 = mission.children[-1]
    assert any(a.startswith("Front") for a in task5.requested_by)
    assert any(a.startswith("autolab") for a in task5.requested_by)


def test_an_answer_that_asks_nothing_is_answered_not_awaiting_the_human():
    """Recorded before `ag.post.v1`: Front answered last and asked nothing
    explicitly. Until clearer_chat_ui step 2 that inference read as
    `awaiting_human`; every report ever posted matched it."""
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    assert result.root.state == "answered"
    assert "nothing is asked" in result.root.detail
    assert result.root.owner == "Front"


def test_an_unreadable_conversation_is_unobservable_never_not_started():
    realm = Realm(8425, failing={("work-m8298", "workrun-task4-m8298")})
    result = tracing.trace(realm, ORIGIN, now=NOW_STALL)
    task4 = node(result, "workrun-task4-m8298")
    assert task4.state == "unobservable"
    assert "workrun-task4" not in " ".join(tracing.next_actions(result))


def test_a_missing_origin_is_said_and_nothing_else_is_read():
    realm = Realm(8425)
    result = tracing.trace(realm, 1, now=NOW_STALL)
    assert result.root is None and "does not exist" in result.problem
    assert realm.calls == [("message", 1)]


def test_calls_are_counted_and_bounded():
    realm = Realm(8460)
    result = tracing.trace(realm, ORIGIN, now=NOW_STALL)
    assert result.calls == len(realm.calls)
    # one lookup, two note searches, one history per conversation (+ ✔ retries)
    assert result.calls <= 3 + 2 * len(list(result.nodes()))


def _conversation(owner_id=11, requester_id=15, *extra):
    base = [
        {"id": 10, "channel": "c", "topic": "t", "sender_id": requester_id, "sender_full_name": "Front",
         "sender_realm_str": "", "timestamp": 100, "content": "[selfnote][rootchat] front/front-x #9"},
        {"id": 11, "channel": "c", "topic": "t", "sender_id": requester_id, "sender_full_name": "Front",
         "sender_realm_str": "", "timestamp": 100, "content": "please do it"},
    ]
    return base + list(extra)


def test_a_post_nobody_acknowledged_is_queued():
    state, detail, *_ = tracing.classify(_conversation(), now=400)
    assert state == "queued"


def test_an_answer_the_requester_has_not_served_is_awaiting_delivery():
    messages = _conversation(
        11, 15,
        {"id": 12, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "",
         "timestamp": 110, "content": SWEEP_ACK},
        {"id": 13, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "",
         "timestamp": 200, "content": "@**Front** done, see above"},
    )
    home = [{"id": 5, "sender_id": 15, "content": "[selfnote][served] c/t 11"}]
    state, *_ = tracing.classify(messages, now=400, homes={15: ("front", "front-x")},
                                 home_messages={15: home}, here=("c", "t"))
    assert state == "awaiting_delivery"
    home.append({"id": 14, "sender_id": 15, "content": "[selfnote][served] c/t 13"})
    state, *_ = tracing.classify(messages, now=400, homes={15: ("front", "front-x")},
                                 home_messages={15: home}, here=("c", "t"))
    assert state == "awaiting_requester"


def test_a_failure_notice_after_the_request_is_failed():
    messages = _conversation(
        11, 15,
        {"id": 12, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "",
         "timestamp": 110, "content": SWEEP_ACK},
        {"id": 13, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "",
         "timestamp": 120, "content": "Please complete previous work (task 1 is open)"},
    )
    state, detail, *_ = tracing.classify(messages, now=400)
    assert state == "failed" and "previous work" in detail


def test_trace_lines_render_the_tree_and_the_owed_list():
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    text = "\n".join(tracing.trace_lines(result))
    assert "task 8298#4" in text and "NOT_STARTED" in text
    assert "owed now:" in text


def _task(*extra):
    base = [
        {"id": 1, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "", "timestamp": 100,
         "content": "[selfnote][task] 7#2"},
        {"id": 2, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "", "timestamp": 100,
         "content": "# Task 2 spec"},
    ]
    return base + [
        {"id": 3 + n, "sender_id": 11, "sender_full_name": "autolab", "sender_realm_str": "", "timestamp": 200 + n,
         "content": content}
        for n, content in enumerate(extra)
    ]


def test_a_task_its_owner_started_is_queued_then_executing_then_answered():
    """robust_workflow p1 step 3: autolab starts the next task itself."""
    start = "[selfnote][start] #50 for 15 Front"
    assert tracing.classify(_task("Starting task 2.", start), now=400)[0] == "queued"
    assert tracing.classify(_task("Starting task 2.", start, SWEEP_ACK), now=400)[0] == "executing"
    state, detail, *_ = tracing.classify(_task("Starting task 2.", start, SWEEP_ACK, "@**Front** done"), now=400)
    assert state == "awaiting_requester" and "started by autolab" in detail


def test_a_held_task_is_waiting_on_its_requester_not_owed():
    state, detail, *_ = tracing.classify(_task("[selfnote][state] held"), now=400)
    assert state == "awaiting_requester" and "held" in detail


# --- candidates (robust_workflow p1 step 4) --------------------------------------


def test_the_p3_stall_is_a_candidate_six_minutes_in():
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    found = tracing.stall_candidates(result, now=NOW_STALL)
    kinds = [(c.kind, c.topic.split("/")[-1]) for c in found]
    assert ("unstarted", "workrun-task4-m8298") in kinds
    unstarted = next(c for c in found if c.kind == "unstarted")
    assert unstarted.responsible.startswith("autolab") and not unstarted.judgment
    assert unstarted.key == tracing.stall_candidates(result, now=NOW_STALL + 600)[0].key, "stable across looks"


def test_nothing_is_a_candidate_while_the_task_runs_or_before_the_grace():
    running = tracing.trace(Realm(8413), ORIGIN, now=NOW_STALL - 360)
    assert not [c for c in tracing.stall_candidates(running, now=NOW_STALL - 360) if "task3" in c.topic]
    just_closed = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    closed_at = next(n for n in just_closed.nodes() if n.topic.endswith("task3-m8298")).last_activity
    assert not [c for c in tracing.stall_candidates(just_closed, now=closed_at + 60) if c.kind == "unstarted"]


def test_a_resolved_conversation_with_live_work_needs_judgment():
    result = tracing.trace(Realm(8425), ORIGIN, now=NOW_STALL)
    twin = [c for c in tracing.stall_candidates(result, now=NOW_STALL) if c.kind == "resolved_live"]
    assert twin and all(c.judgment for c in twin)
    assert "unresolve" in twin[0].next_action


def test_the_mirror_reader_traces_without_a_zulip_call(tmp_path):
    from agag.mirror import Mirror
    from agag.mirror.testing import FakeRealm

    realm = FakeRealm()
    realm.add_channel(3, "front")
    realm.add_channel(6, "pj-x")
    origin = realm.post("front", "front-a", "please build it", sender_id=8, sender_name="Developer")
    realm.post("front", "front-a", SWEEP_ACK, sender_id=15, sender_name="Front")
    realm.post("pj-x", "workplan-a", "[selfnote][rootchat] front/front-a", sender_id=15, sender_name="Front")
    realm.post("pj-x", "workplan-a", "Mission: build it", sender_id=15, sender_name="Front")
    mirror = Mirror.open(tmp_path / "zulip.env", tmp_path / "mirror", client_factory=realm.facet,
                         log=lambda line: None, start=True, resync_backoff=0.05)
    try:
        import time as _time
        deadline = _time.time() + 5
        while _time.time() < deadline and mirror.message(origin) is None:
            _time.sleep(0.05)
        before = realm.calls if hasattr(realm, "calls") else None
        result = tracing.trace(tracing.MirrorReader(mirror), origin)
        assert result.root is not None and result.root.state == "executing"
        assert [child.topic for child in result.root.children] == ["workplan-a"]
        assert result.root.children[0].state == "queued"
        if before is not None:
            assert realm.calls == before
    finally:
        mirror.stop()



def test_a_delivered_record_is_still_owed_until_the_requester_serves_it():
    """robust_workflow p1 N2: forge wrote `delivered` while Front's listener
    was down; the trace said done and nobody noticed for eleven minutes."""
    messages = _conversation(
        11, 15,
        {"id": 12, "sender_id": 11, "sender_full_name": "forge", "sender_realm_str": "",
         "timestamp": 110, "content": SWEEP_ACK},
        {"id": 13, "sender_id": 11, "sender_full_name": "forge", "sender_realm_str": "",
         "timestamp": 200, "content": "@**Front** result: files/x.zip"},
        {"id": 14, "sender_id": 11, "sender_full_name": "forge", "sender_realm_str": "",
         "timestamp": 200, "content": "[selfnote][state] delivered"},
    )
    home = [{"id": 5, "sender_id": 15, "content": "[selfnote][served] c/t 11"}]
    state, detail, *_ = tracing.classify(messages, now=600, homes={15: ("front", "front-x")},
                                         home_messages={15: home}, here=("c", "t"))
    assert state == "awaiting_delivery" and "delivered" in detail
    home.append({"id": 15, "sender_id": 15, "content": "[selfnote][served] c/t 13"})
    assert tracing.classify(messages, now=600, homes={15: ("front", "front-x")},
                            home_messages={15: home}, here=("c", "t"))[0] == "done"
