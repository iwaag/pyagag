"""The whole sequence through a real listener (`clearer_chat_ui` step 5).

human request → progress → question → human response → report, with a
second question, another agent interrupting, input arriving during a run,
a crash before a question is sent and a restart, and a stale source — all
through `agag.listen` and `serve_topic` over a mirrored fake realm, reading
the outstanding requests the way every room does (`agag.outstanding`).
"""

from __future__ import annotations

import time

import pytest

from agag import topics
from agag.outstanding import read_requests
from agag.post import PROGRESS, PostMeta, compose, parse_post

from test_serving_lifecycle import ACK, BOT, DEV, OTHER, Harness, realm_with_channels, wait_until
from endmark import plain

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")

TOPIC = "workplan-e2e"


def said(ctx, text):
    return any(m.get("sender_id") == DEV and parse_post(m.get("content")).text == text for m in ctx.history)


def mine(ctx, words):
    return next(m["id"] for m in reversed(ctx.history) if m.get("sender_id") == BOT and words in m.get("content", ""))


def conversation(h):
    rows = [dict(m) for m in sorted(h.realm.messages.values(), key=lambda m: m["id"])
            if m["display_recipient"] == "pj-x" and m["subject"] in (TOPIC, f"✔ {TOPIC}")]
    return read_requests(rows, is_ack=lambda content: content == ACK)


def agent(ctx):
    """What the agent says, decided from the conversation it was handed."""
    last = next(m for m in reversed(ctx.history) if m.get("sender_id") != BOT and not m["content"].startswith("[selfnote]"))
    if last.get("sender_id") == OTHER:
        return topics.TopicResult(output="```ag-reply intent=report\nNoted, autolab; nothing changes for the Developer.\n```")
    if said(ctx, "done"):
        return topics.TopicResult(output="```ag-reply intent=report\ndone — the logo is blue and big.\n```")
    if said(ctx, "and make it big"):
        # The serving that read the input which arrived mid-run: that input
        # answered the size question, so the asker withdraws it.
        asked = mine(ctx, "Which size?")
        return topics.TopicResult(output=f"```ag-reply intent=report re={asked}\nSize: big, as you said.\n```")
    if said(ctx, "blue"):
        # Input arriving while this run works: posted before the reply.
        ctx.client.realm.post("pj-x", TOPIC, "and make it big", sender_id=DEV, sender_name="Dev")
        return topics.TopicResult(output="```ag-reply intent=response_request ask=question\nWhich size?\n```")
    if said(ctx, "build a logo"):
        ctx.client.send_to_channel("pj-x", TOPIC, compose("Reading the brief.", PostMeta(intent=PROGRESS)))
        return topics.TopicResult(output="```ag-reply intent=response_request ask=question\nWhich colour?\n```")
    return topics.TopicResult(output="```ag-reply intent=report\ndone\n```")


def test_request_progress_question_answer_report_with_every_fault(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path, reply=agent)
    # A crash right before the first question is sent.
    h.client.trouble = lambda content: "crash" if "Which colour?" in content else None
    h.start()
    realm.post("pj-x", TOPIC, "build a logo", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: not h.thread.is_alive() or any("Reading the brief." in c for c in h.posts("pj-x", TOPIC)),
               what="the progress post")
    time.sleep(0.5)
    h.crash()
    assert conversation(h).requests == [], "the question was never sent: nobody is asked anything"

    # Restart: the prepared question is delivered once, without a new run.
    h2 = Harness(realm, tmp_path, reply=agent).start()
    wait_until(lambda: len(conversation(h2).pending) == 1, what="the redelivered question")
    time.sleep(0.3)
    assert h2.contexts == [] and sum("Which colour?" in c for c in h2.posts("pj-x", TOPIC)) == 1
    colour = conversation(h2).pending[0]
    assert (colour.ask, colour.to) == ("question", DEV)
    progress = [c for c in h2.posts("pj-x", TOPIC) if "Reading the brief." in c]
    assert parse_post(progress[0]).meta.intent == PROGRESS

    # Another agent interrupts: it is answered, and the question still waits.
    realm.post("pj-x", TOPIC, "I can draw it later.", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: any("Noted, autolab" in c for c in h2.posts("pj-x", TOPIC)), what="the reply to autolab")
    assert [r.id for r in conversation(h2).pending] == [colour.id]

    # The human answers; input arrives while that run works.
    realm.post("pj-x", TOPIC, "blue", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: any("Size: big" in c for c in h2.posts("pj-x", TOPIC)), what="the serving of the mid-run input")
    time.sleep(0.3)
    found = conversation(h2).by_id()
    assert found[colour.id].state == "answered" and found[colour.id].how == "next_post"
    size = next(r for r in found.values() if "Which size?" in r.text)
    assert size.state == "withdrawn", "the mid-run input answered it; the asker said so"
    assert conversation(h2).pending == []

    # The Developer finishes; the report asks nothing.
    realm.post("pj-x", TOPIC, "done", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: plain(h2.posts("pj-x", TOPIC)[-1]).endswith("`ag-post intent=report`")
               and "done" in parse_post(h2.posts("pj-x", TOPIC)[-1]).text, what="the report")
    before = conversation(h2).as_dict()
    h2.stop()

    # A restart reconstructs the same answer from the same record.
    h3 = Harness(realm, tmp_path, reply=agent).start()
    time.sleep(0.4)
    assert h3.contexts == [] and conversation(h3).as_dict() == before
    h3.stop()

    # A stale source says so rather than presenting the states as current.
    rows = [dict(m) for m in realm.messages.values() if m["subject"] == TOPIC]
    stale = read_requests(rows, stale=True, is_ack=lambda content: content == ACK)
    assert stale.pending == [] and any("stale" in why for why in stale.uncertain)
