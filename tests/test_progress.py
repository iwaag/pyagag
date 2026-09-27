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


# --- step 4: transitions, identity and agreement with the existing readers ----------------


def _with(upto, *extra, base=None):
    """The fixture cut at `upto` (or `base`, rows already extended), plus
    made-up posts `(id, channel, topic, sender id, sender, content)`."""
    rows = [m for m in (base or FIXTURE["messages"]) if m["id"] <= upto]
    t = max(m["timestamp"] for m in rows)
    for ident, channel, topic, sender_id, sender, content in extra:
        t += 10
        rows.append({"id": ident, "channel": channel, "topic": topic, "sender_id": sender_id,
                     "sender_full_name": sender, "sender_realm_str": "", "timestamp": t, "content": content})
    return rows, t


AUTOLAB = ("autolab-agstudio1", 11)


def test_a_revised_plan_moves_the_denominator_and_says_so():
    # m13123 re-planned after task 1 closed: a second plan document and task 2.
    rows, t = _with(13178,
                    (13300, "pj-robustp1", "workplan-failsafe-p4-t2", 11, "autolab-agstudio1", "# revised plan"),
                    (13301, "pj-robustp1", "workplan-failsafe-p4-t2", 11, "autolab-agstudio1", "[selfnote][doc] 13300"),
                    (13302, "work-m13123", "workrun-task2-m13123", 11, "autolab-agstudio1", "[selfnote][task] 13123#2"),
                    (13303, "work-m13123", "workrun-task2-m13123", 11, "autolab-agstudio1",
                     "[selfnote][rootchat] pj-robustp1/workplan-failsafe-p4-t2 #13123"),
                    (13304, "work-m13123", "workrun-task2-m13123", 11, "autolab-agstudio1", "# task 2"))
    found = progress.card(tracing.trace(Realm(13304, rows), 13116, now=t + 5), now=t + 5, viewer_id=DEVELOPER)
    meter = unit(found, "plan")["meter"]
    assert (meter["completed"], meter["total"]) == (1, 2)
    assert [r["total"] for r in meter["revisions"]] == [1, 2] and meter["note"] == "plan revised 1×: 1 → 2 tasks"
    task2 = unit(found, "task", "13123#2")
    assert task2["display"]["state"] == "queued"
    # …and a task cancelled by a later revision leaves the denominator.
    rows, t = _with(13304, (13305, "work-m13123", "workrun-task2-m13123", 11, "autolab-agstudio1",
                            "[selfnote][state] cancelled"), base=rows)
    found = progress.card(tracing.trace(Realm(13305, rows), 13116, now=t + 5), now=t + 5, viewer_id=DEVELOPER)
    meter = unit(found, "plan")["meter"]
    assert (meter["completed"], meter["total"], meter["cancelled"]) == (1, 1, 1)
    assert unit(found, "task", "13123#2")["display"]["state"] == "cancelled"


def test_a_result_not_yet_taken_up_is_a_delivery_wait_and_owed():
    # #13161 names Front; Front's served mark is #13164.
    result = traced(13116, 13162)
    found = progress.card(result, now=at(13162) + 5, viewer_id=DEVELOPER)
    task = unit(found, "task", "13123#1")
    assert task["work"]["state"] == "awaiting_delivery"
    assert task["display"]["state"] == "waiting" and "not yet taken up by Front" in task["display"]["reason"]
    assert any("workrun-task1-m13123" in line for line in tracing.next_actions(result))
    # Once served, the same result waits for its agreement instead.
    task = unit(card(13116, 13164), "task", "13123#1")
    assert task["awaiting_agreement"] and "waiting for Front's agreement" in task["display"]["reason"]
    assert task["display"]["next"] == "Front"


def test_a_completed_card_owes_nothing_by_the_existing_readers():
    for origin, upto in ((13116, 13189),):
        result = traced(origin, upto)
        found = progress.card(result, now=at(upto) + 5, viewer_id=DEVELOPER)
        assert found["state"] == "completed"
        assert tracing.next_actions(result) == []
        assert tracing.stall_candidates(result, now=at(upto) + 7200) == []


def test_a_cancelled_mission_is_cancelled_and_leaves_no_pending_stage():
    rows, t = _with(13136,
                    (13400, "work-m13123", "workrun-task1-m13123", 11, "autolab-agstudio1", "[selfnote][state] cancelled"),
                    (13401, "pj-robustp1", "workplan-failsafe-p4-t2", 11, "autolab-agstudio1", "[selfnote][state] cancelled"),
                    (13402, "front", "front-failsafe-p4-t2", 15, "Front",
                     "@**Omni Agent** cancelled as asked.\n\n`ag-post intent=report`"))
    found = progress.card(tracing.trace(Realm(13402, rows), 13116, now=t + 5), now=t + 5, viewer_id=DEVELOPER)
    assert unit(found, "plan")["display"]["state"] == "cancelled"
    assert found["stages"] == []
    assert found["state"] == "cancelled" and "cancelled by its record" in found["reason"]


