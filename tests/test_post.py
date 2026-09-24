"""What a post is for (`clearer_chat_ui` step 1, `agag.post`).

Pinned: every intent round-trips through the wire line; a post without one
is unclassified and never a request; a malformed line is removed from the
text and read as unclassified with the reason; a line inside a code fence
is text; a run declares the intent on its `ag-reply` fence and the
listener addresses a request to the requester it recorded; the line rides
inside the prepared reply, so a lost send, a crash before or after the send
and a restart deliver it once, with its meaning, without running the model
again; `agentchat send` writes the same line from flags and `read` shows
the meaning instead of the line.
"""

from __future__ import annotations

import io
import time

import pytest

from agag import post, serving, topics
from agag.chat import AgentChatError, build_parser, format_messages, send_meta
from agag.post import PROGRESS, REPORT, RESPONSE_REQUEST, PostMeta, compose, parse_post
from agag.reply import split_reply
from agag.selfnote import is_progress

from test_serving_lifecycle import ACK, DEV, OWNER, Harness, ScriptedClient, human, realm_with_channels, wait_until

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


# --- the wire line --------------------------------------------------------------------


@pytest.mark.parametrize("meta", [
    PostMeta(intent=PROGRESS),
    PostMeta(intent=REPORT),
    PostMeta(intent=REPORT, re=(9001,)),
    PostMeta(intent=RESPONSE_REQUEST, to=8),
    PostMeta(intent=RESPONSE_REQUEST, to=8, ask="question"),
    PostMeta(intent=RESPONSE_REQUEST, to=8, ask="confirmation", re=(9001, 9005)),
    PostMeta(re=(9001,)),
    PostMeta(intent=RESPONSE_REQUEST, to=8, seen=9000),
])
def test_every_intent_round_trips_through_one_message(meta):
    content = compose("Some words.\n\nMore words.", meta)
    assert content.startswith("Some words.\n\nMore words.\n\n`ag-post ")
    parsed = parse_post(content)
    assert parsed.error is None and parsed.meta == meta
    assert parsed.text == "Some words.\n\nMore words.", "the person reads the words, not the line"


def test_the_line_is_exactly_the_documented_shape():
    assert compose("Which one?", PostMeta(intent=RESPONSE_REQUEST, to=8, ask="question")) == (
        "Which one?\n\n`ag-post intent=response_request to=8 ask=question`")


def test_a_post_without_a_line_is_unclassified_and_never_a_request():
    parsed = parse_post("@**Developer**\n\nShall I go ahead?")
    assert parsed.meta is None and parsed.error is None and parsed.intent is None
    assert parsed.text == "@**Developer**\n\nShall I go ahead?"
    assert compose("words", None) == "words" and compose("words", PostMeta()) == "words"


@pytest.mark.parametrize("line,why", [
    ("`ag-post intent=please`", "unknown intent"),
    ("`ag-post intent=response_request`", "needs to="),
    ("`ag-post intent=report to=8`", "only for a response_request"),
    ("`ag-post intent=report ask=question`", "only for a response_request"),
    ("`ag-post intent=response_request to=8 ask=maybe`", "unknown ask"),
    ("`ag-post intent=report colour=blue`", "unknown key"),
    ("`ag-post intent=report intent=progress`", "given twice"),
    ("`ag-post to=Developer intent=response_request`", "not a user id"),
    ("`ag-post re=abc`", "not a list of message ids"),
    ("`ag-post progress`", "not key=value"),
    ("`ag-post`", "says nothing"),
])
def test_a_malformed_line_is_removed_and_read_as_unclassified_with_the_reason(line, why):
    parsed = parse_post(f"The words.\n\n{line}")
    assert parsed.meta is None and why in parsed.error
    assert parsed.text == "The words.", "a machine line is never shown as words, even a broken one"


def test_compose_refuses_what_it_could_not_read_back():
    with pytest.raises(ValueError):
        compose("x", PostMeta(intent=RESPONSE_REQUEST))
    with pytest.raises(ValueError):
        compose("x", PostMeta(intent="shout"))


def test_compose_replaces_a_line_rather_than_stacking_two():
    once = compose("words", PostMeta(intent=PROGRESS))
    twice = compose(once, PostMeta(intent=REPORT))
    assert twice == "words\n\n`ag-post intent=report`"


def test_only_the_last_line_counts():
    content = "`ag-post intent=response_request to=8`\n\nthat line above is only quoted"
    parsed = parse_post(content)
    assert parsed.meta is None and parsed.text == content


# --- fences ----------------------------------------------------------------------------


