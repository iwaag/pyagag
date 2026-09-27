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

from study_realm import ARCHSAGE, AUTOLAB, FRONT, Realm, Study, line


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
    assert "unheld" in {c.kind for c in tracing.stall_candidates(result, now=now)}
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
    realm.post("archsage-agstudio1", "study-growbox", f"[selfnote][rootchat] front/{setup.desk_topic} #{setup.origin}",
               FRONT)
    realm.post("archsage-agstudio1", "study-growbox", "@**archsage** please set it up.", FRONT)
    later.ask()
    later.open_run()
    # The later run asks for its refresh in the topic the guide names.
    realm.post("archsage-agstudio1", "study-growbox", "@**archsage** please refresh sage:growbox.", FRONT)
    home = effective_rootchat(realm.topic_history("archsage-agstudio1", "study-growbox", 400), FRONT)
    assert (home.channel, home.topic, home.anchor) == ("front", setup.desk_topic, setup.origin)


def test_baseline_the_refresh_stage_is_matched_by_project_and_time_only():
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
    assert {s["stage"]: s["status"] for s in found["stages"]}["knowledge_refreshed"] == "done"


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