def test_identity_survives_a_rename_and_a_resolve_of_the_request():
    rows = [dict(m) for m in FIXTURE["messages"] if m["id"] <= 13189]
    for m in rows:
        if m["topic"] == "front-failsafe-p4-t2":
            m["topic"] = "front-failsafe-p4-t2-renamed"
    rows.append({"id": 13190, "channel": "front", "topic": "front-failsafe-p4-t2-renamed", "sender_id": 6,
                 "sender_full_name": "Notification Bot", "sender_realm_str": "zulipinternal",
                 "timestamp": at(13189) + 5, "content": "@_**Developer|8** has marked this topic as resolved."})
    result = tracing.trace(Realm(13190, rows), 13116, now=at(13189) + 10)
    found = progress.card(result, now=at(13189) + 10, viewer_id=DEVELOPER)
    assert found["origin"] == 13116 and found["anchor"] == 13116
    assert found["root"]["topic"] == f"{RESOLVED_TOPIC_PREFIX}front-failsafe-p4-t2-renamed"
    assert found["state"] == "completed"


def test_a_late_answer_after_completion_reopens_nothing_but_is_shown_owed():
    rows, t = _with(13189, (13500, "work-m13123", "workrun-task1-m13123", 11, "autolab-agstudio1",
                            "@**Front** one more note on the finished task.\n\n`ag-post intent=report`"))
    result = tracing.trace(Realm(13500, rows), 13116, now=t + 5)
    found = progress.card(result, now=t + 5, viewer_id=DEVELOPER)
    task = unit(found, "task", "13123#1")
    assert task["work"]["record"] == "accepted" and task["work"]["state"] == "awaiting_delivery"
    assert task["display"]["state"] == "waiting" and found["state"] == "waiting"
    assert unit(found, "plan")["meter"]["completed"] == 1  # the record still says agreed


def test_a_stop_then_a_new_serving_reads_the_new_serving_not_the_old_check():
    # #13149 Observer's stop request; #13155 the resumed serving's ack.
    stopped = traced(13116, 13149)
    task = node(stopped, "workrun-task1-m13123")
    now = at(13149) + 5
    report = {"verdict": "stopped", "why": "the process exited (-9)", "observed_at": now - 2,
              "subject": {"ack": task.ack}}
    recovery = {task.anchor: {"kind": "stopped", "state": "recovering", "open": True, "fact": "stopped"}}
    got = unit(progress.card(stopped, now=now, health={task.anchor: report}, recovery=recovery,
                             viewer_id=DEVELOPER), "task")
    assert got["display"]["state"] == "stopped"
    resumed = traced(13116, 13156)
    task2 = node(resumed, "workrun-task1-m13123")
    assert task2.ack == 13155 and task2.ack != task.ack
    now = at(13156) + 5
    rescued = {task2.anchor: {"kind": "stopped", "state": "rescued", "open": False}}
    got = unit(progress.card(resumed, now=now, health={task2.anchor: report}, recovery=rescued,
                             viewer_id=DEVELOPER), "task")
    # The old check is of #13135's serving: not applied; the resumed serving works.
    assert got["execution"]["health"] is None and got["display"]["state"] == "working"
    assert got["recovery"]["state"] == "rescued"


def test_two_requests_sharing_an_agent_keep_their_own_counts():
    a, b = card(11522, 11770), card(11711, 11770)
    assert a["origin"] != b["origin"]
    plans_a = {u["label"] for u in _all(a) if u["kind"] == "plan"}
    plans_b = {u["label"] for u in _all(b) if u["kind"] == "plan"}
    assert plans_a == {"mission m11579 (aisvgs)"} and plans_b == {"mission m11741 (aisvgs)"}
    assert unit(a, "plan")["meter"]["completed"] == 1 and unit(b, "plan")["meter"]["completed"] == 0


def _all(found):
    stack = [found["root"]]
    while stack:
        u = stack.pop()
        yield u
        stack.extend(u["children"])


def test_a_run_waiting_on_an_unfinished_unit_below_is_not_itself_silent():
    """Front's routine run reads `executing` (its own serving is open), and
    the task below it is the unit that is quiet: only the task is a `silent`
    candidate (Observer's reproduction suite caught the run being asked
    about beside a healthy task)."""
    result = traced(11711, 11770)
    later = at(11770) + tracing.THRESHOLDS["silent"] + 60
    kinds = {(c.kind, c.topic) for c in tracing.stall_candidates(result, now=later)}
    assert ("silent", "routinerun-20260926-2225") not in kinds
    assert ("silent", "workrun-task1-m11741") in kinds