def test_a_line_inside_an_unclosed_code_fence_is_text():
    content = "Here is the format:\n\n```\n`ag-post intent=response_request to=8`"
    parsed = parse_post(content)
    assert parsed.meta is None and parsed.error is None and parsed.text == content


def test_a_line_after_a_closed_code_fence_is_the_post_line():
    content = "Here is the format:\n\n```text\n`ag-post intent=progress`\n```\n\n`ag-post intent=report`"
    parsed = parse_post(content)
    assert parsed.meta == PostMeta(intent=REPORT)
    assert parsed.text.endswith("```text\n`ag-post intent=progress`\n```")


def test_nested_fences_are_followed_to_their_real_close():
    # A four-backtick block containing a three-backtick one: the inner bare
    # fence does not close the outer, so the line is still inside it.
    inside = "````markdown\n```\n`ag-post intent=report`\n```\n`ag-post intent=progress`"
    assert parse_post(inside).meta is None
    outside = inside + "\n````\n\n`ag-post intent=progress`"
    assert parse_post(outside).meta == PostMeta(intent=PROGRESS)


# --- the reply fence -------------------------------------------------------------------


def test_a_run_declares_the_intent_on_its_reply_fence():
    split = split_reply("thinking\n\n```ag-reply intent=response_request ask=question\nWhich one?\n```")
    assert split.ok and split.reply == "Which one?"
    assert split.meta == PostMeta(intent=RESPONSE_REQUEST, to=None, ask="question")


def test_a_reply_without_an_intent_is_unclassified():
    split = split_reply("```ag-reply\nDone.\n```")
    assert split.ok and split.meta is None and split.meta_error is None


def test_a_misspelt_reply_intent_keeps_the_reply_and_says_why():
    split = split_reply("```ag-reply intent=answer\nDone.\n```")
    assert split.ok and split.reply == "Done."
    assert split.meta is None and "unknown intent" in split.meta_error


def test_several_blocks_are_the_strongest_thing_any_of_them_is():
    output = ("```ag-reply intent=report\nThe build passed.\n```\n\n"
              "```ag-reply intent=response_request ask=confirmation\nDeploy it?\n```\n\n"
              "```ag-reply intent=progress\n(still watching the logs)\n```")
    split = split_reply(output)
    assert split.meta == PostMeta(intent=RESPONSE_REQUEST, ask="confirmation")


def test_attributes_on_a_four_backtick_reply_with_a_nested_fence():
    said = "Run this:\n\n```bash\nmake test\n```\n\nand tell me the result."
    split = split_reply(f"````ag-reply intent=response_request\n{said}\n````")
    assert split.ok and split.reply == said and split.meta.intent == RESPONSE_REQUEST


# --- serve_topic -------------------------------------------------------------------------


def run_output(output):
    return lambda ctx: topics.TopicResult(output=output)


def test_a_request_is_addressed_to_the_requester_the_serving_recorded():
    client = ScriptedClient([human("build it", 1)])
    record = topics.serve_topic(client, "c", "t", run_output("```ag-reply intent=response_request\nWhich branch?\n```"),
                                ack_text="ack", journal=serving.NullJournal(1), log=lambda t: None)
    assert record.reply_text == "@**Developer**\n\nWhich branch?\n\n`ag-post intent=response_request to=7 seen=501`"
    assert record.extra["intent"] == {"intent": RESPONSE_REQUEST, "to": DEV, "seen": 501}, \
        "seen= is the processed input boundary (the ack is the newest post the serving read)"


def test_a_report_carries_its_line_and_the_mention_stays_first():
    client = ScriptedClient([human("build it", 1)])
    record = topics.serve_topic(client, "c", "t", run_output("```ag-reply intent=report\nBuilt.\n```"),
                                ack_text="ack", journal=serving.NullJournal(1), log=lambda t: None)
    assert record.reply_text == "@**Developer**\n\nBuilt.\n\n`ag-post intent=report`"
    assert client.sent == ["ack", record.reply_text], "one delivery holds the words and the meaning"


def test_an_unmarked_intent_is_posted_unclassified_and_logged():
    client = ScriptedClient([human("build it", 1)])
    log = []
    record = topics.serve_topic(client, "c", "t", run_output("```ag-reply intent=asking\nBuilt?\n```"),
                                ack_text="ack", journal=serving.NullJournal(1), log=log.append)
    assert record.reply_text == "@**Developer**\n\nBuilt?"
    assert any("reply intent unusable" in line for line in log)


