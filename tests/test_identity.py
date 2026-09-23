"""robust_workflow p2 step 2: a conversation is what its message ids say.

The trace (off a mirror, as the Observer reads it) and the served marks
follow a conversation through an ordinary rename, a resolve, a retirement
with a replacement, a deleted anchor and a reuse of the old name — and a
reused name never inherits what belonged to the conversation that had it.
"""

from __future__ import annotations

import time

import pytest

from agag.identity import bare, served_key, whereabouts
from agag.mirror import Mirror
from agag.mirror.testing import FakeRealm
from agag.selfnote import Conversation
from agag.trace import MirrorReader, stall_candidates, trace

DEV, FRONT, AUTOLAB = 8, 15, 11
ACK = "Message received. Please wait for the reply."
NAMES = {DEV: "Developer", FRONT: "Front", AUTOLAB: "autolab"}


def post(realm, channel, topic, text, sender):
    return realm.post(channel, topic, text, sender_id=sender, sender_name=NAMES[sender])


def settle(mirror, predicate, what="the mirror to catch up"):
    deadline = time.time() + 5
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def world(tmp_path):
    """A request in `#front`, delegated to a workplan with one open task.
    Every root note carries the anchor its writer has since p2: a post in
    the home it names."""
    realm = FakeRealm()
    realm.add_channel(3, "front")
    realm.add_channel(6, "pj-x")
    realm.add_channel(7, "work-m1")
    ask = post(realm, "front", "front-a", "Build it.", DEV)
    post(realm, "front", "front-a", ACK, FRONT)
    post(realm, "pj-x", "workplan-a", f"[selfnote][rootchat] front/front-a #{ask}", FRONT)
    post(realm, "pj-x", "workplan-a", "Mission: build it.", FRONT)
    mission = post(realm, "pj-x", "workplan-a", "[selfnote][mission] x", AUTOLAB)
    post(realm, "pj-x", "workplan-a", "[selfnote][state] started", AUTOLAB)
    task = f"workrun-task1-m{mission}"
    post(realm, "work-m1", task, f"[selfnote][task] {mission}#1", AUTOLAB)
    post(realm, "work-m1", task, f"[selfnote][rootchat] pj-x/workplan-a #{mission}", AUTOLAB)
    post(realm, "work-m1", task, "# Task 1", AUTOLAB)
    post(realm, "work-m1", task, f"[selfnote][rootchat] front/front-a #{ask}", FRONT)
    post(realm, "work-m1", task, "Start task 1.", FRONT)
    post(realm, "work-m1", task, ACK, AUTOLAB)
    answer = post(realm, "work-m1", task, "@**Front** task 1 done", AUTOLAB)
    mirror = Mirror.open(tmp_path / "zulip.env", tmp_path / "mirror", client_factory=realm.facet,
                         log=lambda line: None, start=True, resync_backoff=0.05)
    settle(mirror, lambda: mirror.message(answer) is not None)
    yield realm, mirror, ask, mission, task, answer
    mirror.stop()


def move(realm, mirror, channel, old, new):
    ids = realm._topic_ids(channel, old)
    realm.move(ids, new)
    settle(mirror, lambda: all(mirror.message(i).topic == new for i in ids), f"{old} → {new}")


def tree(mirror, message_id, now=10_000):
    return trace(MirrorReader(mirror), message_id, now=now)


def topics(result):
    return sorted(node.topic for node in result.nodes())


def test_the_trace_follows_a_renamed_origin_by_its_anchor(world):
    realm, mirror, ask, mission, task, _ = world
    before = tree(mirror, ask)
    move(realm, mirror, "front", "front-a", "front-a-renamed")
    after = tree(mirror, ask)
    assert topics(after) == sorted(["front-a-renamed", "workplan-a", task])
    assert [n.anchor for n in after.nodes()] == [n.anchor for n in before.nodes()]


def test_a_reused_name_does_not_inherit_the_conversation_that_had_it(world):
    realm, mirror, ask, mission, task, _ = world
    move(realm, mirror, "front", "front-a", "front-a-renamed")
    fresh = post(realm, "front", "front-a", "Something else entirely.", DEV)
    settle(mirror, lambda: mirror.message(fresh) is not None)
    assert topics(tree(mirror, fresh)) == ["front-a"], "the new request has no delegations"
    assert topics(tree(mirror, ask)) == sorted(["front-a-renamed", "workplan-a", task])


def test_a_note_without_an_anchor_is_not_given_to_a_later_holder_of_its_name(world):
    realm, mirror, ask, mission, task, _ = world
    legacy = post(realm, "pj-x", "old-delegate", "[selfnote][rootchat] front/front-a", FRONT)
    move(realm, mirror, "front", "front-a", "front-a-renamed")
    fresh = post(realm, "front", "front-a", "Something else entirely.", DEV)
    settle(mirror, lambda: mirror.message(fresh) is not None and mirror.message(legacy) is not None)
    assert "old-delegate" not in topics(tree(mirror, fresh)), "older than the conversation that holds the name now"


