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
