"""`agag.mirror` against a fake realm: the fixtures `better_zulip_call` p1
step 2 asks for — bootstrap overlap, event replay, edit and delete, move and
rename with a reused name, queue-expiry recovery, and a restart that reads
nothing."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from agag.mirror import Mirror
from agag.mirror.testing import SELF, FakeRealm
from agag.mirror.store import Store, notes_of
from agag.zulip import RESOLVED_TOPIC_PREFIX


def wait_live(mirror: Mirror, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mirror.live:
            return
        time.sleep(0.02)
    raise AssertionError(f"mirror did not go live: {mirror.health()}")


def wait_until(predicate, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def realm_with_history() -> FakeRealm:
    realm = FakeRealm()
    realm.add_channel(1, "agents", folder_id=3)
    realm.add_channel(2, "pj-demo", folder_id=5)
    realm.add_channel(3, "old-archive", archived=True)
    realm.post("agents", "intro-front-x", "Hello, I am Front.\n\n```agag-roster\nschema: ag.agent-roster.v1\n"
               "instance: front-x\nagent: agfront\nbot: Front\nbot_id: 15\nchannel: front-x\nprefixes: front-\n```",
               sender_id=15, sender_name="Front", quiet=True)
    realm.post("pj-demo", "workplan-a", "[selfnote][mission] demo", sender_id=11, sender_name="autolab", quiet=True)
    realm.post("pj-demo", "workplan-a", "plan text", sender_id=11, sender_name="autolab", quiet=True)
    realm.post("pj-demo", "workplan-a", "looks good", sender_id=7, sender_name="Dev", quiet=True)
    realm.post("pj-demo", "chat", "just a question", quiet=True)
    return realm


def open_mirror(realm: FakeRealm, tmp_path: Path, **kwargs) -> Mirror:
    return Mirror.open(tmp_path / "zulip.env", tmp_path / "mirror", client_factory=lambda: realm,
                       log=lambda line: None, start=False, resync_backoff=0.05, **kwargs)


# --- bootstrap and replay ---------------------------------------------------------


def test_bootstrap_registers_before_history_and_replay_does_not_duplicate(tmp_path):
    realm = realm_with_history()
    # A message that lands while the history is being read is both in the
    # page and in the queue.
    realm.on_first_page = lambda: realm.post("pj-demo", "chat", "posted mid-hydration")
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    wait_until(lambda: any(m.content == "posted mid-hydration" for m in mirror.messages("pj-demo", "chat")),
               what="the mid-hydration post")
    time.sleep(0.3)  # let the queued copy be replayed
    assert realm.log.index("register") < realm.log.index("streams") < realm.log.index("messages")
    chat = mirror.messages("pj-demo", "chat")
    assert [m.content for m in chat] == ["just a question", "posted mid-hydration"]
    changes = mirror.changes(0)
    assert sum(1 for c in changes if c.kind == "message" and c.detail.get("sender_id") == 7
               and c.message_id == chat[-1].id) == 1
    health = mirror.health()
    assert health["state"] == "live" and health["resyncs"] == 1
    assert health["counts"]["messages"] == 6 and health["counts"]["complete"] == 3
    assert mirror.self_id == 42 and mirror.bot_name == "Mirror Bot"
    mirror.close()


def test_replayed_events_neither_duplicate_nor_regress(tmp_path):
    realm = realm_with_history()
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    ident = realm.post("pj-demo", "chat", "first version")
    wait_until(lambda: mirror.message(ident) is not None, what="the post")
    realm.edit(ident, "second version")
    wait_until(lambda: mirror.message(ident).content == "second version", what="the edit")
    revision = mirror.revision()
    realm.replay_once = True  # the next poll answers with every event again
    realm.post("pj-demo", "chat", "trigger")
    wait_until(lambda: any(m.content == "trigger" for m in mirror.messages("pj-demo", "chat")), what="trigger")
    time.sleep(0.2)
    assert mirror.message(ident).content == "second version"
    assert [m.content for m in mirror.messages("pj-demo", "chat")] == ["just a question", "first version" if False else "second version", "trigger"]
    # Exactly one change for the trigger; the replayed message and edit made none.
    assert [c.kind for c in mirror.changes(revision)] == ["message"]
    mirror.close()


# --- edit and delete -----------------------------------------------------------------


def test_edit_reindexes_notes_and_delete_hides_the_message(tmp_path):
    realm = realm_with_history()
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    ident = realm.post("pj-demo", "workplan-a", "[selfnote][state] planned", sender_id=11)
    wait_until(lambda: any(n.value == "planned" for n in mirror.notes(tag="state")), what="state note")
    realm.edit(ident, "[selfnote][state] started")
    wait_until(lambda: [n.value for n in mirror.notes(tag="state")] == ["started"], what="edited note")
    # An older edit replayed after a newer one is dropped.
    mirror.store.edit_message(ident, "[selfnote][state] planned", 1)
    assert [n.value for n in mirror.notes(tag="state")] == ["started"]
    realm.delete(ident)
    wait_until(lambda: mirror.message(ident) is None, what="the deletion")
    assert mirror.notes(tag="state") == []
    assert mirror.store.message(ident, include_deleted=True).deleted
    kinds = [c.kind for c in mirror.changes(0)]
    assert kinds[-3:] == ["message", "edit", "delete"]
    # Deleting the last message of a topic removes the topic from the index.
    lone = realm.post("pj-demo", "lonely", "only one")
    wait_until(lambda: mirror.topic("pj-demo", "lonely"), what="lonely topic")
    realm.delete(lone)
    wait_until(lambda: not mirror.topic("pj-demo", "lonely"), what="lonely topic gone")
    mirror.close()


# --- move, rename, reused names -----------------------------------------------------


def test_resolve_moves_the_conversation_and_a_reused_name_is_a_twin(tmp_path):
    realm = realm_with_history()
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    anchor = next(n.message_id for n in mirror.notes(tag="mission"))
    realm.resolve("pj-demo", "workplan-a")
    wait_until(lambda: mirror.message(anchor).resolved, what="the resolve")
    found = mirror.topic("pj-demo", "workplan-a")
    assert [t.live_name for t in found] == ["✔ workplan-a"]
    assert found[0].resolved and found[0].complete and found[0].count == 3
    assert mirror.live_name("pj-demo", "workplan-a") == "✔ workplan-a"
    assert mirror.store.coverage(2, "✔ workplan-a").complete
    assert mirror.store.coverage(2, "workplan-a") is None
    # The conversation is one thing under both names.
    assert [m.content for m in mirror.messages("pj-demo", "workplan-a")] == ["[selfnote][mission] demo", "plan text", "looks good"]
    # Somebody posts under the freed bare name: a second, open topic.
    twin = realm.post("pj-demo", "workplan-a", "[selfnote][mission] demo-again", sender_id=11)
    wait_until(lambda: len(mirror.topic("pj-demo", "workplan-a")) == 2, what="the twin")
    open_one = [t for t in mirror.topic("pj-demo", "workplan-a") if not t.resolved][0]
    assert open_one.count == 1 and open_one.complete and open_one.first_id == twin
    assert mirror.live_name("pj-demo", "workplan-a") == "workplan-a"
    # The old anchor is still where it is, not where the name now points.
    assert mirror.message(anchor).topic == "✔ workplan-a"
    assert mirror.message(twin).topic == "workplan-a"
    assert {n.message_id for n in mirror.notes(tag="mission")} == {anchor, twin}
    # A plain rename moves as well, and the old name leaves the index.
    realm.move(mirror.store.message_ids(2, "chat"), "chat-renamed")
    wait_until(lambda: mirror.topic("pj-demo", "chat-renamed"), what="the rename")
    assert not mirror.topic("pj-demo", "chat")
    moves = [c for c in mirror.changes(0) if c.kind == "move"]
    assert moves and moves[-1].detail["from_topic"] == "chat"
    mirror.close()


# --- queue expiry ---------------------------------------------------------------------


def test_queue_expiry_resyncs_and_recovers_missed_edits_and_deletions(tmp_path):
    realm = realm_with_history()
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    edited = next(m.id for m in mirror.messages("pj-demo", "workplan-a") if m.content == "plan text")
    gone = mirror.messages("pj-demo", "chat")[0].id
    # Nobody is listening: an edit, a deletion, a post and a resolve happen
    # with no event for any of them.
    realm.edit(edited, "plan text, corrected", quiet=True)
    realm.delete(gone, quiet=True)
    late = realm.post("pj-demo", "workplan-a", "posted while expired", quiet=True)
    realm.create_channel(4, "work-m1", quiet=True)
    realm.post("work-m1", "workrun-1", "[selfnote][task] 1#1", quiet=True)
    realm.expire_queue()
    wait_until(lambda: mirror.health()["resyncs"] == 2, what="the second resync")
    wait_live(mirror)
    assert mirror.message(edited).content == "plan text, corrected"
    assert mirror.message(gone) is None
    assert mirror.message(late) is not None
    assert not mirror.topic("pj-demo", "chat")
    assert mirror.channel("work-m1") is not None and mirror.topic("work-m1", "workrun-1")
    kinds = [c.kind for c in mirror.changes(0)]
    assert kinds.count("resync") == 2 and "edit" in kinds and "delete" in kinds
    # And the new queue keeps delivering.
    after = realm.post("pj-demo", "chat", "back on the air")
    wait_until(lambda: mirror.message(after) is not None, what="post after recovery")
    mirror.close()


# --- restart ------------------------------------------------------------------------


def test_restart_resumes_the_persisted_queue_without_reading_the_realm(tmp_path):
    realm = realm_with_history()
    first = open_mirror(realm, tmp_path)
    first.start()
    wait_live(first)
    first.stop()
    before = len(realm.log)
    posted = realm.post("pj-demo", "chat", "while the consumer was down")
    second = open_mirror(realm, tmp_path)
    second.start()
    wait_live(second)
    wait_until(lambda: second.message(posted) is not None, what="the post made while down")
    reads = [name for name in realm.log[before:] if name not in ("events", "users/me")]
    assert reads == [], reads
    assert second.health()["resyncs"] == 0 and second.health()["reason"] == "live (resumed queue)"
    assert second.health()["counts"]["messages"] == 6
    second.close()
    # Another account's store is not this one's.
    realm_other = realm_with_history()
    other_self = dict(SELF, email="someone-else@example", user_id=43)
    realm_other.whoami = lambda refresh=False: other_self  # type: ignore[assignment]
    third = open_mirror(realm_other, tmp_path)
    third.start()
    wait_live(third)
    assert third.health()["resyncs"] == 1
    third.close()


# --- hydrate, verify, single flight -------------------------------------------------


def test_hydrate_is_single_flight_and_verify_folds_a_targeted_read(tmp_path):
    realm = realm_with_history()
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    mirror.store.clear_coverage(2)
    assert not mirror.topic("pj-demo", "chat")[0].complete
    realm.edit(mirror.messages("pj-demo", "chat")[0].id, "silently edited", quiet=True)
    before = realm.calls
    # The first read takes a while; the other two callers arrive inside it.
    realm.on_first_page = lambda: time.sleep(0.3)
    results = []
    threads = [threading.Thread(target=lambda: results.append(mirror.hydrate("pj-demo", "chat"))) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert realm.log[before:].count("messages") == 1
    assert all(r.complete for r in results)
    assert mirror.messages("pj-demo", "chat")[0].content == "silently edited"
    # `messages(hydrate=True)` reads only what is not complete.
    calls = realm.calls
    mirror.messages("pj-demo", "chat", hydrate=True)
    assert realm.calls == calls
    # verify: Zulip's answer wins, and "gone" is folded in as a deletion.
    ident = mirror.messages("pj-demo", "workplan-a")[-1].id
    realm.edit(ident, "verified text", quiet=True)
    assert mirror.verify(ident).content == "verified text"
    realm.delete(ident, quiet=True)
    assert mirror.verify(ident) is None
    assert mirror.message(ident) is None
    mirror.close()


# --- the index, notes, intros ---------------------------------------------------------


def test_index_last_real_notes_and_intros(tmp_path):
    realm = realm_with_history()
    realm.post("agents", "intro-old-x", "retired agent", quiet=True)
    realm.resolve("agents", "intro-old-x", quiet=True)
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    index = {t.key: t for t in mirror.topics()}
    plan = index[("pj-demo", "workplan-a")]
    assert plan.count == 3 and plan.complete and plan.last_real.content == "looks good"
    assert plan.max_id == plan.last_id
    # A topic holding only notes and notices has no real speaker.
    realm.post("pj-demo", "notes-only", "[selfnote][rootchat] front/front-x", sender_id=15)
    realm.post("pj-demo", "notes-only", "Notification Bot says", sender_id=1, realm="zulipinternal")
    wait_until(lambda: mirror.topic("pj-demo", "notes-only") and mirror.topic("pj-demo", "notes-only")[0].count == 2,
               what="notes-only topic")
    assert mirror.topic("pj-demo", "notes-only")[0].last_real is None
    assert [(n.tag, n.value) for n in mirror.notes(channel="pj-demo", topic="notes-only")] == [("rootchat", "front/front-x")]
    assert mirror.notes(tag="rootchat", sender_id=15)[0].message_id
    intros = mirror.intros()
    assert intros["front-x"].roster.bot == "Front" and intros["front-x"].roster.prefixes == ("front-",)
    assert intros["front-x"].message.content.startswith("Hello")
    assert intros["old-x"].retired and intros["old-x"].roster is None
    # history() is the drop-in for readers of Zulip dicts.
    dicts = mirror.history("pj-demo", "workplan-a", num_before=2)
    assert [d["content"] for d in dicts] == ["plan text", "looks good"]
    assert dicts[0]["display_recipient"] == "pj-demo" and dicts[0]["subject"] == "workplan-a"
    # channels: archived ones only on request; a channel event archives live.
    assert [c.name for c in mirror.channels()] == ["agents", "pj-demo"]
    assert [c.name for c in mirror.channels(include_archived=True)] == ["agents", "old-archive", "pj-demo"]
    realm.archive(2)
    wait_until(lambda: [c.name for c in mirror.channels()] == ["agents"], what="the archive event")
    # wait() returns when the revision moves.
    revision = mirror.revision()
    threading.Timer(0.05, lambda: realm.post("agents", "intro-front-x", "re-posted")).start()
    assert mirror.wait(revision, timeout=3.0) > revision
    mirror.close()


def test_notes_of_reads_every_note_line():
    assert notes_of("[selfnote][rootchat] a/b") == [("rootchat", "a/b")]
    assert notes_of("prose\n  [selfnote][state] done\nmore") == [("state", "done")]
    assert notes_of("no notes here") == []


def test_changes_feed_says_when_it_forgot(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    with store.transaction():
        for n in range(30):
            store.note_change("resync", {"n": n})
        store.prune_changes(keep=10)
    assert store.changes(0) is None
    assert [c.detail["n"] for c in store.changes(store.revision() - 3)] == [27, 28, 29]
    store.close()


def test_an_archived_channels_topic_is_hydrated_once_on_demand(tmp_path):
    realm = realm_with_history()
    realm.post("old-archive", "workrun-1", "[selfnote][task] 1#1", quiet=True)
    realm.post("old-archive", "workrun-1", "done here", quiet=True)
    realm.resolve("old-archive", "workrun-1", quiet=True)
    mirror = open_mirror(realm, tmp_path)
    mirror.start()
    wait_live(mirror)
    # The resync did not read it: the channel is archived.
    assert mirror.messages("old-archive", "workrun-1") == []
    before = realm.calls
    found = mirror.messages("old-archive", "workrun-1", hydrate=True)
    assert [m.content for m in found] == ["[selfnote][task] 1#1", "done here"]
    assert realm.log[before:].count("messages") == 2  # the bare name (empty) and the ✔ name
    # Both answers are kept, the empty one included: a second ask reads nothing.
    again = realm.calls
    mirror.messages("old-archive", "workrun-1", hydrate=True)
    assert realm.calls == again
    assert mirror.live_name("old-archive", "workrun-1") == "✔ workrun-1"
    mirror.close()
