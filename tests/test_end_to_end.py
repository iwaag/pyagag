"""Request → delegation → callback → final response, with an interruption,
over local fixtures (`explicit_reply` p1 step 5).

One bot (Front's shape) is served in its own conversation, delegates by
posting into another agent's topic with a root note carrying the anchor of
the request, ends its serving with a marked reply and an `ag-continue`
block, dies between the delegate's answer and its own reply, is restarted,
and answers the developer from the callback with the delegate's result —
once, at home, with the served mark bound to the delegate's post and the
continuation note read back. The other agent is simulated by the test
posting as it.

The metrics the plan asks for are asserted, not eyeballed: replies lost 0,
duplicate deliveries 0, model runs exactly one per serving (no rerun of a
completed run), and the recovery delay measured from the restart.
"""

from __future__ import annotations

import threading
import time

import pytest

from agag import serving, topics
from agag.continuation import continuation_view, latest_continuation, Remote
from agag.listen import MENTION, OWNER
from agag.selfnote import Conversation, parse_rootchat, parse_served, rootchat_note
from agag.zulip import remotes_for_home
from test_handoff_binding import Harness as _Harness, RealmClient as _RealmClient
from test_serving_lifecycle import ACK, BOT, DEV, HOME, OTHER, realm_with_channels, wait_until
from endmark import plain

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")

DELEGATE = ("pj-x", "workrun-trailer")


class RealmClient(_RealmClient):
    """…with the narrow `remotes_for_home` reads, so the bot can find its
    own delegations the way Front does."""

    def _own(self, tag):
        return [dict(m) for m in sorted(self.realm.messages.values(), key=lambda m: m["id"])
                if m["sender_id"] == BOT and m["content"].startswith(f"[selfnote][{tag}]")]

    def own_rootchat_notes(self, num_before=200):
        return self._own("rootchat")

    def own_moved_notes(self, num_before=200):
        return []


