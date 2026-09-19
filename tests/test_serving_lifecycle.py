"""Unanswered work survives interruptions, lost sends and handoffs
(`explicit_reply` p1 step 1).

Every test here runs the real `serve_topic` under the real listener over a
mirrored fake realm, with a client whose sends can be lost, refused, or turn
into a crash at a chosen point. What is pinned: crash after the ack, crash
before and after the reply's send, a send the server accepted whose answer
was lost, a handler exception, a failed post-run read, input arriving during
a run that resolves the topic, and the served mark on the mention route
being bound to the mention actually processed. In every case the request is
eventually answered or left in an explicit failure state, and no reply is
confirmed delivered twice.
"""

from __future__ import annotations

import threading
import time

import pytest

from agag import serving, topics

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
from agag.delivery import DeliveryError
from agag.listen import MENTION, OWNER, Listener
from agag.mirror import Mirror
from agag.mirror.testing import FakeRealm
from agag.selfnote import parse_served
from agag.zulip import ZulipError, ZulipRejected

BOT, DEV, OTHER = 42, 7, 9
ACK = "Message received. Please wait for the reply."
HOME = "mirror-bot-x"


class Crash(BaseException):
    """The process dying at this point. Not an `Exception`, so nothing in
    the serving catches it; the executor thread ends where a crash would."""


class NoStatus:
    errors: list[str] = []

    def record_error(self, error):
        NoStatus.errors.append(str(error))

    def record_poll_ok(self, queue_id):
        pass


