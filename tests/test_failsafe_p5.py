"""failsafe p5: waiting correctly and finishing delegated studies.

The realms are synthetic (`study_realm`), shaped the way progress_panel
p1's trial B went: two study requests through Front at once, autolab's one
executor serving one of them at a time, archsage's refresh topic. Step 1
reproduces the gaps the plan names; later steps turn each reproduction into
the regression it fixes.
"""

from agag import progress
from agag import trace as tracing
from agag.selfnote import effective_rootchat

import pytest

from study_realm import ARCHSAGE, AUTOLAB, FRONT, OMNI, Realm, Study, line


def node(result, topic_part):
    return next(n for n in result.nodes() if topic_part in n.topic)


def two_studies():
    """growbox's task runs on autolab while worldtrend's start request waits
    behind it (trial B, #13702)."""
    realm = Realm()
    growbox, worldtrend = Study(realm, "growbox", "b1"), Study(realm, "worldtrend", "b1")
    for study in (growbox, worldtrend):
        study.ask()
        study.open_run()
        study.run_delegates()
    growbox.autolab_plans()
    worldtrend.autolab_plans()
    growbox.front_starts()
    growbox.autolab_starts()
    worldtrend.front_starts()  # queued: autolab is serving growbox's task
    growbox.task_works()
    return realm, growbox, worldtrend


# --- step 1: the baseline ---------------------------------------------------------------


def test_baseline_a_healthy_serial_queue_is_read_as_unacknowledged():
    realm, growbox, worldtrend = two_studies()
    now = realm.at(worldtrend.start_request) + 301
    result = tracing.trace(realm, worldtrend.origin, now=now)
    plan = node(result, worldtrend.plan_topic)
    assert plan.state == "queued"
    found = [c for c in tracing.stall_candidates(result, now=now) if c.kind == "unacknowledged"]
    assert [c.evidence[0] for c in found] == [worldtrend.start_request]
    # …while the same owner is serving the other request's task.
    other = tracing.trace(realm, growbox.origin, now=now)
    task = node(other, growbox.task_topic)
    assert task.execution == "open" and task.owner == plan.owner


def test_baseline_a_workplan_opened_beside_the_run_leaves_the_run_unheld():
    realm = Realm()
    study = Study(realm, "worldtrend", "b1", beside=True)
    study.ask()
    study.open_run()
    study.run_waits("Checked the workplan: only autolab's ack so far; waiting.")
    now = realm.at(study.run_entry) + 120
    result = tracing.trace(realm, study.origin, now=now)
    run, plan = node(result, study.run_topic), node(result, study.plan_topic)
    assert plan not in run.children and plan in result.root.children
    assert run.holder == "none"
    (unheld,) = [c for c in tracing.stall_candidates(result, now=now) if c.kind == "unheld"]
    # Step 4: the run is Front's own, so the way to resume it is a start of
    # its own, not a post (which serves nothing).
    assert "agrun continue" in unheld.next_action and "its own post there serves nothing" in unheld.next_action
    # Front's own "Resuming" post in its own run is its own speech: the run's
    # serving stays ended and nothing is owed to Front there.
    realm.post(study.run_channel, study.run_topic, "Resuming: waiting for autolab's task.\n\n"
               + line("progress"), FRONT)
    again = node(tracing.trace(realm, study.origin, now=now + 60), study.run_topic)
    assert again.execution == "ended" and again.holder == "none"


def test_baseline_the_fixed_refresh_topic_returns_to_the_first_request():
    realm = Realm()
    setup, later = Study(realm, "growbox", "setup"), Study(realm, "growbox", "b1")
    setup.ask("Set the growbox study up.")
    realm.post("archsage-agstudio1", "study-growbox", f"[selfnote][rootchat] front/{setup.desk_topic} #{setup.origin} rel=work",
               FRONT)
    realm.post("archsage-agstudio1", "study-growbox", "@**archsage** please set it up.", FRONT)
    later.ask()
    later.open_run()
    # The later run asks for its refresh in the topic the guide names.
    realm.post("archsage-agstudio1", "study-growbox", "@**archsage** please refresh sage:growbox.", FRONT)
    home = effective_rootchat(realm.topic_history("archsage-agstudio1", "study-growbox", 400), FRONT)
    assert (home.channel, home.topic, home.anchor) == ("front", setup.desk_topic, setup.origin)