def test_request_delegation_callback_and_final_response_survive_a_restart(tmp_path):
    realm = realm_with_channels()
    runs: list[tuple[str, dict]] = []
    gate = threading.Event()
    box: dict = {}  # the harness now serving: the one before the crash, then the one after

    def model(ctx):
        """The bot's run: delegate on the first serving, answer on the
        callback. Records every run so a rerun is visible."""
        runs.append(("run", {"home": (ctx.channel, ctx.topic), "up_to": ctx.processed_up_to,
                             "previous": ctx.previous, "threads": ctx.extra_threads}))
        remotes = [Remote(c, [dict(m) for m in sorted(realm.messages.values(), key=lambda m: m["id"])
                             if m["display_recipient"] == c.channel and m["subject"] == c.topic], c.topic)
                   for c in remotes_for_home(box["h"].client, ctx.channel, ctx.topic)]
        view = continuation_view(ctx.history, BOT, remotes=remotes, interrupted=ctx.previous)
        answered = [r for r in remotes if any(m["sender_id"] != BOT and "@**Mirror Bot**" in m["content"]
                                              for m in r.messages)]
        if not answered:
            # First serving: delegate, as `agentchat send` would — the root
            # note with the anchor of the request, then the request.
            home = Conversation(ctx.channel, ctx.topic, ctx.journal.trigger_id)
            realm.post(*DELEGATE, rootchat_note(home), sender_id=BOT, sender_name="Mirror Bot")
            realm.post(*DELEGATE, "please cut a 30 s trailer and tell me when it is done", sender_id=BOT,
                       sender_name="Mirror Bot")
            return topics.TopicResult(output=(
                "I'll delegate the cut and report back.\n\n"
                "```ag-reply\nAsked autolab for the cut in #pj-x › workrun-trailer; I'll report when it answers.\n```\n"
                "```ag-continue\ngoal: a 30 s trailer for the developer\nnext: when autolab answers, tell the developer\n```"))
        gate.wait(10.0)
        if box["h"].listener._stop.is_set():
            raise SystemExit  # the crash: the process dies mid-run
        assert "answered, not yet dealt with" in view, view
        assert "goal: a 30 s trailer for the developer" in view
        result = answered[0].messages[-1]["content"]
        return topics.TopicResult(output=f"```ag-reply\nautolab is done: {result.split(chr(10))[-1]}\n```")

    def handler(channel, topic):
        h = box["h"]
        topics.serve_topic(h.client, channel, topic, model, ack_text=ACK, log=h.log.append, delivery=h.delivery)

    def mention(channel, topic):
        h = box["h"]
        topics.serve_topic(h.client, HOME, "front-1", model, ack_text=ACK, log=h.log.append,
                           extra_threads=((channel, topic),), delivery=h.delivery)

    class E2E(_Harness):
        def __init__(self, realm, root, **kw):
            super().__init__(realm, root, **kw)
            self.client.__class__ = RealmClient
            self.listener.handler = handler
            self.listener.on_mention = mention
            box["h"] = self

    # --- the request ---------------------------------------------------------------
    h = E2E(realm, tmp_path).start()
    asked = realm.post(HOME, "front-1", "make me a 30 s trailer", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.replies(HOME, "front-1")) == 1, what="the first reply")
    first = plain(h.replies(HOME, "front-1")[0])
    assert first == "@**Dev**\n\nAsked autolab for the cut in #pj-x › workrun-trailer; I'll report when it answers."
    assert "I'll delegate" not in first, "the thought is not posted"
    note = [m for m in realm.messages.values() if m["subject"] == DELEGATE[1] and m["content"].startswith("[selfnote][rootchat]")]
    assert parse_rootchat(note[0]["content"]).anchor == asked, "the delegation is bound to the request by id"
    assert latest_continuation(h.client.topic_history(HOME, "front-1", 50), BOT).get("goal") == "a 30 s trailer for the developer"
    assert len(runs) == 1

    # --- the delegate answers; the bot dies mid-callback -----------------------------------
    done = realm.post(*DELEGATE, "@**Mirror Bot** the cut is done\nv1.mp4 is in result/", sender_id=OTHER,
                      sender_name="autolab")
    wait_until(lambda: len(runs) == 2, what="the callback serving to start")
    assert runs[1][1]["threads"] == (DELEGATE,)
    crashed_at = time.time()
    h.crash()
    assert [(e.route, e.state) for e in h.listener.queue.entries()] == [(MENTION, "running")]
    assert len(h.replies(HOME, "front-1")) == 1, "nothing was posted by the dying run"

    # --- restart: the callback is served again, once, and answered at home -------------------
    gate.set()
    h2 = E2E(realm, tmp_path).start()
    wait_until(lambda: len(h2.replies(HOME, "front-1")) == 2, what="the final response after the restart")
    recovered_in = time.time() - crashed_at
    final = h2.replies(HOME, "front-1")[1]
    assert final == "@**Dev**\n\nautolab is done: v1.mp4 is in result/"
    assert runs[2][1]["previous"] is not None and runs[2][1]["previous"].state == serving.INTERRUPTED
    wait_until(lambda: [c for c in h2.posts(HOME, "front-1") if c.startswith("[selfnote][served]")], what="the served mark")
    marks = [parse_served(c) for c in h2.posts(HOME, "front-1") if c.startswith("[selfnote][served]")]
    assert marks == [(Conversation(*DELEGATE), done)], "the mark names the delegate's post that was answered"
    time.sleep(0.5)

    # --- the metrics ---------------------------------------------------------------------
    replies = h2.replies(HOME, "front-1")
    assert len(replies) == 2, f"lost or duplicated replies: {replies}"
    assert len(set(replies)) == 2, "no duplicate delivery"
    assert len(runs) == 3, "one run per serving; the interrupted run is the only one repeated"
    assert not any(m["sender_id"] == BOT and m["subject"] == DELEGATE[1] and not m["content"].startswith("[selfnote]")
                   and "trailer" not in m["content"] for m in realm.messages.values()), \
        "nothing was posted back into the delegate's topic: no reciprocal loop"
    assert len(h2.listener.queue) == 0
    assert recovered_in < 10.0, f"recovery took {recovered_in:.1f}s"
    print(f"\nrecovery delay after the restart: {recovered_in:.2f}s; runs: {len(runs)}; replies: {len(replies)}")
    h2.stop()
