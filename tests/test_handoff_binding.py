"""Handoffs are bound to requests, not to display names or to whoever spoke
last (`explicit_reply` p1 step 3).

Pinned over the same listener-plus-`serve_topic` harness as the lifecycle
tests: the reply follows the request's message id to a renamed or resolved
topic; a destination that is gone is a terminal failure out loud; a reused
topic name does not hide a new request behind an old mark; two delegates
answering at once are two servings with two marks; a repeated callback is
served once per mention; an unreadable reply conversation leaves the
handoff pending; the root note a run writes elsewhere carries the anchor of
the post it serves, and a callback is located by it.
"""

from __future__ import annotations

import threading
import time

import pytest

from agag import agent, serving, topics
from agag.listen import MENTION, OWNER
from agag.selfnote import Conversation, parse_rootchat, parse_served, rootchat_note
from agag.zulip import locate
from test_serving_lifecycle import (  # noqa: F401 - the harness
    ACK, BOT, DEV, HOME, OTHER, Harness as _Harness, RealmClient as _RealmClient, realm_with_channels, wait_until,
)

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


class RealmClient(_RealmClient):
    def message(self, message_id, *, strict=False):
        found = self.realm.messages.get(int(message_id))
        return dict(found) if found is not None else None

    def users(self):
        return []


class Harness(_Harness):
    def __init__(self, realm, root, **kw):
        super().__init__(realm, root, **kw)
        self.client.__class__ = RealmClient  # the same client, with the lookup


def served_notes(h, channel, topic):
    return [parse_served(c) for c in h.posts(channel, topic) if c.startswith("[selfnote][served]")]


# --- location by id ---------------------------------------------------------------


def test_a_topic_renamed_during_the_run_still_receives_the_reply(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    asked = realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 1, what="the run")
    ids = sorted(i for i, m in realm.messages.items() if m["subject"] == "workplan-a")
    realm.move(ids, "workplan-a-renamed")
    gate.set()
    wait_until(lambda: h.replies("pj-x", "workplan-a-renamed") == ["@**Dev**\n\nthe answer"], what="the reply")
    assert h.replies("pj-x", "workplan-a") == [], "nothing was posted under the old name"
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    assert record.reply_topic == "workplan-a-renamed" and record.trigger_id == asked
    assert record.extra["destination"]["state"] == "renamed"
    h.stop()


def test_a_topic_resolved_during_the_run_receives_the_reply_under_its_closed_name(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 1, what="the run")
    realm.resolve("pj-x", "workplan-a")
    gate.set()
    wait_until(lambda: h.replies("pj-x", "workplan-a") == ["@**Dev**\n\nthe answer"], what="the reply")
    assert [m["subject"] for m in realm.messages.values() if m["content"] == "@**Dev**\n\nthe answer"] == ["✔ workplan-a"]
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    assert record.extra["destination"]["state"] == "resolved"
    assert not any(m["subject"] == "workplan-a" for m in realm.messages.values()), "no twin was opened"
    h.stop()


def test_a_destination_that_is_gone_is_a_terminal_failure_out_loud(tmp_path):
    realm = realm_with_channels()
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    asked = realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 1, what="the run")
    for ident in [i for i, m in realm.messages.items() if m["subject"] == "workplan-a"]:
        realm.delete(ident)
    gate.set()
    wait_until(lambda: h.listener.queue.entries("failed"), what="the failed entry")
    entry = h.listener.queue.entries("failed")[0]
    assert "no longer exists" in entry.failure
    record = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
    assert record.extra["destination"] == {"state": "gone", "asked": "pj-x/workplan-a", "found": None}
    assert record.state == serving.FAILED and asked not in realm.messages
    assert not any(m["sender_id"] == BOT and m["content"] != ACK for m in realm.messages.values()), \
        "nothing was posted under whatever might take the name"
    h.stop()