def test_another_runs_refresh_of_another_revision_does_not_complete_this_run():
    realm = Realm()
    study = Study(realm, "growbox", "b1")
    study.ask()
    study.open_run()
    study.run_delegates()
    study.autolab_plans()
    study.front_starts()
    study.autolab_starts()
    study.task_shows()
    study.task_closes()
    realm.post(study.plan_channel, study.plan_topic, f"[selfnote][acceptance] #{study.agreed} by {FRONT} (Front)",
               AUTOLAB)
    done = realm.post(study.plan_channel, study.plan_topic, "[selfnote][state] done", AUTOLAB)
    now = realm.at(done) + 5
    result = tracing.trace(realm, study.origin, now=now)
    before = progress.card(result, now=now)
    assert {s["stage"]: s["status"] for s in before["stages"]}["knowledge_refreshed"] == "pending"
    elsewhere = [{"tag": "sagesync", "value": "growbox 0123456789ab project=growbox findings=1",
                  "id": done + 50, "at": now, "by": ARCHSAGE}]  # another run's refresh, another revision
    found = progress.card(result, now=now, syncs=elsewhere)
    # Step 1 found this "done" (matched by project and time). Step 5: the
    # record establishes nothing about this run's result, so it stays pending.
    assert {s["stage"]: s["status"] for s in found["stages"]}["knowledge_refreshed"] == "pending"
    theirs = [{**elsewhere[0], "value": f"growbox abc1234def56 project=growbox findings=1 includes=abc1234def5678"}]
    assert {s["stage"]: s["status"] for s in progress.card(result, now=now, syncs=theirs)["stages"]}[
        "knowledge_refreshed"] == "done"  # the revision establishes it, wherever it was recorded


# --- step 2: one reading of a queue, shared ------------------------------------------------


def test_the_conversations_say_the_queued_post_waits_behind_the_other_requests_task():
    from agag import waits

    realm, growbox, worldtrend = two_studies()
    now = realm.at(worldtrend.start_request) + 301
    results = [tracing.trace(realm, s.origin, now=now) for s in (growbox, worldtrend)]
    plan = node(results[1], worldtrend.plan_topic)
    wait = waits.from_conversation(plan, waits.open_servings(results, now), now)
    assert wait.state == "behind" and wait.evidence == "conversation" and wait.excused
    assert wait.post == worldtrend.start_request and wait.queued_at == realm.at(worldtrend.start_request)
    assert [row["topic"] for row in wait.ahead] == [growbox.task_topic]
    # A conversation-only excuse lapses: an open conversation is not health.
    late = waits.from_conversation(plan, waits.open_servings(results, now), realm.at(worldtrend.start_request)
                                   + waits.CONVERSATION_MAX + 1)
    assert late.state == "unknown" and not late.excused


def test_a_confirmed_check_decides_and_an_old_one_excuses_nothing():
    from agag import waits

    realm, growbox, worldtrend = two_studies()
    now = realm.at(worldtrend.start_request) + 301
    plan = node(tracing.trace(realm, worldtrend.origin, now=now), worldtrend.plan_topic)
    report = {"verdict": "queued", "observed_at": now, "why": "queued 301 s behind work-m/workrun",
              "queue": {"ahead": [{"channel": growbox.task_channel, "topic": growbox.task_topic, "ack": 1,
                                   "verdict": "running"}]}}
    assert waits.from_probe(plan, report, now).state == "behind"
    assert waits.from_probe(plan, report, now + waits.CONFIRMED_FRESH + 1).state == "unknown"
    stopped_ahead = {**report, "verdict": "unknown",
                     "queue": {"ahead": [{**report["queue"]["ahead"][0], "verdict": "stopped"}]}}
    assert waits.from_probe(plan, stopped_ahead, now).state == "blocked"
    idle = {"verdict": "stopped", "observed_at": now, "why": "the executor runs nothing", "queue": {"ahead": []}}
    assert waits.from_probe(plan, idle, now).state == "unserved"
    assert waits.from_probe(plan, None, now).state == "unknown"


