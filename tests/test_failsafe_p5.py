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