def wait_until(predicate, timeout=6.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class RealmClient:
    """`ZulipClient`'s surface as `serve_topic` uses it, over the fake
    realm, with trouble injected per send: `trouble(content)` answers
    None (deliver), "lost" (posted, answer lost), "error" (not posted),
    "reject" (refused), "crash" (die before posting) or "crash_after"
    (die after posting)."""

    email = "mirror-bot@example"

    def __init__(self, realm: FakeRealm):
        self.realm = realm
        self.trouble = lambda content: None
        self.fail_reads = lambda: False
        self.sent: list[tuple[str, str, str]] = []

    def whoami(self, refresh=False):
        return {"user_id": BOT, "full_name": "Mirror Bot", "email": self.email}

    def _messages(self, channel, topic):
        return [dict(m) for m in sorted(self.realm.messages.values(), key=lambda m: m["id"])
                if m["display_recipient"] == channel and m["subject"] == topic]

    def topic_history(self, channel, topic, num_before=50):
        if self.fail_reads():
            raise ZulipError("zulip is down")
        return self._messages(channel, topic)[-num_before:]

    def send_to_channel(self, channel, topic, content):
        mode = self.trouble(content)
        if mode == "error":
            raise ZulipError("connection dropped")
        if mode == "reject":
            raise ZulipRejected("Invalid topic")
        if mode == "crash":
            raise Crash()
        ident = self.realm.post(channel, topic, content, sender_id=BOT, sender_name="Mirror Bot")
        self.sent.append((channel, topic, content))
        if mode == "lost":
            raise ZulipError("response lost")
        if mode == "crash_after":
            raise Crash()
        return ident

    def resolve_topic(self, message_id, topic):
        message = self.realm.messages[message_id]
        self.realm.resolve(message["display_recipient"], topic)

    def stream_id(self, name):
        return next(c["stream_id"] for c in self.realm.channels_by_id.values() if c["name"] == name)

    def channel_topics(self, stream_id):
        return sorted({m["subject"] for m in self.realm.messages.values() if m["stream_id"] == stream_id})


def realm_with_channels() -> FakeRealm:
    realm = FakeRealm()
    realm.add_channel(35, "agents")
    realm.add_channel(6, "pj-x")
    realm.add_channel(8, HOME)
    return realm


class Harness:
    """The listener over the fake realm; the owner route and the mention
    route both go through `serve_topic`."""

    def __init__(self, realm: FakeRealm, root, *, reply=None, gate=None, crash_in_handler=False,
                 resolve=False, max_attempts=5, retry_seconds=0.05):
        self.realm = realm
        self.client = RealmClient(realm)
        self.mirror = Mirror.open(root / "zulip.env", root / "mirror", client_factory=realm.facet,
                                  log=lambda line: None, start=True, resync_backoff=0.05)
        self.contexts: list = []
        self.gate = gate
        self.crash_in_handler = crash_in_handler
        self.resolve = resolve
        self.reply = reply or (lambda ctx: "the answer")
        self.log: list[str] = []
        self.delivery = {"sleep": lambda seconds: None, "backoff": (0.0,)}

        def inner(ctx):
            self.contexts.append(ctx)
            if self.gate is not None:
                self.gate.wait(10.0)
            if self.crash_in_handler:
                raise Crash()
            if self.listener._stop.is_set():
                raise Crash()
            text = self.reply(ctx)
            if isinstance(text, Exception):
                raise text
            return topics.TopicResult([text], resolve_after=self.resolve)

        def handler(channel, topic):
            topics.serve_topic(self.client, channel, topic, inner, ack_text=ACK, log=self.log.append,
                               delivery=self.delivery)

        def mention(channel, topic):
            # Front's shape: serve home, answer at home; the listener marks the callback.
            topics.serve_topic(self.client, HOME, "home", inner, ack_text=ACK, log=self.log.append,
                               extra_threads=((channel, topic),), delivery=self.delivery)

        self.listener = Listener(
            self.mirror, self.client,
            topic_filter=lambda channel, topic: channel == HOME or topic.startswith("workplan-"),
            handler=handler, on_mention=mention, is_ack=lambda content: content == ACK,
            log=self.log.append, status=NoStatus(), idle_seconds=0.2,
            max_attempts=max_attempts, retry_seconds=retry_seconds, delivery=self.delivery,
        )
        self.thread = threading.Thread(target=self.listener.run, daemon=True)

    def start(self):
        self.thread.start()
        wait_until(lambda: self.listener.recoveries >= 1, what="startup recovery")
        return self

    def crash(self):
        """Stop without letting any run act further."""
        self.listener.stop()
        if self.gate is not None:
            self.gate.set()
        self.thread.join(3.0)
        self.mirror.stop()

    def stop(self):
        self.listener.stop()
        self.thread.join(3.0)
        self.mirror.stop()

    def posts(self, channel, topic, *, ours=True):
        rows = [m for m in sorted(self.realm.messages.values(), key=lambda m: m["id"])
                if m["display_recipient"] == channel and m["subject"] in (topic, f"✔ {topic}")]
        return [m["content"] for m in rows if (m["sender_id"] == BOT) == ours]

    def replies(self, channel, topic):
        return [c for c in self.posts(channel, topic)
                if c != ACK and not c.startswith("[selfnote]") and c != "on it"]


# --- crashes ------------------------------------------------------------------------


def test_a_crash_after_the_ack_leaves_the_request_owed_and_hands_its_evidence_on(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    realm.post("pj-x", "workplan-a", "please plan a", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 1, what="the run to start")
    assert h.posts("pj-x", "workplan-a") == [ACK], "the ack is on the conversation, nothing else"
    h.crash()
    assert [(e.topic, e.state) for e in h.listener.queue.entries()] == [("workplan-a", "running")]
    # The bot's ack is the last post. The old rule read that as answered.
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nthe answer"], what="the answer")
    assert any("interrupted at 'acked'" in line for line in h2.log)
    previous = h2.contexts[0].previous
    assert previous is not None and previous.state == serving.INTERRUPTED and previous.ack_id is not None
    assert h2.posts("pj-x", "workplan-a").count(ACK) == 2 and len(h2.replies("pj-x", "workplan-a")) == 1
    h2.stop()


def test_a_crash_before_the_send_redelivers_the_prepared_reply_without_rerunning(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path).start()
    h.client.trouble = lambda content: "crash" if content.startswith("@**Dev**") else None
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: not h.thread.is_alive() or h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
               and h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).state == serving.PREPARED,
               what="the prepared record")
    h.crash()
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    assert record.state == serving.PREPARED and record.reply_text == "@**Dev**\n\nthe answer"
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nthe answer"], what="the redelivery")
    time.sleep(0.3)
    assert h2.contexts == [], "the model did not run again"
    assert h2.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).state == serving.DELIVERED
    assert len(h2.listener.queue) == 0
    h2.stop()