def test_the_panel_names_what_the_post_waits_behind_with_its_evidence():
    realm, growbox, worldtrend = two_studies()
    now = realm.at(worldtrend.start_request) + 301
    cards = []
    for study in (growbox, worldtrend):
        found = progress.card(tracing.trace(realm, study.origin, now=now), now=now)
        found["topic"] = study.desk_topic
        cards.append(found)
    progress.queue_behind(cards, now=now)
    plan = next(u for u in progress._walk(cards[1]["root"]) if u["topic"] == worldtrend.plan_topic)
    assert plan["queue"]["state"] == "behind" and plan["queue"]["post"] == worldtrend.start_request
    assert f"of another request ({growbox.desk_topic})" in plan["display"]["reason"]


def test_recheck_of_a_queued_post_says_it_is_asked_not_stopped():
    realm, growbox, worldtrend = two_studies()
    checked = tracing.recheck(realm, worldtrend.request, worldtrend.start_request,
                              now=realm.at(worldtrend.start_request) + 301)
    assert checked.verdict == "asked" and checked.pending == [worldtrend.start_request]


# --- step 3: acceptance by whoever holds the decision ---------------------------------------


def closed_study(*, reserve=False):
    """A study run to its task's close: desk (Omni) → run (Front) →
    workplan → task shown, agreed by Front, closed (`+shown=`)."""
    realm = Realm()
    study = Study(realm, "growbox", "b1")
    study.ask("Run the study once; I will approve the result myself." if reserve else "Run the study once, small.")
    if reserve:
        from agag.acceptance import reservation_note

        realm.post("front", study.desk_topic, reservation_note(OMNI, "Omni Agent", study.origin), FRONT)
    study.open_run()
    study.run_delegates()
    study.autolab_plans()
    study.front_starts()
    study.autolab_starts()
    study.task_shows()
    study.task_closes()
    return realm, study


def test_the_runner_records_its_own_agreement_and_autolab_would_record_the_same():
    from agag.acceptance import accept_mission

    realm, study = closed_study()
    agreed = realm.post(study.plan_channel, study.plan_topic, "The task is in; that completes the mission.", FRONT)
    done = accept_mission(realm.speaking_as(FRONT), study.mission, evidence=agreed, resolve=False)
    assert (done.evidence, done.by_id, done.after) == (agreed, FRONT, study.shown)
    again = accept_mission(realm.speaking_as(AUTOLAB), study.mission, evidence=agreed, resolve=False)
    assert again.already and not again.written
    # The task agreement itself is evidence too: it follows the shown result.
    realm2, study2 = closed_study()
    done2 = accept_mission(realm2.speaking_as(AUTOLAB), study2.mission, evidence=study2.agreed, resolve=False)
    assert done2.by_id == FRONT


def test_the_origin_person_holds_the_decision_too_and_a_bystander_does_not():
    from agag.acceptance import AcceptanceRefused, accept_mission

    realm, study = closed_study()
    ok = realm.post("front", study.desk_topic, "Accepted.", OMNI)
    assert accept_mission(realm.speaking_as(FRONT), study.mission, evidence=ok, resolve=False).by_id == OMNI
    realm, study = closed_study()
    other = realm.post("front", study.desk_topic, "Looks done to me.", 8)
    with pytest.raises(AcceptanceRefused, match="does not hold"):
        accept_mission(realm.speaking_as(FRONT), study.mission, evidence=other)