def test_a_retired_mission_keeps_its_tasks_and_the_replacement_gets_only_its_own(world):
    """autolab's retirement: the workplan is renamed away, a replacement
    takes the name with `[replaces] <old anchor>`; the old task's note names
    the workplan without an anchor, as notes written before p2 do."""
    realm, mirror, ask, mission, task, _ = world
    legacy_task = "workrun-task0-legacy"
    post(realm, "work-m1", legacy_task, f"[selfnote][task] {mission}#0", AUTOLAB)
    post(realm, "work-m1", legacy_task, "[selfnote][rootchat] pj-x/workplan-a", AUTOLAB)
    move(realm, mirror, "pj-x", "workplan-a", f"✔ retired-workplan-a-m{mission}")
    post(realm, "pj-x", "workplan-a", f"[selfnote][rootchat] front/front-a #{ask}", FRONT)
    post(realm, "pj-x", "workplan-a", f"[selfnote][replaces] {mission}", AUTOLAB)
    replacement = post(realm, "pj-x", "workplan-a", "[selfnote][mission] x2", AUTOLAB)
    settle(mirror, lambda: mirror.message(replacement) is not None)
    result = tree(mirror, ask)
    retired = next(n for n in result.nodes() if n.topic.startswith("✔ retired-"))
    current = next(n for n in result.nodes() if n.topic == "workplan-a")
    assert sorted(c.topic for c in retired.children) == sorted([legacy_task, task])
    assert current.children == []
    assert retired.anchor != current.anchor


def test_a_served_mark_follows_the_answering_conversation_through_a_rename(world):
    realm, mirror, ask, mission, task, answer = world
    post(realm, "front", "front-a", f"[selfnote][served] work-m1/{task} {answer}", FRONT)
    move(realm, mirror, "work-m1", task, f"{task}-renamed")
    result = tree(mirror, ask, now=realm.messages[answer]["timestamp"] + 3600)
    node = next(n for n in result.nodes() if n.topic == f"{task}-renamed")
    assert node.state != "awaiting_delivery"
    assert served_key(mirror, Conversation("work-m1", task), answer) == ("work-m1", f"{task}-renamed")


def test_a_rename_keeps_the_candidate_key(world):
    realm, mirror, ask, mission, task, answer = world
    later = realm.messages[answer]["timestamp"] + 3600
    before = [c.key for c in stall_candidates(tree(mirror, ask, now=later), now=later)]
    assert before, "an answer nobody served is owed"
    move(realm, mirror, "work-m1", task, f"{task}-renamed")
    after = [c.key for c in stall_candidates(tree(mirror, ask, now=later), now=later)]
    assert after == before


def test_a_deleted_anchor_is_nowhere_and_the_name_is_not_guessed(world):
    realm, mirror, ask, mission, task, _ = world
    assert whereabouts(mirror, ask) == ("front", "front-a")
    realm.delete(ask)
    settle(mirror, lambda: mirror.message(ask) is None)
    assert whereabouts(mirror, ask) is None
    assert served_key(mirror, Conversation("front", "front-a"), ask) == ("front", "front-a")
    assert bare("✔ x") == "x" and bare("x") == "x"


# --- threads: which delegations a serving of home is party to ---------------------


class NotesClient:
    """What `remotes_for_home` reads: this bot's root notes, and a message
    lookup, over the fake realm."""

    def __init__(self, realm, self_id):
        self.realm, self.self_id = realm, self_id

    def _own(self, marker):
        return [dict(m) for m in sorted(self.realm.messages.values(), key=lambda m: m["id"])
                if m["sender_id"] == self.self_id and m["content"].startswith(marker)]

    def own_rootchat_notes(self, num_before=200):
        return self._own("[selfnote][rootchat] ")

    def own_moved_notes(self, num_before=200):
        return self._own("[selfnote][rootchat-moved] ")

    def message(self, message_id, strict=False):
        found = self.realm.messages.get(int(message_id))
        return dict(found) if found is not None else None

    def history(self, channel, topic):
        return [dict(m) for m in sorted(self.realm.messages.values(), key=lambda m: m["id"])
                if m["display_recipient"] == channel and m["subject"] == topic]


def test_a_reused_home_name_inherits_no_threads_and_a_renamed_home_keeps_its_own(world):
    from agag.zulip import remotes_for_home

    realm, mirror, ask, mission, task, _ = world
    legacy = post(realm, "pj-x", "old-thread", "[selfnote][rootchat] front/front-a", FRONT)
    client = NotesClient(realm, FRONT)
    move(realm, mirror, "front", "front-a", "front-a-renamed")
    fresh = post(realm, "front", "front-a", "Something else entirely.", DEV)
    reused = [c.topic for c in remotes_for_home(client, "front", "front-a",
                                                 home_messages=client.history("front", "front-a"))]
    assert reused == [], "neither the anchored delegations nor the older unanchored one belong to the new request"
    renamed = [c.topic for c in remotes_for_home(client, "front", "front-a-renamed",
                                                  home_messages=client.history("front", "front-a-renamed"))]
    assert sorted(renamed) == sorted(["workplan-a", task])
    assert fresh and legacy
