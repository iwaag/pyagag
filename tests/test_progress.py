"""`agag.progress` and the trace facts it reads (progress_panel p1).

The fixture is three requests as the realm held them on 2026-09-27,
text shortened (`fixtures/progress_p1.json`):

- o11522 — a study established by archsage, its routine run, mission
  m11579 and its task, accepted from the desk while the run itself was
  never ended (step 1's G2), and the sage refresh said only in prose (G3);
- o11711 — round 2: mission m11741 whose task's run died with its serving
  open, held by a person since;
- o13116 — failsafe p4 T2: one mission, one task whose run was killed,
  resumed, shown, agreed and accepted.

Cut at a message id, it is the realm as a reader would have found it then.
"""

import json
from pathlib import Path

from agag import progress
from agag import trace as tracing
from agag.zulip import RESOLVED_TOPIC_PREFIX

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "progress_p1.json").read_text("utf-8"))
DEVELOPER = 8


class Realm:
    """A read-only stand-in cut at message `upto`, topics named as they stood."""

    def __init__(self, upto: int, messages=None):
        self.all = [m for m in (messages or FIXTURE["messages"]) if m["id"] <= upto]

    def _live(self, channel, topic):
        resolved = False
        for m in self.all:
            if m["channel"] == channel and m["topic"] == topic and m["sender_realm_str"]:
                resolved = "marked this topic as resolved" in m["content"]
        return f"{RESOLVED_TOPIC_PREFIX}{topic}" if resolved else topic

    def _shape(self, m):
        return {"id": m["id"], "type": "stream", "display_recipient": m["channel"],
                "subject": self._live(m["channel"], m["topic"]), "sender_id": m["sender_id"],
                "sender_full_name": m["sender_full_name"], "sender_realm_str": m["sender_realm_str"],
                "timestamp": m["timestamp"], "content": m["content"]}

    def message(self, message_id, strict=False):
        return next((self._shape(m) for m in self.all if m["id"] == message_id), None)

    def topic_history(self, channel, topic, num_before=50):
        bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
        if self._live(channel, bare) != topic:
            return []
        return [self._shape(m) for m in self.all if m["channel"] == channel and m["topic"] == bare][-num_before:]

    def public_notes(self, tag, num_before=1000):
        marker = f"[selfnote][{tag}]"
        return [self._shape(m) for m in self.all if m["content"].startswith(marker)]


def at(message_id):
    return next(m["timestamp"] for m in FIXTURE["messages"] if m["id"] == message_id)


def traced(origin, upto, now=None):
    return tracing.trace(Realm(upto), origin, now=now or at(upto) + 5)


def card(origin, upto, now=None, **kwargs):
    now = now or at(upto) + 5
    return progress.card(traced(origin, upto, now), now=now, viewer_id=DEVELOPER, **kwargs)


def unit(found, kind, label_part=""):
    stack = [found["root"]]
    while stack:
        u = stack.pop()
        if u["kind"] == kind and label_part in u["label"]:
            return u
        stack.extend(u["children"])
    raise AssertionError(f"no {kind} {label_part!r}")


def node(result, topic_part):
    return next(n for n in result.nodes() if topic_part in n.topic)


# --- the trace facts the panel needs (step 1's G1 and the finish record) -------------


def test_a_run_its_owner_serves_itself_is_executing_then_answered_never_not_started():
    # routinerun-20260926-2100: opened by Front at #11559, acked #11562.
    run = node(traced(11522, 11562), "routinerun-20260926-2100")
    assert run.state == "executing" and run.execution == "open"
    assert "served by its owner itself" in run.detail
    # After #11655 (a progress reply, pre-failsafe: no `end=`) it is still
    # the serving acked at #11649 — open, and the state says so.
    run = node(traced(11522, 11657), "routinerun-20260926-2100")
    assert run.state == "executing" and run.ack == 11649


def test_a_routine_runs_finish_block_is_its_end_record():
    messages = [m for m in FIXTURE["messages"] if m["id"] <= 11657]
    finish = ("Done.\n\n```ag-routinerun\n" + json.dumps({"schema": "ag.routinerun-finish.v1", "achieved": True,
                                                          "reason": "goal reached", "report": "r"}) + "\n```")
    messages.append({"id": 11660, "channel": "routine-study-aisvgs", "topic": "routinerun-20260926-2100",
                     "sender_id": 15, "sender_full_name": "Front", "sender_realm_str": "", "timestamp": at(11657) + 60,
                     "content": finish})
    result = tracing.trace(Realm(11660, messages), 11522, now=at(11657) + 90)
    run = node(result, "routinerun-20260926-2100")
    assert (run.state, run.note_state) == ("done", "finished")
    assert run.records[-1]["tag"] == "finish" and run.records[-1]["value"] == "achieved"
    not_achieved = finish.replace('"achieved": true', '"achieved": false')
    messages[-1] = {**messages[-1], "content": not_achieved}
    run = node(tracing.trace(Realm(11660, messages), 11522, now=at(11657) + 90), "routinerun-20260926-2100")
    assert (run.state, run.note_state) == ("done", "ended")


