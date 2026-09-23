"""`agag.listen` over a mirrored fake realm (`better_zulip_call` p1 step 5).

What is pinned: intake keeps going while a handler runs; a burst on one
conversation is one serving; a post after the reply is served again; a
restart resumes what was pending without re-running what finished; a
mention is served once and its served mark keeps a restart quiet; the
recovery hook runs at startup and after a resync.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from agag.listen import MENTION, OWNER, Listener, Queue
from agag.mirror import Mirror
from agag.mirror.testing import FakeRealm

BOT, DEV, OTHER = 42, 7, 9
ACK = "Message received. Please wait for the reply."


class NoStatus:
    def record_error(self, error):
        pass

    def record_poll_ok(self, queue_id):
        pass


def wait_until(predicate, timeout=5.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def realm_with_channels() -> FakeRealm:
    realm = FakeRealm()
    realm.add_channel(35, "agents")
    realm.add_channel(6, "pj-x")
    realm.add_channel(8, "mirror-bot-x")  # this bot's own channel
    return realm


class Harness:
    """A mirror, a listener and a recording handler over one fake realm."""

    def __init__(self, realm: FakeRealm, root: Path, *, on_mention=True, on_recover=None, block=None, blocks=None):
        self.realm = realm
        self.root = root
        self.mirror = Mirror.open(root / "zulip.env", root / "mirror", client_factory=realm.facet,
                                  log=lambda line: None, start=True, resync_backoff=0.05)
        self.client = realm.facet()
        self.served: list[tuple[str, str, str]] = []
        self.recovered: list[str] = []
        self.block = block  # an Event the handler waits on, to simulate a long run
        self.blocks = blocks or {}  # per-topic Events, for a crash at a chosen point
        self.log: list[str] = []

        def handler(channel, topic):
            self.served.append((OWNER, channel, topic))
            gate = self.blocks.get(topic, self.block)
            if gate is not None:
                gate.wait(10.0)
            if self.listener._stop.is_set():
                return  # a stopped listener's run must not act (the "crash")
            # The serving replies, like `serve_topic`: ack, then the answer.
            realm.post(channel, topic, ACK, sender_id=BOT, sender_name="Mirror Bot")
            realm.post(channel, topic, f"answer for {topic}", sender_id=BOT, sender_name="Mirror Bot")

        def mention(channel, topic):
            self.served.append((MENTION, channel, topic))
            # Front's shape: answer at home and mark the callback served.
            last = max(m["id"] for m in realm.messages.values() if m["subject"] == topic)
            realm.post("mirror-bot-x", "home", f"[selfnote][served] {channel}/{topic} {last}", sender_id=BOT,
                       sender_name="Mirror Bot")

        def recover():
            self.recovered.append("hook")
            if on_recover is not None:
                on_recover()

        self.listener = Listener(
            self.mirror, self.client,
            topic_filter=lambda channel, topic: channel == "mirror-bot-x" or topic.startswith("workplan-"),
            handler=handler, on_mention=mention if on_mention else None, on_recover=recover,
            is_ack=lambda content: content == ACK, log=self.log.append, status=NoStatus(), idle_seconds=0.2,
        )
        self.thread = threading.Thread(target=self.listener.run, daemon=True)

    def start(self):
        self.thread.start()
        wait_until(lambda: self.listener.recoveries >= 1, what="startup recovery")
        return self

    def stop(self):
        self.listener.stop()
        self.thread.join(3.0)
        self.mirror.stop()


def test_intake_keeps_going_while_a_handler_runs_and_a_burst_is_one_serving(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, block=gate).start()
    realm.post("pj-x", "workplan-a", "please plan a", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.served) == 1, what="the first serving to start")
    # While it runs, a burst lands on another topic and two more posts on a third.
    for n in range(5):
        realm.post("pj-x", "workplan-b", f"burst {n}", sender_id=DEV, sender_name="Dev")
    realm.post("pj-x", "workplan-c", "one", sender_id=DEV, sender_name="Dev")
    realm.post("pj-x", "workplan-c", "two", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: {(e.channel, e.topic) for e in h.listener.queue.entries("pending")}
               == {("pj-x", "workplan-b"), ("pj-x", "workplan-c")}, what="intake during the run")
    assert len(h.served) == 1, "the executor is busy; intake queued without serving"
    gate.set()
    wait_until(lambda: len(h.served) == 3, what="the queued servings")
    assert [t for _, _, t in h.served] == ["workplan-a", "workplan-b", "workplan-c"]
    wait_until(lambda: len(h.listener.queue) == 0, what="an empty queue")
    # A post after the reply is served again; the bot's own reply is not.
    realm.post("pj-x", "workplan-a", "thanks, one more thing", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.served) == 4, what="the follow-up serving")
    time.sleep(0.5)
    assert len(h.served) == 4
    h.stop()


def test_a_post_during_the_serving_is_looked_at_again_and_a_reply_ends_it(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, block=gate).start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.served) == 1, what="serving")
    realm.post("pj-x", "workplan-a", "and also this", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: any(e.again for e in h.listener.queue.entries()), what="the again flag")
    gate.set()
    # The handler's reply made the bot the last speaker: the second look finds nothing owed.
    wait_until(lambda: len(h.listener.queue) == 0, what="the entry to clear")
    time.sleep(0.3)
    assert len(h.served) == 1
    assert any("looking again" in line for line in h.log) and any("nothing owed" in line for line in h.log)
    h.stop()


def test_a_restart_resumes_pending_work_and_does_not_rerun_what_finished(tmp_path):
    realm = realm_with_channels()
    gate_a, gate_c = threading.Event(), threading.Event()
    h = Harness(realm, tmp_path, blocks={"workplan-a": gate_a, "workplan-c": gate_c}).start()
    realm.post("pj-x", "workplan-a", "a", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.served) == 1, what="serving a")
    realm.post("pj-x", "workplan-b", "b", sender_id=DEV, sender_name="Dev")
    realm.post("pj-x", "workplan-c", "c", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.listener.queue.entries("pending")) == 2, what="b and c pending")
    gate_a.set()
    # a replies, b is served and replies, c starts and blocks: the crash
    # lands with b finished, c running without a reply, and nothing else.
    wait_until(lambda: (OWNER, "pj-x", "workplan-c") in h.served, what="c to start")
    wait_until(lambda: any(m["content"] == "answer for workplan-b" for m in realm.messages.values()), what="b's reply")
    h.listener.stop()
    h.thread.join(3.0)
    h.mirror.stop()
    assert [(e.topic, e.state) for e in h.listener.queue.entries()] == [("workplan-c", "running")]
    realm.post("pj-x", "workplan-d", "d while down", sender_id=DEV, sender_name="Dev")
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: len(h2.served) >= 2, what="c and d after the restart")
    time.sleep(0.5)
    assert sorted(t for _, _, t in h2.served) == ["workplan-c", "workplan-d"]
    assert h2.mirror.health()["resyncs"] == 0, "the restart resumed the persisted queue"
    assert any("was running at the restart; queued again" in line for line in h2.log)
    gate_c.set()  # release the crashed run's thread; it returns without acting
    h2.stop()


def test_a_mention_is_served_once_and_its_mark_keeps_a_restart_quiet(tmp_path):
    realm = realm_with_channels()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post("mirror-bot-x", "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    h = Harness(realm, tmp_path).start()
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** it is done", sender_id=OTHER, sender_name="autolab",
               flags=("mentioned",))
    wait_until(lambda: (MENTION, "pj-x", "workrun-1") in h.served, what="the mention serving")
    time.sleep(0.4)
    assert h.served.count((MENTION, "pj-x", "workrun-1")) == 1
    # The served note landed at home; a restart's recovery sees the mark.
    h.stop()
    h2 = Harness(realm, tmp_path).start()
    time.sleep(0.4)
    assert (MENTION, "pj-x", "workrun-1") not in h2.served
    # A newer post naming the bot is past the mark and is served.
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** one more", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: (MENTION, "pj-x", "workrun-1") in h2.served, what="the second mention")
    h2.stop()


def test_recovery_reads_the_index_and_runs_the_hook_at_startup_and_after_a_resync(tmp_path):
    realm = realm_with_channels()
    realm.post("pj-x", "workplan-old", "waiting since before the listener existed", sender_id=DEV, sender_name="Dev",
               quiet=True)
    realm.post("pj-x", "workplan-done", "answered", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post("pj-x", "workplan-done", "here", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    realm.post("pj-x", "workplan-gone", "resolved", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.resolve("pj-x", "workplan-gone", quiet=True)
    h = Harness(realm, tmp_path).start()
    wait_until(lambda: (OWNER, "pj-x", "workplan-old") in h.served, what="the recovered topic")
    assert h.recovered == ["hook"]
    assert not any(t in ("workplan-done", "workplan-gone") for _, _, t in h.served)
    # Downtime by another name: the queue expires, the mirror resyncs, the
    # listener recovers from the index again and the hook runs again.
    realm.post("pj-x", "workplan-missed", "posted while the queue was dead", sender_id=DEV, sender_name="Dev",
               quiet=True)
    realm.expire_queue()
    wait_until(lambda: h.mirror.health()["resyncs"] == 2, what="the resync")
    wait_until(lambda: (OWNER, "pj-x", "workplan-missed") in h.served, what="the missed topic")
    wait_until(lambda: h.recovered == ["hook", "hook"], what="the hook after the resync")
    # No Zulip call for any of it beyond the mirror's own.
    assert h.client.calls == 1  # whoami
    h.stop()


def test_the_queue_coalesces_and_orders_owners_first(tmp_path):
    queue = Queue(tmp_path / "q.sqlite")
    assert queue.enqueue("c", "m", MENTION, revision=1, message_id=1)
    assert queue.enqueue("c", "t", OWNER, revision=2, message_id=2)
    assert not queue.enqueue("c", "t", OWNER, revision=3, message_id=3)  # coalesced
    assert len(queue) == 2
    first = queue.take()
    assert first.route == OWNER and first.message_id == 3 and first.state == "running"
    assert queue.enqueue("c", "t", OWNER, revision=4, message_id=4)  # re-armed while running
    assert queue.finish(first) is True  # pending again
    assert [e.route for e in queue.entries("pending")] == [MENTION, OWNER] or \
        [e.route for e in queue.entries("pending")] == [OWNER, MENTION]
    second = queue.take()
    assert second.route == OWNER  # owners first, whatever the order they arrived in
    assert queue.finish(second) is False
    third = queue.take()
    assert third.route == MENTION
    queue.finish(third)
    assert len(queue) == 0 and queue.take() is None
    queue.set_revision(9)
    assert queue.revision() == 9
    queue.close()


def test_a_mention_matches_the_name_whatever_its_case():
    from agag.listen import mentions_bot

    assert mentions_bot("@**cagent** hi", "Cagent") and mentions_bot("@**Cagent** hi", "cagent")
    assert not mentions_bot("@**cagent2** hi", "Cagent")



def test_a_queue_file_from_before_the_schema_key_is_rebuilt_not_crashed(tmp_path):
    """The first layout wrote no `schema` row. Deployed on 2026-09-20, the
    check that only compared an existing row let the old `pending` table
    through, and every listener's executor died on `no such column:
    next_at`. An existing table without the key is the old layout."""
    import sqlite3

    path = tmp_path / "q.sqlite"
    old = sqlite3.connect(str(path))
    old.executescript("""
    CREATE TABLE pending (
        channel TEXT NOT NULL, topic TEXT NOT NULL, route TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 0,
        message_id INTEGER NOT NULL DEFAULT 0, enqueued_at REAL NOT NULL,
        started_at REAL, attempts INTEGER NOT NULL DEFAULT 0, again INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (channel, topic, route)
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    INSERT INTO pending (channel, topic, route, enqueued_at) VALUES ('c', 't', 'owner', 1);
    INSERT INTO meta (key, value) VALUES ('revision', '7');
    -- …and a stamp that lies, as the first deployment of v2 left behind:
    -- the layout is judged by the table's columns, never by the stamp alone.
    INSERT INTO meta (key, value) VALUES ('schema', '2');
    """)
    old.commit()
    old.close()
    queue = Queue(path)
    assert queue.take() is None and len(queue) == 0, "the old rows are gone with the old layout"
    assert queue.enqueue("c", "t", OWNER, revision=1, message_id=1) and queue.take() is not None
    queue.close()
    # And a file already on the new layout keeps its rows.
    again = Queue(path)
    assert len(again) == 1
    again.close()


# --- an owner's own start (robust_workflow p1 step 3) ------------------------------


def test_an_owner_s_start_note_is_served_once_and_survives_a_restart(tmp_path):
    """autolab starts the next task of an authorized mission itself: a
    visible line and a `[selfnote][start]` of its own in a topic nobody else
    has posted into. Until now such a topic was never owed — its last speaker
    was the owner — which is why every task needed a relay's post."""
    realm = realm_with_channels()
    realm.post("pj-x", "workplan-t2", "Task 2 spec", sender_id=BOT, sender_name="Mirror Bot")
    h = Harness(realm, tmp_path).start()
    realm.post("pj-x", "workplan-t2", "Starting task 2: task 1 was accepted.", sender_id=BOT,
               sender_name="Mirror Bot")
    realm.post("pj-x", "workplan-t2", "[selfnote][start] #5 for 7 Dev", sender_id=BOT, sender_name="Mirror Bot")
    wait_until(lambda: (OWNER, "pj-x", "workplan-t2") in h.served, what="the start to be served")
    time.sleep(0.5)
    assert h.served.count((OWNER, "pj-x", "workplan-t2")) == 1, "answered once, then nothing owed"
    h.stop()

    again = Harness(realm, tmp_path).start()
    time.sleep(0.5)
    assert (OWNER, "pj-x", "workplan-t2") not in again.served, "the answer after the note spends it"
    again.stop()


def test_a_start_note_left_unanswered_by_a_crash_is_recovered(tmp_path):
    realm = realm_with_channels()
    realm.post("pj-x", "workplan-t3", "Task 3 spec", sender_id=BOT, sender_name="Mirror Bot")
    realm.post("pj-x", "workplan-t3", "[selfnote][start] #5 for 7 Dev", sender_id=BOT, sender_name="Mirror Bot")
    realm.post("pj-x", "workplan-t3", ACK, sender_id=BOT, sender_name="Mirror Bot")  # acked, then the crash
    h = Harness(realm, tmp_path).start()
    wait_until(lambda: (OWNER, "pj-x", "workplan-t3") in h.served, what="recovery to serve the start")
    h.stop()


def test_somebody_else_s_start_note_is_not_our_work(tmp_path):
    realm = realm_with_channels()
    realm.post("pj-x", "workplan-t4", "Task 4 spec", sender_id=BOT, sender_name="Mirror Bot")
    h = Harness(realm, tmp_path).start()
    realm.post("pj-x", "workplan-t4", "[selfnote][start] #5 for 7 Dev", sender_id=OTHER, sender_name="Other")
    time.sleep(0.6)
    assert (OWNER, "pj-x", "workplan-t4") not in h.served
    h.stop()



def test_a_mention_whose_topic_is_resolved_right_after_it_is_still_served(tmp_path):
    """robust_workflow p1 N3: autolab posts its closing report naming the
    requester and resolves the task at once; the report was skipped as
    'nothing owed' whenever the ✔ reached the mirror first."""
    realm = realm_with_channels()
    realm.post("pj-x", "task-a", "[selfnote][rootchat] mirror-bot-x/home", sender_id=BOT, sender_name="Mirror Bot")
    gate = threading.Event()
    h = Harness(realm, tmp_path, block=gate).start()
    realm.post("mirror-bot-x", "busy", "keep the executor busy", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: (OWNER, "mirror-bot-x", "busy") in h.served, what="the executor to be busy")
    realm.post("pj-x", "task-a", "@**Mirror Bot** task done", sender_id=OTHER, sender_name="Other")
    realm.resolve("pj-x", "task-a")
    time.sleep(0.5)
    gate.set()
    wait_until(lambda: any(route == MENTION for route, _, _ in h.served), what="the mention to be served")
    h.stop()