def test_a_post_waiting_for_a_busy_agent_says_what_it_waits_behind():
    """Two requests at once are not two executions at once: autolab's listener
    serves one conversation at a time (progress_panel p1 step 5, live)."""
    rows, t = _with(13136, (13600, "pj-robustp1", "workplan-failsafe-p4-t2", 15, "Front",
                            "@**autolab-agstudio1** one more thing for the plan"))
    now = t + 5
    mine = progress.card(tracing.trace(Realm(13600, rows), 13116, now=now), now=now, viewer_id=DEVELOPER)
    other = progress.card(traced(11711, 11770, now), now=now, viewer_id=DEVELOPER)
    mine["topic"], other["topic"] = "front-failsafe-p4-t2", "front-desk-20260926-221323"
    progress.queue_behind([mine, other])
    plan = unit(mine, "plan", "m13123")
    assert plan["display"]["state"] == "queued"
    ahead = {row["label"] for row in plan["queue"]}
    # m11741's task is open but dead (unknown): not what autolab is serving.
    assert ahead == {"task 13123#1"}
    assert "serves one conversation at a time" in plan["display"]["reason"]


def test_a_card_is_about_the_deepest_unit_not_a_pass_through():
    found = card(13116, 13145)  # the plan's answer served (#13143), task 1 running
    assert found["state"] == "working"
    assert unit(found, "plan", "m13123")["passthrough"] is True
    assert found["focus"] == unit(found, "task", "13123#1")["anchor"]


TRIAL = json.loads((Path(__file__).parent / "fixtures" / "progress_p1_trial.json").read_text("utf-8"))


def test_an_answer_owed_to_a_runs_owner_is_held_by_that_owner_not_nobody():
    """Step 5's trial, #13375–#13382: worldtrend's routine run ended its
    serving saying the work goes on, while task 13312#1's close-out (#13373,
    naming Front) waited in Front's listener behind the growbox serving.
    Observer read "nobody holds it" and asked Front (#13384). Front's listener
    owed that serving: the run is held by its owner."""
    t_at = {m["id"]: m["timestamp"] for m in TRIAL["messages"]}
    result = tracing.trace(Realm(13381, TRIAL["messages"]), 13271, now=t_at[13381])
    task = node(result, "workrun-task1-m13312")
    assert task.state == "awaiting_delivery" and task.owed_to == ["Front"]
    run = node(result, "routinerun-20260927T092542Z")
    assert run.execution == "ended" and run.holder == "owner"
    later = t_at[13375] + tracing.THRESHOLDS["unheld"] + 60
    assert not [c for c in tracing.stall_candidates(result, now=later) if c.kind == "unheld"]
    # The delivery itself stays owed, and is found if it never happens.
    overdue = t_at[13373] + tracing.THRESHOLDS["undelivered"] + 5
    assert any(c.kind == "undelivered" for c in tracing.stall_candidates(result, now=overdue))


def test_a_finished_task_resolved_before_its_close_out_was_served_is_not_resolved_live():
    t_at = {m["id"]: m["timestamp"] for m in TRIAL["messages"]}
    result = tracing.trace(Realm(13381, TRIAL["messages"]), 13271, now=t_at[13381])
    task = node(result, "workrun-task1-m13312")
    assert task.topic.startswith(RESOLVED_TOPIC_PREFIX) and task.note_state == "completed"
    kinds = {c.kind for c in tracing.stall_candidates(result, now=t_at[13374] + 120) if c.anchor == task.anchor}
    assert "resolved_live" not in kinds


def test_a_question_to_a_person_in_the_request_is_the_cards_reason():
    t_at = {m["id"]: m["timestamp"] for m in TRIAL["messages"] if True}
    rows = TRIAL["messages"]
    upto = max(i for i in t_at if i <= 13486)
    result = tracing.trace(Realm(upto, rows), 13270, now=t_at[upto] + 5)
    from agag.outstanding import read_requests
    from agag.agent import is_ack

    history = [Realm(upto, rows)._shape(m) for m in rows if m["id"] <= upto and m["topic"] == "front-desk-20260927-pp1-growbox"]
    pending = [{"id": r.id, "to": r.to, "to_name": r.to_name, "ask": r.ask}
               for r in read_requests(history, is_ack=is_ack).pending]
    assert pending and pending[-1]["to"] == 9
    found = progress.card(result, now=t_at[upto] + 5, viewer_id=DEVELOPER, pending=pending)
    assert found["state"] == "waiting" and found["reason"].startswith(f"#{pending[0]['id']} asks")
