"""Memo conversations are read and never answered (`argue` p2 step 1).

What is pinned, over the same mirrored fake realm `test_listen` uses: in a
memo channel a plain post, a real mention, a copied selfnote, a rename, a
resolve / un-resolve and a restart with work already queued all stay quiet —
on the owner route and the mention route — while an ordinary conversation
next to it is served exactly as before.
"""

from __future__ import annotations

import time

from agag import memo
from agag.listen import MENTION, OWNER
from agag.mirror.testing import FakeRealm
from agag.topics import serve_topic
from agag.zulip import is_mention_for_us, topic_from_event

from test_listen import BOT, DEV, OTHER, Harness, wait_until


def realm_with_memo() -> FakeRealm:
    realm = FakeRealm()
    realm.add_channel(35, "agents")
    realm.add_channel(6, "pj-x")
    realm.add_channel(8, "mirror-bot-x")  # this bot's own channel
    realm.add_channel(70, "memo")
    realm.add_channel(71, "memo-frontdesk")
    return realm


def test_the_channel_is_the_whole_distinction():
    assert memo.is_memo_channel("memo") and memo.is_memo_channel("memo-frontdesk") and memo.is_memo_channel("Memo")
    assert not memo.is_memo_channel("memorial") and not memo.is_memo_channel("argue") and not memo.is_memo_channel("")
    assert memo.is_memo_message({"display_recipient": "memo", "subject": "x"})
    assert not memo.is_memo_message({"display_recipient": [{"id": 1}]})  # a DM is not a memo


def test_the_source_link_is_its_own_note_and_the_record_round_trips():
    line = memo.source_note(7012)
    assert line == "[selfnote][memosource] 7012" and memo.parse_source(line) == 7012
    assert memo.parse_source("[selfnote][rootchat] front/front-x") is None
    topic = memo.memo_topic(7012, "argue-A very long stem " + "x" * 80)
    assert topic.endswith("-s7012") and len(topic) <= memo.TOPIC_LIMIT
    payload = {"schema": "x.v1", "turns": [{"text": "@**Mirror Bot** 「こんにちは」"}]}
    assert memo.parse_record("before\n" + memo.render_record(payload) + "\nafter") == payload
    assert memo.parse_record("no record here") is None


def test_the_fingerprint_sees_an_edit_and_not_an_order():
    a = memo.fingerprint([(2, "two"), (1, "one")])
    assert a == memo.fingerprint([(1, "one"), (2, "two")]) and a.startswith("sha256:")
    assert a != memo.fingerprint([(1, "one"), (2, "two!")]) and a != memo.fingerprint([(1, "one")])
    assert memo.render_request_note(" abc123 ") == "[selfnote][render] abc123"


def test_nothing_written_in_a_memo_is_served(tmp_path):
    realm = realm_with_memo()
    h = Harness(realm, tmp_path).start()
    # An owned prefix, an owner-looking name, a real mention, a command line,
    # a copied selfnote: each would be work anywhere else.
    realm.post("memo", "workplan-looks-owned", "please plan this", sender_id=DEV, sender_name="Dev")
    realm.post("memo", "argue-x-s1", "@**Mirror Bot** what do you think?", sender_id=OTHER, sender_name="Front",
               flags=("mentioned",))
    realm.post("memo-frontdesk", "desk-s2", "@**Mirror Bot** use agy", sender_id=DEV, sender_name="Dev",
               flags=("mentioned",))
    realm.post("memo", "argue-x-s1", "[selfnote][rootchat] mirror-bot-x/home", sender_id=BOT, sender_name="Mirror Bot")
    realm.post("memo", "argue-x-s1", "```ag-memo\n{\"text\": \"@**Mirror Bot** quoted\"}\n```", sender_id=OTHER,
               sender_name="Front")
    # The ordinary conversation beside it is the control: served as ever.
    realm.post("pj-x", "workplan-real", "please plan the real one", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: (OWNER, "pj-x", "workplan-real") in h.served, what="the ordinary serving")
    time.sleep(0.5)
    assert h.served == [(OWNER, "pj-x", "workplan-real")]
    assert len(h.listener.queue) == 0
    h.stop()