def test_a_handler_failure_and_a_missing_reply_are_reports_not_requests():
    client = ScriptedClient([human("build it", 1)])

    def broken(ctx):
        raise RuntimeError("disk full")

    record = topics.serve_topic(client, "c", "t", broken, ack_text="ack", journal=serving.NullJournal(1),
                                log=lambda t: None)
    assert parse_post(record.reply_text).meta == PostMeta(intent=REPORT)
    client = ScriptedClient([human("build it", 1)])
    record = topics.serve_topic(client, "c", "t", run_output("I forgot the mark"), ack_text="ack",
                                journal=serving.NullJournal(1), log=lambda t: None)
    parsed = parse_post(record.reply_text)
    assert parsed.meta == PostMeta(intent=REPORT) and "produced no reply" in parsed.text


def test_a_handler_may_classify_its_literal_sections():
    client = ScriptedClient([human("make an apple", 1)])
    record = topics.serve_topic(client, "c", "t",
                                lambda ctx: topics.TopicResult(["Delivered: apple.png"], meta=PostMeta(intent=REPORT)),
                                ack_text="ack", journal=serving.NullJournal(1), log=lambda t: None)
    assert record.reply_text.endswith("Delivered: apple.png\n\n`ag-post intent=report`")


def test_the_repair_run_is_asked_to_keep_the_intent():
    client = ScriptedClient([human("build it", 1)])
    prompts = []

    def repair(reason):
        prompts.append(reason)
        return "```ag-reply intent=response_request ask=question\nWhich branch?\n```"

    record = topics.serve_topic(client, "c", "t",
                                lambda ctx: topics.TopicResult(output="no mark here", repair=repair),
                                ack_text="ack", journal=serving.NullJournal(1), log=lambda t: None)
    assert parse_post(record.reply_text).meta == PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask="question", seen=501)
    from agag.reply import repair_prompt
    assert "intent=" in repair_prompt("x", "why")


# --- delivery: retries, crashes, restarts ------------------------------------------------

ASKING = "```ag-reply intent=response_request ask=question\nWhich branch?\n```"
EXPECTED_WORDS = "@**Dev**\n\nWhich branch?"


def is_expected(content):
    parsed = parse_post(content)
    return (parsed.text == EXPECTED_WORDS and parsed.meta is not None and parsed.meta.intent == RESPONSE_REQUEST
            and parsed.meta.to == DEV and parsed.meta.ask == "question" and parsed.meta.seen)


def test_a_dropped_send_is_redelivered_with_its_meaning(tmp_path):
    attempts = []
    client = ScriptedClient([human("build it", 1)],
                            trouble=lambda c: (attempts.append(1) or "error") if "ag-post" in c else None)
    journal = serving.NullJournal()
    with pytest.raises(Exception):
        topics.serve_topic(client, "c", "t", run_output(ASKING), ack_text="ack", journal=journal,
                           log=lambda t: None, delivery={"sleep": lambda s: None})
    prepared = journal.serving().reply_text
    assert parse_post(prepared).meta.intent == RESPONSE_REQUEST, "the meaning is in the prepared text"
    client.trouble = lambda c: None
    topics.resume_prepared(client, journal.serving(), journal, log=lambda t: None)
    assert client.sent[-1] == prepared and client.sent.count(prepared) == 1


def test_a_lost_answer_is_found_on_read_back_with_the_meaning_and_not_repeated():
    client = ScriptedClient([human("build it", 1)], trouble=lambda c: "lost" if "ag-post" in c else None)
    record = topics.serve_topic(client, "c", "t", run_output(ASKING), ack_text="ack",
                                journal=serving.NullJournal(), log=lambda t: None, delivery={"sleep": lambda s: None})
    posted = [c for c in client.sent if "ag-post" in c]
    assert len(posted) == 1 and record.state == serving.DELIVERED
    assert parse_post(posted[0]).meta.intent == RESPONSE_REQUEST


def test_a_crash_before_the_send_redelivers_the_request_after_a_restart_without_rerunning(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path, reply=lambda ctx: topics.TopicResult(output=ASKING))
    h.client.trouble = lambda content: "crash" if "ag-post" in content else None
    h.start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: not h.thread.is_alive() or (h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER))
               and h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).state == serving.PREPARED),
               what="the prepared record")
    h.crash()
    prepared = h.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).reply_text
    assert is_expected(prepared)
    h2 = Harness(realm, tmp_path, reply=lambda ctx: topics.TopicResult(output="```ag-reply\nWRONG\n```")).start()
    wait_until(lambda: h.replies("pj-x", "workplan-a") == [prepared], what="the redelivery")
    time.sleep(0.3)
    assert h2.contexts == [], "the model did not run again"
    assert h.replies("pj-x", "workplan-a") == [prepared], "one post, with its meaning"
    h2.stop()