def test_a_reused_topic_name_does_not_hide_a_new_request_behind_an_old_mark(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post(HOME, "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    h = Harness(realm, tmp_path).start()
    first = realm.post("pj-x", "workrun-1", "@**Mirror Bot** done with the first", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: served_notes(h, HOME, "home") == [(Conversation("pj-x", "workrun-1"), first)], what="the mark")
    realm.resolve("pj-x", "workrun-1")
    # Somebody opens a new conversation under the freed name.
    second = realm.post("pj-x", "workrun-1", "@**Mirror Bot** a new thing under the old name", sender_id=OTHER,
                        sender_name="autolab")
    wait_until(lambda: len(served_notes(h, HOME, "home")) == 2, what="the second serving's mark")
    assert served_notes(h, HOME, "home")[-1] == (Conversation("pj-x", "workrun-1"), second)
    assert len(h.contexts) == 2
    h.stop()


# --- delegates and callbacks ------------------------------------------------------------


def test_two_delegates_answering_at_once_are_two_servings_with_two_marks(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post(HOME, "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    a = realm.post("pj-x", "workrun-a", "@**Mirror Bot** a is done", sender_id=OTHER, sender_name="autolab")
    b = realm.post("pj-x", "workrun-b", "@**Mirror Bot** b is done", sender_id=OTHER, sender_name="forge")
    wait_until(lambda: len(h.contexts) == 1, what="the first callback")
    gate.set()
    wait_until(lambda: len(served_notes(h, HOME, "home")) == 2, what="both marks")
    marks = dict(served_notes(h, HOME, "home"))
    assert marks == {Conversation("pj-x", "workrun-a"): a, Conversation("pj-x", "workrun-b"): b}
    assert len(h.contexts) == 2 and len(h.replies(HOME, "home")) == 2
    # Each reply at home is handed to the developer — the home's requester —
    # never to the delegate that called, so no reciprocal reply loop starts.
    assert all(r.startswith("@**Dev**") for r in h.replies(HOME, "home"))
    assert not any(m["sender_id"] == BOT and m["subject"].startswith("workrun-") for m in realm.messages.values())
    h.stop()


def test_a_repeated_callback_from_one_delegate_is_served_once_per_mention(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post(HOME, "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    h = Harness(realm, tmp_path).start()
    first = realm.post("pj-x", "workrun-1", "@**Mirror Bot** step one done", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: served_notes(h, HOME, "home") == [(Conversation("pj-x", "workrun-1"), first)], what="mark 1")
    realm.post("pj-x", "workrun-1", "(working on step two)", sender_id=OTHER, sender_name="autolab")
    time.sleep(0.4)
    assert len(h.contexts) == 1, "a post that does not name the bot is not a callback"
    second = realm.post("pj-x", "workrun-1", "@**Mirror Bot** step two done", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: len(served_notes(h, HOME, "home")) == 2, what="mark 2")
    assert served_notes(h, HOME, "home")[-1][1] == second and len(h.contexts) == 2
    h.stop()


def test_a_third_party_interjecting_at_home_during_a_callback_is_not_the_addressee(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, "home", "my own conversation", sender_id=DEV, sender_name="Dev", quiet=True)
    realm.post(HOME, "home", "on it", sender_id=BOT, sender_name="Mirror Bot", quiet=True)
    gate = threading.Event()
    h = Harness(realm, tmp_path, gate=gate).start()
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** done", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: len(h.contexts) == 1, what="the callback")
    realm.post(HOME, "home", "just passing by", sender_id=OTHER, sender_name="Bystander")
    gate.set()
    wait_until(lambda: h.replies(HOME, "home"), what="the home reply")
    assert h.replies(HOME, "home")[0].startswith("@**Dev**")
    # The bystander's post is unprocessed input at home and is served next.
    wait_until(lambda: len(h.replies(HOME, "home")) == 2, what="the bystander answered")
    assert h.replies(HOME, "home")[1].startswith("@**Bystander**")
    h.stop()


# --- the skeleton alone ------------------------------------------------------------------


class ScriptedClient:
    email = "bot@example.invalid"

    def __init__(self, histories, *, unreadable=()):
        self.histories = histories
        self.unreadable = set(unreadable)
        self.sent = []

    def whoami(self):
        return {"user_id": BOT, "full_name": "Bot"}

    def topic_history(self, channel, topic, num_before):
        if (channel, topic) in self.unreadable:
            from agag.zulip import ZulipError

            raise ZulipError("down")
        return list(self.histories.get((channel, topic), []))

    def send_to_channel(self, channel, topic, content):
        self.sent.append((channel, topic, content))
        return 700 + len(self.sent)


def test_an_unreadable_reply_conversation_leaves_the_handoff_pending_before_any_run():
    from agag.delivery import DeliveryError

    ran = []
    client = ScriptedClient({("h", "home"): [{"id": 1, "sender_id": DEV, "sender_full_name": "Dev", "content": "hi"}]},
                            unreadable={("r", "remote")})
    with pytest.raises(DeliveryError) as caught:
        topics.serve_topic(client, "h", "home", lambda ctx: ran.append(1) or topics.TopicResult(["x"]),
                           ack_text="ack", reply_to=("r", "remote"), journal=serving.NullJournal(), log=lambda t: None)
    assert not caught.value.terminal and ran == [] and client.sent == []


def test_the_reply_elsewhere_names_the_requester_there_as_it_stood_before_the_run():
    client = ScriptedClient({
        ("h", "home"): [{"id": 1, "sender_id": DEV, "sender_full_name": "Dev", "content": "hi"}],
        ("r", "remote"): [{"id": 5, "sender_id": OTHER, "sender_full_name": "autolab", "content": "@**Bot** ?"}],
    })

    def handler(ctx):
        client.histories[("r", "remote")].append(
            {"id": 9, "sender_id": 77, "sender_full_name": "Latecomer", "content": "me too"})
        return topics.TopicResult(["answer"])

    record = topics.serve_topic(client, "h", "home", handler, ack_text="ack", reply_to=("r", "remote"),
                                journal=serving.NullJournal(), log=lambda t: None)
    assert client.sent == [("r", "remote", "@**autolab**\n\nanswer")]
    assert record.requester_id == OTHER and record.requester_name == "autolab"


# --- the anchor in the root note ----------------------------------------------------------


def test_the_run_environment_carries_the_anchor_of_the_post_it_serves(tmp_path):
    spec = agent.AgentSpec("agtest", tmp_path, plan_prefix="plan-", run_prefix="run-")
    plain = agent.chat_environment(spec, home=("front", "front-1"), bin_dir=tmp_path)
    assert plain["AGENTCHAT_HOME"] == "front/front-1" and "AGENTCHAT_HOME_ANCHOR" not in plain
    with serving.bound(serving.NullJournal(trigger_id=7225)):
        bound = agent.chat_environment(spec, home=("front", "front-1"), bin_dir=tmp_path)
    assert bound["AGENTCHAT_HOME"] == "front/front-1" and bound["AGENTCHAT_HOME_ANCHOR"] == "7225"


def test_a_root_note_with_an_anchor_round_trips_and_is_located_by_it():
    home = Conversation("front", "front-1", 7225)
    note = rootchat_note(home)
    assert note == "[selfnote][rootchat] front/front-1 #7225"
    parsed = parse_rootchat(note)
    assert parsed == Conversation("front", "front-1") and parsed.anchor == 7225
    assert parse_rootchat("[selfnote][rootchat] front/front-1") == home and parse_rootchat(
        "[selfnote][rootchat] front/front-1").anchor is None

    class Moved:
        def message(self, message_id, *, strict=False):
            return {"id": message_id, "display_recipient": "front", "subject": "✔ front-1-renamed"}

        def stream_id(self, name):
            return 1

        def channel_topics(self, stream_id):
            return ["front-1"]

    class Gone(Moved):
        def message(self, message_id, *, strict=False):
            return None

    assert locate(Moved(), home) == Conversation("front", "✔ front-1-renamed")
    assert locate(Gone(), home) == Conversation("front", "front-1"), "a gone anchor falls back to the name"
    assert locate(Moved(), Conversation("front", "front-1")) == Conversation("front", "front-1"), \
        "without an anchor the name decides, across the resolve rename"
