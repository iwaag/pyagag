"""A reply that claims an act it never did (failsafe p7, `agag.claims`).

The incident: Front's run-0183 answered "I release the hold; stop following
this request" with "Released hold #15837 … Recorded the disposition as
`withdrawn`" in one turn and no tool call, and nothing was recorded. These
tests pin the check that notices it — the records a serving wrote are its
window on the mirror, a reader lists what the reply claims, code judges —
and what a mismatch does: one repair serving triggered by the notice, then
recorded, corrected or escalated. The reader is a stub here; its measured
behaviour on the real model is step 2's report.
"""

from __future__ import annotations

import time

import pytest

from agag import claims as C
from agag import topics
from agag.listen import OWNER
from agag.selfnote import note, parse_start
from test_serving_lifecycle import ACK, BOT, DEV, HOME, Harness, realm_with_channels, wait_until

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")

OMNI = 9
FALSE_REPLY = ("Recorded, on your post #15842 (full developer authority):\n\n"
               "- Released hold #15837 (the hold on how pj-protoprey v0.2 proceeds) via `agentchat hold --release`.\n"
               "- Recorded the disposition of this request as `withdrawn` via `agentchat disposition`.\n\n"
               "This request is closed; I won't follow it further.")


class StubReader:
    """The reader's answer per reply text: `answers(words) -> [Claim]`, or
    an exception to raise."""

    def __init__(self, answers):
        self.answers = answers
        self.read_texts: list[str] = []

    def read(self, text):
        self.read_texts.append(text)
        found = self.answers(text)
        if isinstance(found, Exception):
            raise found
        return found


def incident_reader(words):
    found = []
    if "Released hold #" in words:
        found.append(C.Claim("release", 15837, "", "Released hold #15837"))
    if "disposition" in words and "withdrawn" in words and "Recorded" in words:
        found.append(C.Claim("disposition", 0, "", "Recorded the disposition … withdrawn"))
    if "I asked autolab" in words:
        found.append(C.Claim("send", 0, "pj-x", "I asked autolab"))
    return found


# --- the rules, without a listener ----------------------------------------------------------


def test_the_reader_answer_is_parsed_with_fences_and_unknown_acts_dropped():
    found = C.parse_reader_output('```json\n{"acts": [{"act": "release", "target": "#15837", "quote": "q"},'
                                  ' {"act": "dance", "target": 1}]}\n```')
    assert [(c.act, c.target) for c in found] == [("release", 15837)]
    with pytest.raises(C.ReaderError):
        C.parse_reader_output("I think the reply claims a release")
    with pytest.raises(C.ReaderError):
        C.parse_reader_output('{"claims": []}')


def test_the_reader_is_given_the_words_without_the_mention_and_the_post_line():
    text = "@**Omni Agent**\n\n@Omni Agent\n\nReleased it.\n\n`ag-post intent=report re=15847 end=15848`"
    assert C.reply_words(text) == "@Omni Agent\n\nReleased it."


def _message(ident, content, *, channel="front", topic="front-desk-x", sender=15):
    return {"id": ident, "display_recipient": channel, "subject": topic, "sender_id": sender, "content": content,
            "timestamp": 0}