def test_a_crash_after_the_send_is_settled_by_reading_back_not_by_posting_again(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path).start()
    h.client.trouble = lambda content: "crash_after" if content.startswith("@**Dev**") else None
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nthe answer"], what="the send")
    h.crash()
    assert h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).state == serving.PREPARED
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: h2.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).state == serving.DELIVERED,
               what="the read-back")
    time.sleep(0.3)
    assert h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nthe answer"], "one delivery, confirmed once"
    assert h2.contexts == []
    assert any("already delivered" in line for line in h2.log)
    h2.stop()


# --- the transport ------------------------------------------------------------------


def test_a_send_whose_answer_was_lost_is_found_on_read_back_and_not_repeated(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path).start()
    lost = []
    h.client.trouble = lambda content: (lost.append(1) or "lost") if content.startswith("@**Dev**") and not lost else None
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.listener.queue) == 0 and h.listener.served == 1, what="the serving to end")
    assert h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nthe answer"]
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    delivered = [m["id"] for m in realm.messages.values() if m["content"] == "@**Dev**\n\nthe answer"]
    assert record.state == serving.DELIVERED and record.delivered_id == delivered[0]
    assert any("found on read-back" in line for line in h.log)
    h.stop()


def test_a_refused_send_is_a_terminal_failure_kept_visible(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path).start()
    h.client.trouble = lambda content: "reject" if content.startswith("@**Dev**") else None
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: h.listener.queue.entries("failed"), what="the failed entry")
    entry = h.listener.queue.entries("failed")[0]
    assert "refused" in entry.failure and entry.attempts == 1
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    assert record.state == serving.FAILED and record.reply_text == "@**Dev**\n\nthe answer"
    assert NoStatus.errors and "refused" in NoStatus.errors[-1]
    h.stop()


def test_exhausted_retries_stay_in_the_queue_and_the_next_post_re_arms_them(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path, max_attempts=2).start()
    h.client.trouble = lambda content: "error" if content.startswith("@**Dev**") else None
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: h.listener.queue.entries("failed"), what="the failed entry")
    assert len(h.contexts) == 1, "the run happened once; only the delivery was retried"
    assert any("retrying in" in line for line in h.log) and any("FAILED after 2" in line for line in h.log)
    # The prepared reply is still there, and nothing was posted twice.
    assert h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).reply_text == "@**Dev**\n\nthe answer"
    assert h.replies("pj-x", "workplan-a") == []
    # Zulip comes back and the human posts again: the entry is re-armed, the
    # prepared reply is delivered first, then the new input is served.
    h.client.trouble = lambda content: None
    realm.post("pj-x", "workplan-a", "still there?", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.replies("pj-x", "workplan-a")) == 2, what="both replies")
    assert h.replies("pj-x", "workplan-a")[0] == "@**Dev**\n\nthe answer"
    assert len(h.contexts) == 2 and len(h.listener.queue) == 0
    h.stop()


def test_a_handler_exception_is_an_explicit_failure_reply_and_the_entry_is_done(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path, reply=lambda ctx: RuntimeError("claude_code timed out")).start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: h.replies("pj-x", "workplan-a"), what="the failure reply")
    assert h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nfailed during chatlog: claude_code timed out"]
    wait_until(lambda: len(h.listener.queue) == 0, what="the entry to clear")
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    assert record.state == serving.DELIVERED and record.delivered_id is not None
    # A human post re-arms it; the failure was the reply, not a retry.
    realm.post("pj-x", "workplan-a", "try again", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 2, what="the second serving")
    h.stop()