def test_the_initial_request_or_an_early_agreement_accepts_nothing():
    from agag.acceptance import AcceptanceRefused, accept_mission

    realm, study = closed_study()
    with pytest.raises(AcceptanceRefused, match="older than"):
        accept_mission(realm.speaking_as(FRONT), study.mission, evidence=study.origin)
    with pytest.raises(AcceptanceRefused, match="before the result"):
        accept_mission(realm.speaking_as(FRONT), study.mission, evidence=study.start_request)
    with pytest.raises(AcceptanceRefused, match="own agent"):
        accept_mission(realm.speaking_as(FRONT), study.mission, evidence=study.closed)


def test_evidence_from_another_conversation_is_refused():
    from agag.acceptance import AcceptanceRefused, accept_mission

    realm, study = closed_study()
    elsewhere = realm.post("front", "front-desk-unrelated", "That completes the mission.", FRONT)
    with pytest.raises(AcceptanceRefused, match="not m"):
        accept_mission(realm.speaking_as(FRONT), study.mission, evidence=elsewhere)


def test_a_reserved_approval_waits_for_that_person():
    from agag.acceptance import AcceptanceRefused, accept_mission

    realm, study = closed_study(reserve=True)
    agreed = realm.post(study.plan_channel, study.plan_topic, "That completes the mission.", FRONT)
    with pytest.raises(AcceptanceRefused, match="reserved"):
        accept_mission(realm.speaking_as(AUTOLAB), study.mission, evidence=agreed)
    theirs = realm.post("front", study.desk_topic, "Yes, I accept it.", OMNI)
    done = accept_mission(realm.speaking_as(FRONT), study.mission, evidence=theirs, resolve=False)
    assert done.by_id == OMNI


def test_agentchat_reserve_records_the_person_s_own_words_where_they_said_them(monkeypatch, capsys):
    from agag import chat
    from agag.acceptance import parse_reservation

    realm = Realm()
    study = Study(realm, "growbox", "b1")
    study.ask("Run it; I approve the result myself.")
    monkeypatch.setattr(chat, "client_from_environment", lambda *a, **k: realm)
    monkeypatch.setenv("AGENTCHAT_HOME", f"front/{study.desk_topic}")
    monkeypatch.setenv("AGENTCHAT_HOME_ANCHOR", str(study.origin))
    assert chat.main(["reserve", "--evidence", str(study.origin)]) == 0
    (note,) = [r["content"] for r in realm.rows if parse_reservation(r["content"])]
    assert parse_reservation(note) == (OMNI, "Omni Agent", study.origin)
    assert chat.main(["reserve", "--evidence", str(study.desk_ack)]) == 1  # Front's own post


# --- step 5: the refresh returns to the run that asked ------------------------------------


def _send(monkeypatch, realm, home, anchor, channel, topic, text):
    from agag import chat

    realm.me = FRONT
    monkeypatch.setattr(chat, "client_from_environment", lambda *a, **k: realm)
    monkeypatch.setattr(chat, "join_and_record", lambda client, channel, topic, out: False)
    monkeypatch.setattr(chat, "refuse_resolved", lambda client, channel, topic: None)
    chat._ANCHORED.clear()
    monkeypatch.setenv("AGENTCHAT_HOME", home)
    monkeypatch.setenv("AGENTCHAT_HOME_ANCHOR", str(anchor))
    return chat.main(["send", channel, topic, text])


def test_a_topic_that_returns_to_another_request_is_refused_and_a_new_one_is_not(monkeypatch, capsys):
    realm = Realm()
    setup, later = Study(realm, "growbox", "setup"), Study(realm, "growbox", "b1")
    setup.ask("Set the growbox study up.")
    realm.post("archsage-agstudio1", "study-growbox",
               f"[selfnote][rootchat] front/{setup.desk_topic} #{setup.origin} rel=work", FRONT)
    realm.post("archsage-agstudio1", "study-growbox", "@**archsage** please set it up.", FRONT)
    later.ask()
    later.open_run()
    run_home = f"{later.run_channel}/{later.run_topic}"
    before = len(realm.topic_history("archsage-agstudio1", "study-growbox", 400))
    assert _send(monkeypatch, realm, run_home, later.run_open, "archsage-agstudio1", "study-growbox",
                 "@**archsage** please refresh sage:growbox") == 1
    assert "belongs to another request" in capsys.readouterr().err
    assert len(realm.topic_history("archsage-agstudio1", "study-growbox", 400)) == before
    # The refusal is left on record in the run ([opfail]), where it happened.
    assert realm.rows[-1]["content"].startswith("[selfnote][opfail]") and realm.rows[-1]["topic"] == later.run_topic
    assert _send(monkeypatch, realm, run_home, later.run_open, "archsage-agstudio1", "refresh-growbox-b1",
                 "@**archsage** please refresh sage:growbox") == 0
    home = effective_rootchat(realm.topic_history("archsage-agstudio1", "refresh-growbox-b1", 50), FRONT)
    assert (home.channel, home.topic, home.anchor) == (later.run_channel, later.run_topic, later.run_open)