def test_records_of_reads_each_act_and_nothing_else():
    home = ("front", "front-desk-x")
    kinds = lambda m: [r.act for r in C.records_of(m, home=home, is_ack=lambda c: c == ACK)]
    assert kinds(_message(1, "[selfnote][hold] decision a15835 by 9 (Omni Agent) #15835 — why")) == ["hold"]
    assert kinds(_message(2, "[selfnote][hold-release] #15837 by 9 (Omni Agent) #15842")) == ["release"]
    assert kinds(_message(3, "[selfnote][disposition] withdrawn a15835 upto=#15847 by 9 #15842")) == ["disposition"]
    assert kinds(_message(4, "[selfnote][state] accepted")) == ["accept"]
    assert kinds(_message(5, "[selfnote][state] started")) == []
    assert kinds(_message(6, "[selfnote][rootchat] front/front-desk-x")) == []
    assert kinds(_message(7, "[selfnote][rootchat] front/front-desk-x #1 rel=work", channel="pj-x",
                          topic="workplan-a")) == ["relation"]
    assert kinds(_message(8, "[selfnote][continuation] {}")) == []
    assert kinds(_message(9, "please plan it", channel="pj-x", topic="workplan-a")) == ["send"]
    assert kinds(_message(10, "the answer", channel="front", topic="✔ front-desk-x")) == [], "home is no send"
    assert kinds(_message(11, "```ag-memo {}```", channel="memo", topic="front-desk-x-s1")) == [], "a memo is nothing"
    assert kinds(_message(12, ACK, channel="pj-x", topic="workplan-a")) == []
    hold = C.records_of(_message(1, "[selfnote][hold] decision a15835 by 9 (Omni Agent) #15835"), home=home)[0]
    assert {15835, 1} <= hold.ids


def test_judge_takes_the_window_first_and_never_fabricates():
    window = [C.Record("hold", 20, "front", "d", frozenset({20, 15835}))]
    missing, found = C.judge([C.Claim("hold", 15835), C.Claim("release", 15837)], window)
    assert [f["act"] for f in found] == ["hold"] and found[0]["how"] == "window"
    assert [m["act"] for m in missing] == ["release"]


# --- the listener -------------------------------------------------------------------------


DESK = "front-desk-trial"


def said(text):
    """A run's output: the words inside the reply mark, as a model writes them."""
    if not isinstance(text, str):
        return text
    return topics.TopicResult(output=f"notes to myself\n<ag-reply intent=report>\n{text}\n</ag-reply>")


def harness(tmp_path, realm, reply, reader, **kwargs):
    h = Harness(realm, tmp_path, reply=lambda ctx: said(reply(ctx)), **kwargs)
    h.listener.claims = C.ClaimCheck(StubReader(reader) if not isinstance(reader, StubReader) else reader,
                                     mirror_wait=3.0, retry_seconds=0.2, attempts=3)
    return h.start()


def notes(h, tag):
    from agag.selfnote import parse_note

    return [(m["id"], parse_note(m["content"], tag)) for m in sorted(h.realm.messages.values(), key=lambda m: m["id"])
            if m["sender_id"] == BOT and parse_note(m["content"], tag) is not None]


def claim_notes(h):
    return [(i, C.parse_claim(note(C.CLAIM_TAG, v))) for i, v in notes(h, C.CLAIM_TAG)]


def post_hold(realm):
    realm.post(HOME, DESK, "keep the v0.2 decision on hold", sender_id=OMNI, sender_name="Omni Agent")


def test_run_0183_is_detected_and_the_notice_is_what_serves_the_agent_again(tmp_path):
    """The deterministic reproduction: a reply with run-0183's words and no
    tool call. The mismatch is recorded, the owner's own start note (its
    `because` the claim) triggers one repair serving, its prompt carries
    the notice, and the records it writes settle the claim."""
    realm = realm_with_channels()
    prompts: list[str] = []
    state = {"n": 0}

    def reply(ctx):
        state["n"] += 1
        prompts.append(topics.prompt_with_guide([], "guide", reply=True))
        if state["n"] == 1:
            return FALSE_REPLY  # one turn, nothing recorded
        # The repair: the records, then a reply that says so.
        h.client.send_to_channel(HOME, DESK, "[selfnote][hold-release] #15837 by 9 (Omni Agent) #15842 — trial ends")
        h.client.send_to_channel(HOME, DESK, "[selfnote][disposition] withdrawn a15835 upto=#1 by 9 (Omni Agent) #15842")
        return ("My previous reply said the records existed; they did not. Now: released hold #15837 and recorded "
                "the disposition withdrawn (#records above).")

    h = harness(tmp_path, realm, reply, incident_reader)
    post_hold(realm)
    wait_until(lambda: claim_notes(h), what="the claim note")
    (claim_id, claim), = claim_notes(h)
    assert claim["attempt"] == 1 and [m["act"] for m in claim["missing"]] == ["release", "disposition"]
    assert claim["requester"] == OMNI
    starts = [(i, parse_start(note("start", v))) for i, v in notes(h, "start")]
    assert starts and starts[0][1][0] == claim_id, "the start's `because` is the claim, not the decision"
    wait_until(lambda: notes(h, C.SETTLED_TAG), what="the settlement")
    assert state["n"] == 2, "one repair serving"
    record = h.listener.queue.latest_serving((HOME, DESK, OWNER))
    assert record.trigger_id == starts[0][0], "the repair serving's trigger is the notice"
    assert "Released hold #15837" in prompts[1] and "agentchat release" in prompts[1]
    assert "not on record" not in prompts[0]
    (_, settled), = notes(h, C.SETTLED_TAG)
    assert settled.startswith(f"#{claim_id} recorded")
    assert record.extra["claims"]["state"] == "clean"
    replies = h.replies(HOME, DESK)
    assert len(replies) == 2 and replies[1].startswith("@**Omni Agent**"), "the repair answers the person misled"
    time.sleep(0.4)
    assert state["n"] == 2, "nothing more is served"
    h.stop()