def test_a_failed_post_run_read_is_recorded_and_the_listener_still_finds_the_input(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 1, what="the run")
    realm.post("pj-x", "workplan-a", "and this", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: any(e.again for e in h.listener.queue.entries()), what="the again flag")
    reads = [0]

    def failing():
        reads[0] += 1
        return reads[0] == 1  # the first read after the reply — the re-check — fails

    h.client.fail_reads = failing
    gate.set()
    wait_until(lambda: len(h.contexts) == 2, what="the second serving, found by the listener")
    h.gate = None
    record = h.listener.queue.servings()[-1]
    assert record.extra.get("recheck_failed"), "the failed re-check is on the record"
    wait_until(lambda: len(h.replies("pj-x", "workplan-a")) == 2, what="both replies")
    h.stop()


# --- the completion rule ---------------------------------------------------------------


def test_input_during_a_resolving_run_is_answered_before_the_topic_is_resolved(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate, resolve=True).start()
    realm.post("pj-x", "workplan-a", "cancel it", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 1, what="the run")
    realm.post("pj-x", "workplan-a", "wait, one question first", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: any(e.again for e in h.listener.queue.entries()), what="the again flag")
    gate.set()
    wait_until(lambda: len(h.contexts) == 2, what="the second serving")
    assert any("deferred" in line for line in h.log)
    wait_until(lambda: any(m["subject"] == "✔ workplan-a" for m in realm.messages.values()), what="the resolve")
    assert len(h.replies("pj-x", "workplan-a")) == 2
    assert h.contexts[1].processed_up_to > h.contexts[0].processed_up_to
    h.stop()


# --- the mention route ------------------------------------------------------------


