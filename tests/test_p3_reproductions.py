"""robust_workflow p3 step 1: receipt gaps in pyagag, reproduced.

Each test states the required outcome and is `xfail(strict=True)` while the
defect stands; the fix removes the mark.
"""

from __future__ import annotations

import time

import pytest

from agag import topics
from agag.listen import MENTION, OWNER
from agag.selfnote import parse_served

import test_listen
from test_serving_lifecycle import DEV, HOME, OTHER, Harness, realm_with_channels, wait_until

defect = pytest.mark.xfail(strict=True, reason="reproduced in robust_workflow p3 step 1; not fixed yet")


@defect
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


@defect
def test_s2_a_callback_under_a_check_mark_is_recovered_at_restart(tmp_path):
    """p2 report, remaining limitations: startup recovery reads open topics
    only. A callback that arrived while the listener was down, in a topic
    its owner ✔'d right after it (autolab closes a task that way), is never
    queued again — it comes back only if Observer asks."""
    realm = test_listen.realm_with_channels()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=test_listen.DEV, sender_name="Dev",
               quiet=True)
    answer = realm.post("pj-x", "workrun-1", "@**Mirror Bot** task done", sender_id=test_listen.OTHER,
                        sender_name="autolab", quiet=True)
    realm.resolve("pj-x", "workrun-1")
    h = test_listen.Harness(realm, tmp_path).start()
    time.sleep(0.8)
    served = [s for s in h.served if s[0] == MENTION]
    h.stop()
    assert len(served) == 1, f"the callback #{answer} under ✔ was not recovered at startup"
    assert all(s[0] != OWNER for s in h.served)