def test_a_repair_that_says_it_again_is_escalated_and_buys_no_third_serving(tmp_path):
    realm = realm_with_channels()
    state = {"n": 0}

    def reply(ctx):
        state["n"] += 1
        return FALSE_REPLY

    h = harness(tmp_path, realm, reply, incident_reader)
    post_hold(realm)
    wait_until(lambda: len(claim_notes(h)) == 2, what="the second claim")
    first, second = [c for _, c in claim_notes(h)]
    assert (first["attempt"], second["attempt"]) == (1, 2) and second["of"] == claim_notes(h)[0][0]
    time.sleep(0.5)
    assert state["n"] == 2 and len(notes(h, "start")) == 1, "one mismatch, one serving"
    rows = C.claims_of(h.realm.messages[i] for i in sorted(h.realm.messages))
    assert [r["state"] for r in rows] == ["superseded", "escalated"]
    h.stop()


def test_a_repair_that_corrects_the_reply_settles_the_claim_as_corrected(tmp_path):
    realm = realm_with_channels()
    state = {"n": 0}

    def reply(ctx):
        state["n"] += 1
        return FALSE_REPLY if state["n"] == 1 else "My previous reply was wrong: nothing is recorded; hold #15837 is still in force."

    h = harness(tmp_path, realm, reply, incident_reader)
    post_hold(realm)
    wait_until(lambda: notes(h, C.SETTLED_TAG), what="the settlement")
    (claim_id, _), = claim_notes(h)
    assert notes(h, C.SETTLED_TAG)[0][1].startswith(f"#{claim_id} corrected")
    h.stop()


def test_a_reply_whose_records_are_in_its_window_is_clean_and_writes_nothing(tmp_path):
    """run-0184's shape, and the proxy case: the release recorded on the
    Omni Agent's words is Front's own post, `by 9 … for 8`."""
    realm = realm_with_channels()

    def reply(ctx):
        h.client.send_to_channel(HOME, DESK, "[selfnote][hold-release] #15837 by 9 (Omni Agent) for 8 (Developer) #15842")
        h.client.send_to_channel(HOME, DESK, "[selfnote][disposition] withdrawn a15835 upto=#1 by 9 (Omni Agent) #15842")
        return FALSE_REPLY

    h = harness(tmp_path, realm, reply, incident_reader)
    post_hold(realm)
    wait_until(lambda: (h.listener.queue.latest_serving((HOME, DESK, OWNER)) or None) is not None and
               (h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra.get("claims") or {}).get("state"),
               what="the check")
    outcome = h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra["claims"]
    assert outcome["state"] == "clean" and [f["how"] for f in outcome["found"]] == ["window", "window"]
    assert claim_notes(h) == [] and notes(h, "start") == []
    h.stop()