def test_the_same_request_may_post_from_its_desk_into_work_its_run_opened(monkeypatch):
    realm = Realm()
    study = Study(realm, "growbox", "b1")
    study.ask()
    study.open_run()
    study.run_delegates()
    assert _send(monkeypatch, realm, f"front/{study.desk_topic}", study.origin, study.plan_channel,
                 study.plan_topic, "One more detail for the plan.") == 0


def _accepted_study(realm, stamp, sha):
    study = Study(realm, "growbox", stamp)
    study.ask()
    study.open_run()
    study.run_delegates()
    study.autolab_plans()
    study.front_starts()
    study.autolab_starts()
    study.task_shows()
    study.task_closes(sha=sha)
    realm.post(study.plan_channel, study.plan_topic, f"[selfnote][acceptance] #{study.agreed} by {FRONT} (Front)",
               AUTOLAB)
    realm.post(study.plan_channel, study.plan_topic, "[selfnote][state] done", AUTOLAB)
    return study


def test_two_runs_of_the_same_study_each_need_their_own_refresh_in_any_order():
    realm = Realm()
    first, second = _accepted_study(realm, "r1", "1111111aaaaaaa"), _accepted_study(realm, "r2", "2222222bbbbbbb")
    now = realm.clock + 5

    def stage(study, *values):
        syncs = [{"tag": "sagesync", "value": v, "id": 5000 + i, "at": now, "by": ARCHSAGE} for i, v in enumerate(values)]
        found = progress.card(tracing.trace(realm, study.origin, now=now), now=now, syncs=syncs)
        return {s["stage"]: s["status"] for s in found["stages"]}["knowledge_refreshed"]

    for_second = (f"growbox 2222222bbbbb project=growbox findings=2 for={second.run_channel}/{second.run_topic}"
                  f"#{second.run_open} includes=2222222bbbbbbb")
    for_first = (f"growbox 1111111aaaaa project=growbox findings=1 for={first.run_channel}/{first.run_topic}"
                 f"#{first.run_open} includes=1111111aaaaaaa")
    # The second run's refresh arrives first: it completes the second, not the first.
    assert stage(second, for_second) == "done" and stage(first, for_second) == "pending"
    assert stage(first, for_second, for_first) == "done"
    assert stage(second, for_first) == "pending"
    # A refresh for this run that does not hold its result is not the refresh.
    missed = for_first.replace("includes=", "missing=")
    assert stage(first, missed) == "pending"


def test_a_name_that_is_part_of_an_account_s_name_is_suggested():
    from agag import chat

    class Users:
        def users(self):
            return [{"user_id": 24, "full_name": "archsage", "is_active": True},
                    {"user_id": 15, "full_name": "Front", "is_active": True}]

    with pytest.raises(chat.AgentChatError, match=r"did you mean 'archsage' \(24\)"):
        chat.resolve_user(Users(), "archsage-agstudio1")
    assert chat.resolve_user(Users(), "archsage") == 24


