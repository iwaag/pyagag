"""A run that produced no usable reply leaves its input owed (`failsafe` p3).

p2's #12509: Front's run wrote its reply with an opener the splitter could
not read, the repair wrote the same opener, and the failure line went out as
a closing report — the callback it had been given got its served mark, and
nothing owed the requester anything afterwards. What is pinned here, through
the real listener and journal: a first failure writes no receipt, says the
reply is still owed without closing the serving, and the input is served once
more after a pause with the failed output in front of the run; that serving
settles the debt with a reply or with the last failure, which closes the
serving and is the record the monitor escalates. Exactly two runs, across a
restart too.
"""

from __future__ import annotations

import time

from agag import reply as reply_module, serving, topics
from agag.listen import MENTION, OWNER
from agag.selfnote import parse_served

from test_serving_lifecycle import ACK, DEV, HOME, OTHER, Harness, realm_with_channels, wait_until


def outputs(*texts):
    """A reply function answering each run with the next output, in turn,
    and recording the prompt it would have been given."""
    queue = list(texts)
    prompts: list[str] = []

    def reply(ctx):
        prompts.append(topics.prompt_with_guide(["placement"], "guide", reply=True))
        return topics.TopicResult(output=queue.pop(0) if queue else "<ag-reply>\nextra run\n</ag-reply>")

    reply.prompts = prompts
    return reply


def test_the_input_is_served_again_with_the_failed_output_and_answered_once(tmp_path):
    realm = realm_with_channels()
    reply = outputs("I asked autolab in #88. Done.", "<ag-reply intent=report>\nAsked autolab (#88).\n</ag-reply>")
    h = Harness(realm, tmp_path, reply=reply).start()
    realm.post("pj-x", "workplan-a", "please ask autolab", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.contexts) == 2, what="the re-serving")
    wait_until(lambda: any("Asked autolab (#88)." in r for r in h.replies("pj-x", "workplan-a")), what="the reply")
    said = h.posts("pj-x", "workplan-a")
    failure = next(p for p in said if "(this run produced no reply" in p)
    assert "the reply is still owed" in failure and "intent=progress" in failure and "end=" not in failure
    final = next(p for p in said if "Asked autolab (#88)." in p)
    assert "end=" in final and final.startswith("@**Dev**")
    assert "I asked autolab in #88. Done." not in reply.prompts[0]
    assert "I asked autolab in #88. Done." in reply.prompts[1] and "Do not repeat any of it" in reply.prompts[1]
    assert any("serving the input again" in line for line in h.log)
    time.sleep(0.6)
    assert len(h.contexts) == 2, "settled: no third run"
    records = h.listener.queue.servings()
    assert [r.extra.get("reply_owed", {}).get("settled_by") for r in records if "reply_owed" in r.extra] \
        and all(not reply_module.owed_reply(r) for r in records)
    h.stop()


def test_two_failures_on_one_input_are_the_last_and_close_the_serving(tmp_path):
    realm = realm_with_channels()
    reply = outputs("prose", "more prose")
    h = Harness(realm, tmp_path, reply=reply).start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: sum("(this run produced no reply" in p for p in h.posts("pj-x", "workplan-a")) == 2,
               what="two failures")
    last = [p for p in h.posts("pj-x", "workplan-a") if "(this run produced no reply" in p][-1]
    assert "the input stays unanswered and is reported" in last and "intent=report" in last and "end=" in last
    time.sleep(0.6)
    assert len(h.contexts) == 2, "bounded: two runs for one input"
    h.stop()


def test_a_callback_keeps_its_receipt_until_the_reply_is_made(tmp_path):
    realm = realm_with_channels()
    reply = outputs("thinking only", "<ag-reply intent=report>\nautolab reports: done.\n</ag-reply>")
    h = Harness(realm, tmp_path, reply=reply).start()
    realm.post("pj-x", "workrun-1", "@**Mirror Bot** done: all tests pass", sender_id=OTHER, sender_name="autolab")
    wait_until(lambda: any("(this run produced no reply" in p for p in h.posts(HOME, "home")), what="the failure")
    marks = [p for p in h.posts(HOME, "home") if parse_served(p) is not None]
    assert marks == [], "no receipt for a callback nobody answered"
    wait_until(lambda: any("autolab reports: done." in r for r in h.replies(HOME, "home")), what="the reply")
    wait_until(lambda: len([p for p in h.posts(HOME, "home") if parse_served(p) is not None]) == 1,
               what="the receipt")
    time.sleep(0.6)
    assert len(h.contexts) == 2
    h.stop()


def test_a_restart_before_the_reserving_keeps_the_debt_and_serves_it_once(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path, reply=outputs("prose"), reply_retry_seconds=3.0).start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: any("(this run produced no reply" in p for p in h.posts("pj-x", "workplan-a")),
               what="the failure")
    wait_until(lambda: any(e.next_at for e in h.listener.queue.entries()), what="the scheduled re-serving")
    h.stop()
    reply = outputs("<ag-reply>\nhere it is\n</ag-reply>")
    h2 = Harness(realm, tmp_path, reply=reply, reply_retry_seconds=0.2).start()
    # The entry kept its time across the restart; startup recovery found the
    # debt on its own as well, and the two are one serving.
    wait_until(lambda: any("here it is" in r for r in h2.replies("pj-x", "workplan-a")), timeout=15.0,
               what="the reply after the restart")
    time.sleep(0.6)
    assert len(h2.contexts) == 1 and "prose" in reply.prompts[0]
    h2.stop()
