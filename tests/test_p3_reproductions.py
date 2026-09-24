"""robust_workflow p3 step 1: receipt gaps in pyagag, reproduced.

Each test states the required outcome. Step 1 committed them as
`xfail(strict=True)`; step 4 removed the marks with the fix and added the
tests after them.
"""

from __future__ import annotations

import time

import pytest

from agag import topics
from agag.listen import MENTION, OWNER
from agag.selfnote import parse_served

import test_listen
from test_serving_lifecycle import DEV, HOME, OTHER, Harness, realm_with_channels, wait_until

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")

def test_s1_an_answer_read_by_an_owner_serving_gets_a_receipt(tmp_path):
    """p2 trials A2/A3: a delegated answer arrived while the requester's
    home was about to be served by its owner route (a human's post, or
    Observer's request for a *different* answer). The owner serving read the
    answer in its threads and relayed it — and wrote no receipt, so the
    mention route served the same answer again and the run said "nothing
    new" (Front #9609, #9664)."""
    realm = realm_with_channels()
    answer = realm.post("pj-x", "task-x", "@**Mirror Bot** task done, committed", sender_id=OTHER,
                        sender_name="autolab", quiet=True)
    realm.post(HOME, "home", "what is the state of task-x?", sender_id=DEV, sender_name="Dev", quiet=True)
    h = Harness(realm, tmp_path)

    def reply(ctx):
        # The owner serving's input: its conversation and, as a thread, the
        # delegated conversation — rendered by the shared renderer.
        if (ctx.channel, ctx.topic) == (HOME, "home"):
            topics.write_threads(ctx.client, tmp_path / f"ws{len(h.contexts)}", [("pj-x", "task-x")], 42)
        return "task-x is done and committed"

    h.reply = reply
    h.start()
    wait_until(lambda: h.replies(HOME, "home"), what="the owner reply")
    time.sleep(0.8)
    served = list(h.contexts)
    receipts = [parse_served(c) for c in h.posts(HOME, "home") if c.startswith("[selfnote][served]")]
    h.stop()
    assert [(r[0].as_pair(), r[1]) for r in receipts] == [(("pj-x", "task-x"), answer)], \
        "the answer the owner serving read has no receipt"
    assert len(served) == 1, "the same answer was served a second time by the mention route"


def test_s2_a_callback_under_a_check_mark_is_recovered_at_restart(tmp_path):
    """p2 report, remaining limitations: startup recovery reads open topics
    only. A callback that arrived while the listener was down, in a topic
    its owner ✔'d right after it (autolab closes a task that way), is never
    queued again — it comes back only if Observer asks."""
    realm = test_listen.realm_with_channels()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=test_listen.DEV, sender_name="Dev",
               quiet=True)
    # The listener has run here before (that first start fixes its horizon)…
    test_listen.Harness(realm, tmp_path).start().stop()
    # …and the callback lands, and is ✔'d, while it is down — long enough
    # for its event queue to expire, so the restart re-reads the realm
    # instead of replaying the events.
    answer = realm.post("pj-x", "workrun-1", "@**Mirror Bot** task done", sender_id=test_listen.OTHER,
                        sender_name="autolab", quiet=True)
    realm.resolve("pj-x", "workrun-1", quiet=True)
    realm.expire_queue()
    h = test_listen.Harness(realm, tmp_path).start()
    wait_until(lambda: any(s[0] == MENTION for s in h.served), what="the recovered callback")
    time.sleep(0.5)
    served = [s for s in h.served if s[0] == MENTION]
    h.stop()
    assert len(served) == 1, f"the callback #{answer} under ✔ was not recovered at startup"
    assert all(s[0] != OWNER for s in h.served)


# --- step 4: beyond the reproductions ------------------------------------------------


def test_the_realm_s_older_callbacks_under_a_check_mark_are_not_replayed(tmp_path):
    """The horizon: the first start of this rule adopts the realm as it is.
    ✔ history from before receipts were reliable is not a queue of work."""
    realm = test_listen.realm_with_channels()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=test_listen.DEV, sender_name="Dev",
               quiet=True)
    realm.post("pj-x", "workrun-old", "@**Mirror Bot** done long ago", sender_id=test_listen.OTHER,
               sender_name="autolab", quiet=True)
    realm.resolve("pj-x", "workrun-old", quiet=True)
    h = test_listen.Harness(realm, tmp_path).start()
    time.sleep(0.6)
    h.stop()
    assert [s for s in h.served if s[0] == MENTION] == []
    assert any("recovered from #" in line for line in h.log)


def test_an_answer_arriving_after_the_thread_was_read_stays_owed(tmp_path):
    """The receipt covers what the serving was given: an answer posted
    while the run is in flight is newer than its thread and is served once
    more — by the mention route, for itself."""
    import threading

    realm = realm_with_channels()
    realm.post("pj-x", "task-x", "@**Mirror Bot** first report", sender_id=OTHER, sender_name="autolab", quiet=True)
    realm.post(HOME, "home", "state of task-x?", sender_id=DEV, sender_name="Dev", quiet=True)
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate)
    late = {}

    def reply(ctx):
        if (ctx.channel, ctx.topic) == (HOME, "home") and "late" not in late:
            topics.write_threads(ctx.client, tmp_path / "ws", [("pj-x", "task-x")], 42)
            late["late"] = realm.post("pj-x", "task-x", "@**Mirror Bot** second report", sender_id=OTHER,
                                      sender_name="autolab")
        return "noted"

    h.reply = reply
    h.start()
    gate.set()
    wait_until(lambda: len(h.contexts) >= 2, what="the late answer served on its own")
    time.sleep(0.5)
    receipts = [parse_served(c)[1] for c in h.posts(HOME, "home") if c.startswith("[selfnote][served]")]
    h.stop()
    assert receipts[0] < late["late"], "the first receipt covered an answer the serving was never given"
    assert receipts[-1] == late["late"]


def test_a_restart_between_delivery_and_receipt_writes_the_receipt_without_a_rerun(tmp_path):
    realm = realm_with_channels()
    answer = realm.post("pj-x", "task-x", "@**Mirror Bot** done", sender_id=OTHER, sender_name="autolab", quiet=True)
    realm.post(HOME, "home", "state of task-x?", sender_id=DEV, sender_name="Dev", quiet=True)
    h = Harness(realm, tmp_path)
    crashed = {}
    real = h.listener._mark_inputs

    def crash_before_receipt(record):
        if not crashed:
            crashed["at"] = record.id
            raise KeyboardInterrupt  # the process dying between delivery and receipt
        real(record)

    h.listener._mark_inputs = crash_before_receipt

    def reply(ctx):
        topics.write_threads(ctx.client, tmp_path / f"ws{len(h.contexts)}", [("pj-x", "task-x")], 42)
        return "task-x is done"

    h.reply = reply
    h.start()
    wait_until(lambda: crashed, what="the crash")
    h.crash()
    runs = len(h.contexts)
    h2 = Harness(realm, tmp_path)
    h2.reply = reply
    h2.start()
    wait_until(lambda: any(c.startswith("[selfnote][served]") for c in h2.posts(HOME, "home")), what="the receipt")
    time.sleep(0.6)
    h2.stop()
    assert runs == 1 and h2.contexts == [], "the serving was run again instead of its bookkeeping"
    assert [parse_served(c)[1] for c in h2.posts(HOME, "home") if c.startswith("[selfnote][served]")] == [answer]