def test_the_served_mark_is_bound_to_the_mention_processed_not_to_a_later_one(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post(HOME, "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    first = realm.post("pj-x", "workrun-1", "@**Mirror Bot** it is done", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: len(h.contexts) == 1, what="the callback serving")
    second = realm.post("pj-x", "workrun-1", "@**Mirror Bot** and another thing", sender_id=OTHER,
                        sender_name="autolab")
    gate.set()
    wait_until(lambda: len(h.contexts) == 2, what="the second mention to be served too")
    notes = [parse_served(c) for c in h.posts(HOME, "home") if c.startswith("[selfnote][served]")]
    assert [n[1] for n in notes] == [first, second], "each mark names the mention it answered"
    assert h.replies(HOME, "home") and all(r.startswith("@**Dev**") for r in h.replies(HOME, "home"))
    h.stop()


def test_a_restart_between_the_home_reply_and_the_served_mark_writes_the_mark_without_a_rerun(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post(HOME, "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    h = Harness(realm, tmp_path).start()
    h.client.trouble = lambda content: "crash" if content.startswith("[selfnote][served]") else None
    mention = realm.post("pj-x", "workrun-1", "@**Mirror Bot** it is done", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: h.replies(HOME, "home") == ["@**Dev**\n\nthe answer"], what="the home reply")
    h.crash()
    assert h.listener.queue.latest_serving(("pj-x", "workrun-1", MENTION)).state == serving.DELIVERED
    assert not [c for c in h.posts(HOME, "home") if c.startswith("[selfnote][served]")]
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: [c for c in h2.posts(HOME, "home") if c.startswith("[selfnote][served]")], what="the mark")
    time.sleep(0.3)
    assert h2.contexts == [] and h.replies(HOME, "home") == ["@**Dev**\n\nthe answer"]
    note = parse_served([c for c in h2.posts(HOME, "home") if c.startswith("[selfnote][served]")][0])
    assert note[1] == mention
    assert len(h2.listener.queue) == 0
    h2.stop()


# --- the skeleton alone -------------------------------------------------------------


class ScriptedClient:
    email = "bot@example.invalid"

    def __init__(self, history, *, trouble=None):
        self.history = history
        self.sent = []
        self.trouble = trouble or (lambda content: None)
        self.next_id = 500

    def whoami(self):
        return {"user_id": BOT, "full_name": "Bot"}

    def topic_history(self, channel, topic, num_before):
        return list(self.history)

    def send_to_channel(self, channel, topic, content):
        mode = self.trouble(content)
        if mode == "error":
            raise ZulipError("dropped")
        if mode == "reject":
            raise ZulipRejected("no")
        self.next_id += 1
        row = {"id": self.next_id, "sender_id": BOT, "sender_full_name": "Bot", "content": content}
        self.history.append(row)
        self.sent.append(content)
        if mode == "lost":
            raise ZulipError("lost")
        return self.next_id

    def resolve_topic(self, message_id, topic):
        pass


def human(content, id, name="Developer", sender=DEV):
    return {"id": id, "sender_id": sender, "sender_full_name": name, "content": content}


def test_serve_topic_returns_the_journal_and_records_every_stage():
    client = ScriptedClient([human("build it", 1)])
    journal = serving.NullJournal(trigger_id=1)
    record = topics.serve_topic(client, "c", "t", lambda ctx: topics.TopicResult(["ok"]), ack_text="ack",
                                journal=journal, log=lambda t: None)
    assert record.state == serving.DELIVERED and record.ack_id == 501 and record.delivered_id == 502
    assert record.input_up_to == 501 and record.requester_name == "Developer" and record.requester_id == DEV
    assert record.reply_text == "@**Developer**\n\nok" and record.reply_after == 501


def test_a_third_party_speaking_during_the_run_is_not_who_the_reply_is_handed_to():
    client = ScriptedClient([human("build it", 1)])

    def handler(ctx):
        client.history.append(human("me too", 2, name="Bystander", sender=OTHER))
        return topics.TopicResult(["ok"])

    record = topics.serve_topic(client, "c", "t", handler, ack_text="ack", journal=serving.NullJournal(),
                                log=lambda t: None)
    assert record.reply_text.startswith("@**Developer**"), "the requester is read from the processed input"
    assert record.requester_id == DEV


def test_a_send_that_is_refused_escapes_with_the_reply_kept_prepared():
    client = ScriptedClient([human("build it", 1)], trouble=lambda c: "reject" if c.endswith("ok") else None)
    journal = serving.NullJournal()
    with pytest.raises(DeliveryError) as caught:
        topics.serve_topic(client, "c", "t", lambda ctx: topics.TopicResult(["ok"]), ack_text="ack",
                           journal=journal, log=lambda t: None)
    assert caught.value.terminal
    assert journal.serving().state == serving.PREPARED and journal.serving().reply_text == "@**Developer**\n\nok"


def test_a_dropped_send_is_retried_and_then_handed_back_with_the_reply_prepared():
    attempts = []
    client = ScriptedClient([human("build it", 1)],
                            trouble=lambda c: (attempts.append(1) or "error") if c.endswith("ok") else None)
    journal = serving.NullJournal()
    with pytest.raises(DeliveryError) as caught:
        topics.serve_topic(client, "c", "t", lambda ctx: topics.TopicResult(["ok"]), ack_text="ack",
                           journal=journal, log=lambda t: None, delivery={"sleep": lambda s: None})
    assert not caught.value.terminal and len(attempts) == 3
    assert journal.serving().state == serving.PREPARED
    # Delivery, not the run, is what a later pass repeats.
    client.trouble = lambda c: None
    message_id = topics.resume_prepared(client, journal.serving(), journal, log=lambda t: None)
    assert journal.serving().state == serving.DELIVERED and journal.serving().delivered_id == message_id
    assert client.sent == ["ack", "@**Developer**\n\nok"]