def test_a_restatement_of_an_earlier_record_and_a_quoted_command_raise_nothing(tmp_path):
    realm = realm_with_channels()
    realm.post(HOME, DESK, "[selfnote][hold-release] #15837 by 9 (Omni Agent) #15842", sender_id=BOT,
               sender_name="Mirror Bot")
    said = {"n": 0}

    def reply(ctx):
        said["n"] += 1
        return ("Released hold #15837 earlier (#15849). To close the request the command would be "
                "`agentchat disposition 15835 withdrawn` — I have not run it.")

    def reader(words):
        # An over-reading reader: it lists the restated release as done now.
        return [C.Claim("release", 15837, "", "Released hold #15837 earlier")]

    h = harness(tmp_path, realm, reply, reader)
    post_hold(realm)
    wait_until(lambda: (h.listener.queue.latest_serving((HOME, DESK, OWNER)) is not None and
                        (h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra.get("claims") or {}).get("state")),
               what="the check")
    outcome = h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra["claims"]
    assert outcome["state"] == "clean" and outcome["found"][0]["how"] == "on record"
    assert claim_notes(h) == []
    h.stop()


def test_a_send_claim_is_true_only_when_the_serving_posted_elsewhere(tmp_path):
    realm = realm_with_channels()
    state = {"n": 0}

    def reply(ctx):
        state["n"] += 1
        if state["n"] == 1:
            h.client.send_to_channel("pj-x", "workplan-a", "please plan a")
        return "I asked autolab in pj-x › workplan-a; the answer comes back here."

    h = harness(tmp_path, realm, reply, incident_reader)
    post_hold(realm)
    wait_until(lambda: (h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra.get("claims") or {}).get("state")
               if h.listener.queue.latest_serving((HOME, DESK, OWNER)) else False, what="the first check")
    assert h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra["claims"]["state"] == "clean"
    h.stop()

    realm2 = realm_with_channels()
    h2 = harness(tmp_path / "second", realm2, lambda ctx: "I asked autolab in pj-x › workplan-a.", incident_reader)
    post_hold(realm2)
    wait_until(lambda: claim_notes(h2), what="the claim")
    assert [m["act"] for m in claim_notes(h2)[0][1]["missing"]] == ["send"]
    h2.stop()


def test_a_reader_that_fails_leaves_the_serving_unchecked_never_clean_and_it_is_tried_again(tmp_path):
    realm = realm_with_channels()
    calls = {"n": 0}

    def reader(words):
        calls["n"] += 1
        if calls["n"] == 1:
            return C.ReaderError("the reader at http://x did not answer")
        return incident_reader(words)

    h = harness(tmp_path, realm, lambda ctx: FALSE_REPLY, reader)
    post_hold(realm)
    wait_until(lambda: claim_notes(h), what="the claim on the retry", timeout=10)
    assert calls["n"] >= 2
    assert any("unchecked" in line for line in h.log)
    h.stop()


def test_no_reader_configured_is_unchecked_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv(C.CONFIG_VARIABLE, str(tmp_path / "absent.toml"))
    check = C.ClaimCheck.from_host()
    assert check.reader is None and "does not exist" in check.problem
    (tmp_path / "claims.toml").write_text('[reader]\nurl = "http://127.0.0.1:11434"\nmodel = "m"\ntimeout = 5\n')
    monkeypatch.setenv(C.CONFIG_VARIABLE, str(tmp_path / "claims.toml"))
    check = C.ClaimCheck.from_host()
    assert isinstance(check.reader, C.OllamaReader) and check.reader.timeout == 5
    realm = realm_with_channels()
    h = Harness(realm, tmp_path / "h", reply=lambda ctx: said(FALSE_REPLY))
    h.listener.claims = C.ClaimCheck(None, "no reader configured", retry_seconds=0.2, attempts=1)
    h.start()
    post_hold(realm)
    wait_until(lambda: (h.listener.queue.latest_serving((HOME, DESK, OWNER)) is not None and
                        (h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra.get("claims") or {}).get("state")),
               what="the outcome")
    outcome = h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra["claims"]
    assert outcome["state"] == "unchecked" and outcome["problem"] == "no reader configured"
    h.stop()