def test_a_rename_a_resolve_and_an_unresolve_in_a_memo_stay_quiet(tmp_path):
    realm = realm_with_memo()
    first = realm.post("memo", "scratch", "a line", sender_id=DEV, sender_name="Dev")
    h = Harness(realm, tmp_path).start()
    realm.move([first], "workplan-renamed-into-an-owned-name")
    realm.resolve("memo", "workplan-renamed-into-an-owned-name")
    realm.move([first], "workplan-renamed-into-an-owned-name")  # un-resolved again
    realm.post("pj-x", "workplan-real", "control", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: (OWNER, "pj-x", "workplan-real") in h.served, what="the control serving")
    time.sleep(0.4)
    assert h.served == [(OWNER, "pj-x", "workplan-real")]
    h.stop()


def test_a_restart_drops_memo_work_an_older_process_queued(tmp_path):
    realm = realm_with_memo()
    realm.post("memo", "workplan-queued", "queued before the rule existed", sender_id=DEV, sender_name="Dev")
    realm.post("memo", "workplan-running", "was running at the crash", sender_id=DEV, sender_name="Dev")
    realm.post("memo", "argue-x-s1", "@**Mirror Bot** named here", sender_id=OTHER, sender_name="Front")
    h = Harness(realm, tmp_path).start()
    h.stop()
    # What an older listener would have left in the queue file.
    queue = h.listener.queue
    queue.enqueue("memo", "workplan-queued", OWNER, revision=1, message_id=1)
    queue.enqueue("memo", "argue-x-s1", MENTION, revision=1, message_id=3)
    queue.enqueue("memo", "workplan-running", OWNER, revision=1, message_id=2)
    running = queue.take()
    assert running is not None and queue.entries("running")
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: len(h2.listener.queue) == 0, what="the memo entries to be dropped")
    time.sleep(0.3)
    assert h2.served == []
    assert any("is a memo; dropped" in line for line in h2.log)
    assert sum("nothing owed now" in line for line in h2.log) == 2
    h2.stop()


def test_a_selfnote_copied_into_a_memo_is_nobodys_memory(tmp_path):
    realm = realm_with_memo()
    realm.post("mirror-bot-x", "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post("mirror-bot-x", "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    # A served mark far above anything real, copied into a memo by this very
    # bot: were it read, the mention below would look answered forever.
    realm.post("memo", "argue-x-s1", "[selfnote][served] pj-x/workrun-1 999999", sender_id=BOT,
               sender_name="Mirror Bot", quiet=True)
    realm.post("memo", "argue-x-s1", "[selfnote][memosource] 1", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    h = Harness(realm, tmp_path).start()
    assert h.listener.served_marks() == {}
    assert [n.tag for n in h.mirror.notes(channel="memo")] == []
    assert sorted(n.tag for n in h.mirror.notes(channel="memo", include_memos=True)) == ["memosource", "served"]
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** it is done", sender_id=OTHER, sender_name="autolab",
               flags=("mentioned",))
    wait_until(lambda: (MENTION, "pj-x", "workrun-1") in h.served, what="the real mention")
    h.stop()


def test_the_event_path_helpers_and_serve_topic_refuse_a_memo():
    message = {"type": "stream", "display_recipient": "memo", "subject": "workplan-x", "sender_id": DEV,
               "content": "@**Mirror Bot** plan", "id": 5}
    assert topic_from_event(message, BOT, "workplan-") is None
    assert not is_mention_for_us(message, BOT, ["mentioned"], "Mirror Bot")
    ordinary = {**message, "display_recipient": "pj-x"}
    assert topic_from_event(ordinary, BOT, "workplan-") == ("pj-x", "workplan-x")
    assert is_mention_for_us(ordinary, BOT, ["mentioned"], "Mirror Bot")

    class Untouchable:
        def __getattr__(self, name):
            raise AssertionError(f"a memo serving must not reach Zulip ({name})")

    called = []
    serve_topic(Untouchable(), "memo", "workplan-x", lambda context: called.append(context), ack_text="ack",
                log=lambda line: None)
    serve_topic(Untouchable(), "pj-x", "workplan-x", lambda context: called.append(context), ack_text="ack",
                reply_to=("memo", "argue-x-s1"), log=lambda line: None)
    assert called == []
