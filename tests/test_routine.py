"""`agroutine` (sage p2 step 1): register a routine and keep its guide.

Pinned: `create` makes `#routine-<name>` in the `routine` folder with the
exact description, the owners, the routine runner and the board reader,
and posts the guide as the caller, read back; a repeat posts nothing; a
different guide over an existing one is refused in favour of `update`;
`update` posts a complete new version; a truncated guide is a failure;
nothing opens a run.
"""

from __future__ import annotations

import io

import pytest

from agag import routine
from project_realm import ARCHSAGE, DEV, FRONT, PROVISIONER, READER, Realm


@pytest.fixture(autouse=True)
def _environment(monkeypatch):
    monkeypatch.delenv("AGAG_BOARD_READER", raising=False)


def run(realm, argv, tmp_path, text="**Routine `study-aqua` — guide v1**\n\nIn #pj-aqua ask autolab for one mission.\n"):
    guide = tmp_path / "guide.md"
    guide.write_text(text, encoding="utf-8")
    argv = [a if a != "GUIDE" else str(guide) for a in argv]
    out, err = io.StringIO(), io.StringIO()
    code = routine.run(argv, client=realm.client(ARCHSAGE), admin=realm.client(PROVISIONER), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_create_makes_the_channel_and_posts_the_guide_read_back(tmp_path):
    realm = Realm()
    code, out, err = run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path)
    assert code == 0, err
    channel = realm.channels["routine-study-aqua"]
    assert channel["folder_id"] == 13 and channel["description"] == routine.channel_description("study-aqua")
    assert channel["subscribers"] >= {DEV, FRONT, READER, ARCHSAGE}
    (guide,) = realm.topic("routine-study-aqua", "guide")
    assert guide["sender_id"] == ARCHSAGE and "guide v1" in guide["content"]
    assert "read it back intact" in out and "nothing has been started" in out
    assert not any(m["subject"].startswith("routinerun-") for m in realm.messages)


def test_a_repeat_posts_nothing_and_a_different_guide_needs_update(tmp_path):
    realm = Realm()
    run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path)
    code, out, _ = run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path)
    assert code == 0 and "already this text" in out and len(realm.topic("routine-study-aqua", "guide")) == 1
    code, _, err = run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path, text="guide v2")
    assert code == 1 and "agroutine update" in err
    code, out, err = run(realm, ["update", "study-aqua", "--guide-file", "GUIDE"], tmp_path, text="guide v2 whole")
    assert code == 0, err
    assert [m["content"] for m in realm.topic("routine-study-aqua", "guide")][-1] == "guide v2 whole"


def test_a_channel_made_without_its_members_or_guide_is_completed(tmp_path):
    realm = Realm()
    realm.add_channel("routine-study-aqua", [DEV], description="old")
    code, out, err = run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path)
    assert code == 0, err
    channel = realm.channels["routine-study-aqua"]
    assert channel["subscribers"] >= {FRONT, READER} and channel["folder_id"] == 13
    assert channel["description"] == routine.channel_description("study-aqua")
    assert len(realm.topic("routine-study-aqua", "guide")) == 1


def test_a_truncated_guide_is_a_failure(tmp_path):
    realm = Realm()
    realm.truncate_over = 20
    code, out, _ = run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path)
    assert code == 1 and "truncated or altered" in out


def test_a_retired_routine_is_not_revived_by_a_post(tmp_path):
    realm = Realm()
    run(realm, ["create", "study-aqua", "--guide-file", "GUIDE"], tmp_path)
    realm.resolve("routine-study-aqua", "guide")
    code, _, err = run(realm, ["update", "study-aqua", "--guide-file", "GUIDE"], tmp_path, text="v9")
    assert code == 1 and "retired" in err