def test_a_claim_the_reader_does_not_extract_is_missed(tmp_path):
    """The known limit, pinned: detection is only as good as the reader's
    reading. A reader that lists nothing leaves a false reply clean."""
    realm = realm_with_channels()
    h = harness(tmp_path, realm, lambda ctx: FALSE_REPLY, lambda words: [])
    post_hold(realm)
    wait_until(lambda: (h.listener.queue.latest_serving((HOME, DESK, OWNER)) is not None and
                        (h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra.get("claims") or {}).get("state")),
               what="the check")
    assert h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra["claims"]["state"] == "clean"
    assert claim_notes(h) == []
    h.stop()


def test_a_failure_line_is_not_read(tmp_path):
    realm = realm_with_channels()
    reader = StubReader(incident_reader)
    h = harness(tmp_path, realm, lambda ctx: topics.TopicResult(output="no mark here"), reader)
    post_hold(realm)
    wait_until(lambda: (h.listener.queue.latest_serving((HOME, DESK, OWNER)) is not None and
                        (h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra.get("claims") or {}).get("state")),
               what="the outcome")
    assert h.listener.queue.latest_serving((HOME, DESK, OWNER)).extra["claims"]["state"] == "skipped"
    assert reader.read_texts == []
    h.stop()


# --- what readers show ------------------------------------------------------------------------


def _conversation(*rows):
    return [{"id": i, "display_recipient": "front", "subject": "front-desk-x", "sender_id": s, "content": c,
             "timestamp": 1000 + i, "sender_full_name": "x"} for i, s, c in rows]


def test_claim_states_follow_their_notes():
    claim = C.claim_note({"reply": 5, "attempt": 1, "missing": [{"act": "release", "target": 7}]})
    again = C.claim_note({"reply": 9, "attempt": 2, "of": 6, "missing": [{"act": "release", "target": 7}]})
    rows = C.claims_of(_conversation((6, 15, claim)))
    assert [r["state"] for r in rows] == ["repairing"]
    rows = C.claims_of(_conversation((6, 15, claim), (8, 15, C.settled_note(6, "recorded", 9))))
    assert [r["state"] for r in rows] == ["recorded"] and C.open_claims(_conversation((6, 15, claim),
                                                                                       (8, 15, C.settled_note(6, "corrected", 9)))) == []
    rows = C.claims_of(_conversation((6, 15, claim), (10, 15, again)))
    assert [r["state"] for r in rows] == ["superseded", "escalated"]
    assert "Released" not in C.notice([]) and C.notice([]) == ""
    text = C.notice([{"reply": 5, "attempt": 1, "missing": [{"act": "release", "target": 7, "quote": "Released #7"}]}])
    assert "\"Released #7\"" in text and "agentchat release" in text and "say plainly" in text


def test_the_trace_shows_an_open_claim_and_escalation_is_a_candidate_for_observer():
    from agag.trace import Node, Trace, next_actions, stall_candidates, trace_lines
    from agag.progress import card

    claim = {"id": 6, "reply": 5, "attempt": 2, "state": "escalated", "at": 1000, "by": 15,
             "missing": [{"act": "release", "target": 15837}]}
    root = Node("front", "front-desk-x", "answered", anchor=1, owner="Front", claims=[claim])
    result = Trace(root, 1, 2000)
    assert any("claim #6: reply #5 says release #15837 — no record" in line for line in trace_lines(result))
    assert any("claim #6" in line for line in next_actions(result))
    found = stall_candidates(result, now=1000 + 61)
    assert [c.kind for c in found] == ["claim"] and found[0].evidence == (6, 5)
    repairing = dict(claim, state="repairing", attempt=1)
    root.claims = [repairing]
    assert stall_candidates(result, now=1000 + 61) == [], "a repair is under way"
    assert [c.kind for c in stall_candidates(result, now=1000 + C.REPAIR_SECONDS)] == ["claim"]
    shown = card(result, now=1100)
    assert shown["state"] == "waiting" and "claim #6" in shown["reason"] and shown["claims"][0]["id"] == 6
