"""robust_workflow p2 step 1: identity defects in pyagag, reproduced.

Each test states the required outcome and is `xfail(strict=True)` while the
defect stands; the fix removes the mark.
"""

from __future__ import annotations

import time

import pytest

from agag.listen import MENTION

from test_listen import DEV, OTHER, Harness, realm_with_channels, wait_until

defect = pytest.mark.xfail(strict=True, reason="reproduced in robust_workflow p2 step 1; not fixed yet")


def test_l1_a_served_callback_is_not_served_again_after_its_topic_is_renamed(tmp_path):
    """The served mark names the remote conversation by name; after a rename
    (a retire, an ordinary rename) a restart finds no mark for the new name
    and runs the finished exchange again."""
    realm = realm_with_channels()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    h = Harness(realm, tmp_path).start()
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** it is done", sender_id=OTHER, sender_name="autolab",
               flags=("mentioned",))
    wait_until(lambda: (MENTION, "pj-x", "workrun-1") in h.served, what="the mention serving")
    time.sleep(0.4)
    h.stop()
    realm.move(realm._topic_ids("pj-x", "workrun-1"), "workrun-1-renamed")
    h2 = Harness(realm, tmp_path).start()
    time.sleep(0.6)
    served_again = [s for s in h2.served if s[0] == MENTION]
    h2.stop()
    assert served_again == [], "the finished callback was served a second time under the new name"


def test_a_callback_is_not_lost_when_its_topic_moves_between_two_reads(tmp_path):
    """robust_workflow p2 step 5, trial B: autolab posted a task's closing
    report naming Front and ✔'d the topic within the second; Front's listener
    read the listing (the bare name still there), then the messages (moved
    by the ✔ in between), found nobody speaking and logged "nothing owed" —
    the report never came back. Here the second read comes back empty the
    same way; the mention is found by the id that triggered it."""
    realm = realm_with_channels()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    h = Harness(realm, tmp_path)
    real = h.listener._last_real
    raced = []

    def after_the_move(channel, live_name):
        if live_name == "workrun-1" and not raced:
            raced.append(live_name)
            return None
        return real(channel, live_name)

    h.listener._last_real = after_the_move
    h.start()
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** task done", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: (MENTION, "pj-x", "workrun-1") in h.served, what="the callback serving")
    h.stop()