def test_record_notes_are_carried_per_conversation():
    plan = node(traced(13116, 13189), "workplan-failsafe-p4-t2")
    tags = [r["tag"] for r in plan.records]
    assert tags.count("doc") == 1 and "acceptance" in tags and tags[-1] == "state"
    task = node(traced(13116, 13189), "workrun-task1-m13123")
    assert [r["value"].split()[0] for r in task.records if r["tag"] == "change"] == ["checkpoint", "accepted",
                                                                                    "integrated"]


# --- transitions of one request (o13116) ------------------------------------------------


def test_the_request_is_working_before_any_plan_exists():
    found = card(13116, 13120)
    assert found["state"] == "working"
    workplan = unit(found, "conversation", "workplan-failsafe-p4-t2")
    assert workplan["display"]["state"] == "working"
    assert workplan["execution"]["evidence"] == "conversation"


def test_a_mission_with_no_task_yet_is_planning_without_a_percentage():
    found = card(13116, 13125)
    plan = unit(found, "plan", "m13123")
    assert plan["display"]["state"] == "planning"
    assert plan["meter"]["known"] is False and plan["meter"]["total"] is None


def test_tasks_become_the_meter_once_they_exist():
    found = card(13116, 13136)
    plan = unit(found, "plan", "m13123")
    assert plan["meter"]["known"] and plan["meter"]["total"] == 1 and plan["meter"]["completed"] == 0
    task = unit(found, "task", "13123#1")
    assert task["display"]["state"] == "working"
    assert task["run"]["activity"] == "claimed" and task["run"]["determinate"] is None
    assert plan["meter"]["working"] == 1


def test_an_open_serving_with_no_sign_of_work_is_unknown_not_working():
    found = card(13116, 13136, now=at(13136) + progress.WORK_QUIET + 10)
    task = unit(found, "task", "13123#1")
    assert task["display"]["state"] == "unknown"
    assert task["run"]["activity"] == "unknown"


def test_a_health_check_of_this_serving_decides_over_the_conversation():
    result = traced(13116, 13136)
    task = node(result, "workrun-task1-m13123")
    now = at(13136) + 30
    stopped = {"verdict": "stopped", "why": "process exited", "observed_at": now - 5, "subject": {"ack": task.ack}}
    found = progress.card(result, now=now, health={task.anchor: stopped}, viewer_id=DEVELOPER)
    assert unit(found, "task")["display"]["state"] == "stopped" and found["state"] == "stopped"
    running = {"verdict": "running", "why": "work 3 s ago", "observed_at": now - 5, "subject": {"ack": task.ack},
               "progress": {"last_work": "tool Bash", "last_work_at": now - 3}}
    found = progress.card(result, now=now, health={task.anchor: running}, viewer_id=DEVELOPER)
    got = unit(found, "task")
    assert got["display"]["state"] == "working" and got["execution"]["evidence"] == "confirmed"
    assert got["run"]["activity"] == "active" and got["run"]["action"] == "tool Bash"
    waiting = {"verdict": "waiting", "why": "a tool call", "observed_at": now - 5, "subject": {"ack": task.ack},
               "wait": {"kind": "tool", "name": "Bash", "detail": "pytest -q", "since": now - 200}}
    got = unit(progress.card(result, now=now, health={task.anchor: waiting}, viewer_id=DEVELOPER), "task")
    assert got["display"]["state"] == "waiting" and "pytest" in got["display"]["reason"]
    assert got["run"]["activity"] == "waiting" and got["run"]["action_age"] == 200