def test_a_task_not_started_is_not_said_to_wait_behind_the_executor():
    realm, growbox, worldtrend = two_studies()
    worldtrend.autolab_starts()  # its task opened and not served yet; growbox's task still open
    now = realm.clock + 5
    cards = []
    for study in (growbox, worldtrend):
        found = progress.card(tracing.trace(realm, study.origin, now=now), now=now)
        found["topic"] = study.desk_topic
        cards.append(found)
    progress.queue_behind(cards, now=now)
    for unit in progress._walk(cards[1]["root"]):
        if unit["work"]["state"] == "not_started":
            assert "queue" not in unit


def test_a_topic_spelled_as_channel_slash_topic_is_refused(monkeypatch, capsys):
    realm = Realm()
    study = Study(realm, "growbox", "e1")
    study.ask()
    study.open_run()
    realm.stream_id = lambda name: {"work-m1": 7, "pj-growbox": 6}[name]
    assert _send(monkeypatch, realm, f"{study.run_channel}/{study.run_topic}", study.run_open, "pj-growbox",
                 "work-m1/workrun-task1-m1", "Agreed.") == 1
    assert "did you mean `#work-m1 > workrun-task1-m1`" in capsys.readouterr().err
    assert not realm.topic_history("pj-growbox", "work-m1/workrun-task1-m1", 10)
    # A slash that names no channel is an ordinary topic.
    assert _send(monkeypatch, realm, f"{study.run_channel}/{study.run_topic}", study.run_open, "pj-growbox",
                 "notes/2026", "a note") == 0


def test_recheck_of_a_conversation_nobody_serves_says_so():
    realm = Realm()
    stray = realm.post("pj-growbox", "work-m1/workrun-task1-m1", "Agreed — please close the task.", FRONT)
    checked = tracing.recheck(realm, stray, stray, now=realm.clock + 600)
    assert checked.verdict == "unowned" and "nobody to serve it" in checked.detail


def test_an_unmentioned_first_request_is_queued_for_the_agent_whose_roster_serves_it():
    """Trial G: Front's plan request named nobody, so the trace knew no owner
    and the queue could not be read; autolab serves `workplan-` by its roster."""
    from types import SimpleNamespace

    from agag.intro import Roster, roster_block, roster_owner

    autolab = Roster("autolab-agstudio1", "agautolab", "autolab-agstudio1", 11, "autolab-agstudio1",
                     ("workplan-", "workrun-"))
    front = Roster("front-agstudio1", "agfront", "Front", 15, "front-agstudio1", ("front-", "routinerun-"))
    assert roster_owner([autolab, front], "pj-x", "workplan-g3") == "autolab-agstudio1"
    assert roster_owner([autolab, front], "pj-x", "notes") == ""
    realm = Realm()
    study = Study(realm, "growbox", "g3")
    study.ask()
    realm.post("pj-growbox", "workplan-g3", f"[selfnote][rootchat] front/{study.desk_topic} #{study.origin} rel=work", FRONT)
    post = realm.post("pj-growbox", "workplan-g3", "Mission request: one task.", FRONT)

    class Board(Realm):
        """The realm read through the mirror reader's roster lookup."""
        def roster_owner(self, channel, topic):
            return roster_owner([autolab, front], channel, topic)

    board = Board(realm.rows)
    plan = node(tracing.trace(board, study.origin, now=realm.at(post) + 301), "workplan-g3")
    assert plan.state == "queued" and plan.owner == "autolab-agstudio1"
    assert "unacknowledged" in {c.kind for c in tracing.stall_candidates(
        tracing.trace(board, study.origin, now=realm.at(post) + 301), now=realm.at(post) + 301)}

    # The mirror reader reads the rosters off the `#agents` board.
    intro = "# autolab\n\n" + roster_block(autolab)
    fake = SimpleNamespace(
        topics=lambda channel: [SimpleNamespace(name="intro-autolab-agstudio1", live_name="intro-autolab-agstudio1",
                                                resolved=False)] if channel == "agents" else [],
        history=lambda channel, topic, num_before=1, across_resolve=False: [{"content": intro}])
    assert tracing.MirrorReader(fake).roster_owner("pj-y", "workplan-z") == "autolab-agstudio1"
