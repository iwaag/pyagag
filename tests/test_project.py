"""`agproject` (sage p2 step 1): open, inspect and continue a project or study.

Pinned: a new study gets its channel (owners, the routine runner, the
workspace agent, the caller and the board reader), its plan and a setup
request carrying a machine-readable `ag-setup` block, anchored to the
caller's conversation; a second `open` writes nothing; an `open` after a
failure half way does only what is missing; an existing study made by hand
is inspected and completed without a second channel, document or request;
`status` says pending until autolab's answer names the repository and
commit, and ready afterwards.
"""

from __future__ import annotations

import io

import pytest

from agag import project
from project_realm import ARCHSAGE, AUTOLAB, DEV, FRONT, PROVISIONER, READER, Realm


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.delenv("AGAG_BOARD_READER", raising=False)
    monkeypatch.setenv("AGAG_GITEA_URL", "")
    monkeypatch.setenv("AGENTCHAT_HOME", "archsage-agstudio1/study-aquaculture")
    monkeypatch.setenv("AGENTCHAT_HOME_ANCHOR", "900")


def run(realm, argv, tmp_path, *, text="# Aquaculture\n\nQuestions: …\n"):
    doc = tmp_path / "RESEARCHPLAN.md"
    doc.write_text(text, encoding="utf-8")
    argv = [a if a != "DOC" else str(doc) for a in argv]
    out, err = io.StringIO(), io.StringIO()
    code = project.run(argv, client=realm.client(ARCHSAGE), admin=realm.client(PROVISIONER), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_a_new_study_is_created_with_every_member_and_a_structured_setup_request(tmp_path):
    realm = Realm()
    code, out, err = run(realm, ["open", "aquaculture", "--kind", "study", "--doc", "DOC", "--about", "fish farming"], tmp_path)
    assert code == 0, err
    channel = realm.channels["pj-aquaculture"]
    assert channel["subscribers"] >= {DEV, FRONT, AUTOLAB, ARCHSAGE, READER}
    assert "[AUTO] project: aquaculture; study" in channel["description"]
    assert channel["folder_id"] is not None
    (plan,) = realm.topic("pj-aquaculture", "researchplan-aquaculture")
    assert plan["sender_id"] == ARCHSAGE and plan["content"].startswith("# Aquaculture")
    note, request = realm.topic("pj-aquaculture", "workplan-setup-aquaculture")
    assert note["content"] == "[selfnote][rootchat] archsage-agstudio1/study-aquaculture #900"
    block = project.parse_setup(request["content"])
    assert block["pattern"] == "study" and block["slug"] == "aquaculture" and block["knowledge"] == "main"
    assert block["document"] == f"researchplan-aquaculture #{plan['id']}" and block["about"] == "fish farming"
    assert "setup only" in request["content"] and "workrun-" not in request["content"]
    assert "state: setup-pending" in out and "end this run" in out


def test_a_second_open_writes_nothing(tmp_path):
    realm = Realm()
    run(realm, ["open", "aquaculture", "--kind", "study", "--doc", "DOC"], tmp_path)
    count = len(realm.messages)
    code, out, _ = run(realm, ["open", "aquaculture", "--kind", "study", "--doc", "DOC"], tmp_path, text="# other\n")
    assert code == 0 and len(realm.messages) == count
    assert "nothing was missing" in out and "is kept" in out


def test_an_open_that_failed_half_way_is_continued_without_duplicates(tmp_path):
    realm = Realm()
    realm.fail_after["send"] = 1  # the plan is posted, the setup request is not
    code, _, err = run(realm, ["open", "aquaculture", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 1 and "injected failure" in err
    assert len(realm.topic("pj-aquaculture", "researchplan-aquaculture")) == 1
    assert realm.topic("pj-aquaculture", "workplan-setup-aquaculture") == []
    realm.fail_after.clear()
    code, out, err = run(realm, ["open", "aquaculture", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 0, err
    assert len(realm.topic("pj-aquaculture", "researchplan-aquaculture")) == 1
    assert len([m for m in realm.topic("pj-aquaculture", "workplan-setup-aquaculture")
                if not m["content"].startswith("[selfnote]")]) == 1
    assert [c for c in realm.client(PROVISIONER).calls if c[0] == "create"] == []
    assert "asked for the workspace" in out and "created #" not in out


def test_an_existing_hand_made_study_is_completed_not_replaced(tmp_path):
    realm = Realm()
    realm.add_channel("pj-worldtrend", [DEV, FRONT, AUTOLAB],
                      description="[AUTO] project: worldtrend; study; opened from argue x")
    realm.post(FRONT, "pj-worldtrend", "researchplan-worldtrend", "# World trend\n")
    realm.post(FRONT, "pj-worldtrend", "workplan-setup-worldtrend", "Please prepare the workspace … (prose)")
    realm.post(AUTOLAB, "pj-worldtrend", "workplan-setup-worldtrend", "@**Front** main/ exists at autodev/worldtrend")
    realm.resolve("pj-worldtrend", "workplan-setup-worldtrend")
    code, out, err = run(realm, ["open", "worldtrend", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 0, err
    assert realm.channels["pj-worldtrend"]["subscribers"] >= {ARCHSAGE, READER}
    assert len(realm.topic("pj-worldtrend", "researchplan-worldtrend")) == 1
    assert "subscribed [22, 24]" in out
    state = project.inspect_project("worldtrend", realm.client(ARCHSAGE), check_gitea=False)
    assert state.setup_id and not state.setup_structured and state.state == "answered"


def test_status_is_pending_until_the_established_line_and_ready_after(tmp_path):
    realm = Realm()
    run(realm, ["open", "aquaculture", "--kind", "study", "--doc", "DOC"], tmp_path)
    client = realm.client(ARCHSAGE)
    assert project.inspect_project("aquaculture", client, check_gitea=False).state == "setup-pending"
    # autolab's acknowledgement is not an answer, and the repository it made
    # before planning is not a finished setup (live probe #11500).
    realm.post(AUTOLAB, "pj-aquaculture", "workplan-setup-aquaculture", "Message received. Please wait for the reply.")
    import agag.project as module
    real = module.gitea_head
    module.gitea_head = lambda slug, **_: {"exists": True, "empty": False, "repository": "r", "revision": "abc1234"}
    try:
        assert project.inspect_project("aquaculture", client).state == "setup-pending"
    finally:
        module.gitea_head = real
    realm.post(AUTOLAB, "pj-aquaculture", "workplan-setup-aquaculture", "@**archsage** a question first?")
    assert project.inspect_project("aquaculture", client, check_gitea=False).state == "answered"
    line = project.established_line("http://gitea.example/autodev/aquaculture.git", "1a2b3c4")
    realm.post(AUTOLAB, "pj-aquaculture", "workplan-setup-aquaculture", f"@**archsage** done.\n\n{line}")
    realm.resolve("pj-aquaculture", "workplan-setup-aquaculture")
    state = project.inspect_project("aquaculture", client, check_gitea=False)
    assert state.state == "ready" and state.revision == "1a2b3c4"
    assert state.repository.endswith("autodev/aquaculture.git") and state.setup_home.startswith("archsage-agstudio1/")


def test_refusals(tmp_path):
    realm = Realm()
    realm.add_channel("pj-gone", [DEV], archived=True)
    code, _, err = run(realm, ["open", "gone", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 1 and "archived" in err
    code, _, err = run(realm, ["open", "Bad Slug", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 1 and "not a project slug" in err
    realm.add_channel("pj-thing", [DEV], description="[AUTO] project: thing; project; x")
    code, _, err = run(realm, ["open", "thing", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 1 and "is a project, not a study" in err


def test_parse_setup_needs_the_schema():
    assert project.parse_setup("```ag-setup\npattern: study\n```") is None
    assert project.parse_setup(project.setup_block({"pattern": "study", "slug": "x"}))["slug"] == "x"


def test_board_reader_can_be_named_or_switched_off(monkeypatch):
    realm = Realm()
    admin = realm.client(PROVISIONER)
    assert project.board_reader(admin) == READER
    assert project.board_reader(admin, {"AGAG_BOARD_READER": "none"}) is None
    assert project.board_reader(admin, {"AGAG_BOARD_READER": "77"}) == 77


def test_a_study_set_up_by_hand_is_ready_by_its_repository(tmp_path, monkeypatch):
    realm = Realm()
    realm.add_channel("pj-studyhand", [DEV, FRONT, AUTOLAB, READER], description="hand made")
    monkeypatch.setattr(project, "gitea_head", lambda slug, **_: {"repository": "http://g/autodev/studyhand.git",
                                                                  "exists": True, "revision": "abc1234", "empty": False})
    state = project.inspect_project("studyhand", realm.client(ARCHSAGE))
    assert state.state == "ready" and state.revision == "abc1234"
    code, out, err = run(realm, ["open", "studyhand", "--kind", "study", "--doc", "DOC"], tmp_path)
    assert code == 0, err
    assert realm.topic("pj-studyhand", "workplan-setup-studyhand") == [] and "no setup request" in out
    assert len(realm.topic("pj-studyhand", "researchplan-studyhand")) == 1