def test_a_stale_check_never_animates_and_another_servings_check_is_not_applied():
    result = traced(13116, 13136)
    task = node(result, "workrun-task1-m13123")
    now = at(13136) + 30
    old = {"verdict": "running", "why": "x", "observed_at": now - progress.HEALTH_FRESH - 60,
           "subject": {"ack": task.ack}}
    got = unit(progress.card(result, now=now, health={task.anchor: old}, viewer_id=DEVELOPER), "task")
    assert got["execution"]["evidence"] == "stale" and got["display"]["state"] == "unknown"
    assert got["run"]["activity"] == "unknown"
    other = {"verdict": "stopped", "why": "x", "observed_at": now, "subject": {"ack": task.ack + 1}}
    got = unit(progress.card(result, now=now, health={task.anchor: other}, viewer_id=DEVELOPER), "task")
    assert got["execution"]["health"] is None and "not this one" in got["execution"]["health_note"]
    assert got["display"]["state"] == "working"


def test_a_shown_result_waits_for_agreement_and_is_its_own_segment():
    found = card(13116, 13164)
    task = unit(found, "task", "13123#1")
    assert task["awaiting_agreement"] and task["display"]["state"] == "waiting"
    plan = unit(found, "plan", "m13123")
    assert plan["meter"]["awaiting_agreement"] == 1 and plan["meter"]["segments"] == ["awaiting_agreement"]


def test_an_agreed_task_is_not_a_completed_request_until_the_plan_is_accepted():
    found = card(13116, 13178)
    plan = unit(found, "plan", "m13123")
    assert plan["meter"]["completed"] == 1
    stages = {s["stage"]: s["status"] for s in found["stages"]}
    assert stages == {"tasks_agreed": "done", "plan_accepted": "pending"}
    assert found["state"] != "completed"


def test_the_accepted_request_is_completed():
    found = card(13116, 13189)
    assert found["state"] == "completed"
    assert all(s["status"] == "done" for s in found["stages"])


# --- a study: stages kept until their records (o11522) ------------------------------------


def test_a_study_is_not_complete_while_its_run_is_unended_and_its_sage_refresh_unrecorded():
    found = card(11522, 11708)
    stages = {s["stage"]: s["status"] for s in found["stages"]}
    assert stages == {"tasks_agreed": "done", "plan_accepted": "done", "run_ended": "pending",
                      "report_delivered": "pending", "knowledge_refreshed": "pending"}
    assert found["state"] != "completed"
    run = unit(found, "routine_run")
    assert run["display"]["state"] == "unknown"  # an open serving with no sign of work for 30 min


def test_a_recorded_sage_refresh_after_the_acceptance_satisfies_the_stage():
    messages = [m for m in FIXTURE["messages"] if m["id"] <= 11708]
    messages.append({"id": 11709, "channel": "archsage-agstudio1", "topic": "study-aisvgs", "sender_id": 24,
                     "sender_full_name": "archsage", "sender_realm_str": "", "timestamp": at(11708) + 10,
                     "content": "[selfnote][sagesync] aisvgs 13e0d6e project=aisvgs findings=12"})
    result = tracing.trace(Realm(11709, messages), 11522, now=at(11708) + 20)
    found = progress.card(result, now=at(11708) + 20, viewer_id=DEVELOPER)
    stage = next(s for s in found["stages"] if s["stage"] == "knowledge_refreshed")
    assert stage["status"] == "done" and stage["evidence"] == 11709


def test_a_held_request_waits_for_a_person_whatever_its_conversations_claim():
    result = traced(11711, 11770)
    found = progress.card(result, now=at(11770) + 3600, viewer_id=DEVELOPER,
                          recovery={result.root.anchor: {"held": True, "held_why": "the Developer decides"}})
    assert found["state"] == "awaiting_you" and found["next"] == "you"
    task = unit(found, "task", "11741#1")
    assert task["display"]["state"] == "unknown"


def test_an_unreadable_origin_is_unknown():
    found = progress.card(tracing.Trace(root=None, origin=1, observed_at=0, problem="gone"), now=10)
    assert found["state"] == "unknown" and found["root"] is None


def test_a_check_that_says_ended_beside_an_unended_serving_is_unknown_and_not_in_progress():
    """m11741's task, live on 2026-09-27: the probe says the run is over, the
    conversation shows no reply ending its serving. Neither is "in progress"."""
    result = traced(11711, 11770)
    task = node(result, "workrun-task1-m11741")
    now = at(11770) + 3600
    ended = {"verdict": "ended", "why": "the run is over and its serving is delivered", "observed_at": now - 5,
             "subject": {"ack": task.ack}}
    found = progress.card(result, now=now, health={task.anchor: ended}, viewer_id=DEVELOPER)
    got = unit(found, "task")
    assert got["display"]["state"] == "unknown" and "no post here ended" in got["display"]["reason"]
    meter = unit(found, "plan")["meter"]
    assert (meter["working"], meter["unknown"]) == (0, 1)