def test_a_crash_after_the_send_is_settled_by_read_back_with_the_meaning(tmp_path):
    realm = realm_with_channels()
    h = Harness(realm, tmp_path, reply=lambda ctx: topics.TopicResult(output=ASKING))
    h.client.trouble = lambda content: "crash_after" if "ag-post" in content else None
    h.start()
    realm.post("pj-x", "workplan-a", "please", sender_id=DEV, sender_name="Dev")
    wait_until(lambda: len(h.replies("pj-x", "workplan-a")) == 1, what="the send")
    assert is_expected(h.replies("pj-x", "workplan-a")[0])
    h.crash()
    h2 = Harness(realm, tmp_path).start()
    wait_until(lambda: h2.listener.queue.latest_serving(("pj-x", "workplan-a", OWNER)).state == serving.DELIVERED,
               what="the read-back")
    time.sleep(0.3)
    replies = h.replies("pj-x", "workplan-a")
    assert len(replies) == 1 and is_expected(replies[0]) and h2.contexts == []
    h2.stop()


# --- agents reading the conversation -----------------------------------------------------


def test_the_chatlog_says_what_a_post_is_and_hides_the_line():
    history = [
        human("build it", 1, name="Dev"),
        {"id": 2, "sender_id": 42, "sender_full_name": "Bot", "content": compose("Working.", PostMeta(intent=PROGRESS))},
        {"id": 3, "sender_id": 42, "sender_full_name": "Bot",
         "content": compose("@**Dev**\n\nWhich branch?", PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask="question"))},
        {"id": 4, "sender_id": DEV, "sender_full_name": "Dev", "content": compose("main", PostMeta(re=(3,)))},
    ]
    log = topics.format_chatlog(history, 42)
    assert "ag-post" not in log
    assert "[Bot (you)] (progress) Working." in log
    assert "[Bot (you)] (asks Dev to answer (question); request #3) @**Dev**" in log
    assert "[Dev] (answers #3) main" in log
    printed = format_messages([{**m, "timestamp": 0} for m in history])
    assert "ag-post" not in printed and "(message 3, asks Dev to answer (question))" in printed


def test_a_progress_intent_is_progress_and_a_report_is_not():
    assert is_progress(compose("Rendering scene 2.", PostMeta(intent=PROGRESS)))
    assert is_progress("🔧 Bash: make test")
    assert not is_progress(compose("🔧 all done", PostMeta(intent=REPORT)))


# --- agentchat send ----------------------------------------------------------------------


class Users:
    def users(self):
        return [{"user_id": 8, "full_name": "Developer", "is_active": True},
                {"user_id": 15, "full_name": "Front", "is_active": True}]


def parsed_send(*argv):
    return build_parser().parse_args(["send", "ch", "tp", *argv])


def test_send_flags_become_the_line():
    meta = send_meta(Users(), parsed_send("--intent", "response_request", "--to", "Developer",
                                          "--ask", "confirmation", "--re", "12", "ok?"))
    assert meta == PostMeta(intent=RESPONSE_REQUEST, to=8, ask="confirmation", re=(12,))
    assert send_meta(Users(), parsed_send("--intent", "progress", "half way")) == PostMeta(intent=PROGRESS)
    assert send_meta(Users(), parsed_send("--to", "15", "--intent", "response_request", "x")).to == 15
    assert send_meta(Users(), parsed_send("plain")) is None


@pytest.mark.parametrize("argv,why", [
    (("--intent", "response_request", "x"), "needs --to"),
    (("--to", "Developer", "x"), "give --intent response_request"),
    (("--intent", "report", "--to", "8", "x"), "give --intent response_request"),
    (("--intent", "response_request", "--to", "Nobody", "x"), "names nobody"),
])
def test_send_refuses_a_request_it_cannot_address(argv, why):
    args = parsed_send(*argv)
    with pytest.raises(AgentChatError) as caught:
        send_meta(Users(), args)
    if "give --intent" in why:
        assert "--intent response_request" in str(caught.value) or "only for a response_request" in str(caught.value)
    else:
        assert why in str(caught.value)


def test_send_posts_words_and_meaning_in_one_message(monkeypatch):
    from agag import chat

    sent = []

    class Client(Users):
        def send_to_channel(self, channel, topic, content):
            sent.append(content)
            return 77

    monkeypatch.setattr(chat, "refuse_resolved", lambda *a: None)
    monkeypatch.setattr(chat, "join_and_record", lambda *a: False)
    monkeypatch.setattr(chat, "ensure_rootchat", lambda *a: None)
    out = io.StringIO()
    chat._run(parsed_send("--intent", "response_request", "--to", "Developer", "Which one?"), Client(), out)
    assert sent == ["Which one?\n\n`ag-post intent=response_request to=8`"]
    assert "sent message 77" in out.getvalue()
