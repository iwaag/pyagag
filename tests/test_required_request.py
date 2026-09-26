"""A handler's required request survives the run's words (`clearer_chat_ui` ex1 step 1).

autolab's task serving returns `response_request ask=confirmation` when a run
wrote its report but the requester has not agreed; the run's own reply said
`intent=report`, and `split.meta or meta` let the report win — the text said
agreement was needed and no request was listed. Pinned here: `combine`'s
precedence, and the real `serve_topic` path delivering a request that
`read_requests` lists as pending, through progress, a question, conflicting
recipients, reply repair, a failed repair and a failed handler.
"""

from __future__ import annotations

import pytest

from agag import serving, topics
from agag.outstanding import PENDING, read_requests
from agag.post import CONFIRMATION, PROGRESS, QUESTION, REPORT, RESPONSE_REQUEST, PostMeta, combine, parse_post

from test_serving_lifecycle import BOT, DEV, ScriptedClient, human
from endmark import plain

REQ = PostMeta(intent=RESPONSE_REQUEST, ask=CONFIRMATION)
OTHER = 44


# --- combine ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", [PostMeta(intent=REPORT), PostMeta(intent=PROGRESS), None, PostMeta()])
def test_a_required_request_survives_a_weaker_or_missing_intent(declared):
    assert combine(declared, PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION)) == \
        PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION)


def test_the_handlers_recipient_and_kind_win_over_the_runs_own_question():
    declared = PostMeta(intent=RESPONSE_REQUEST, to=OTHER, ask=QUESTION)
    assert combine(declared, PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION)) == \
        PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION)


def test_what_the_handler_left_out_comes_from_the_runs_request():
    assert combine(PostMeta(intent=RESPONSE_REQUEST, to=OTHER, ask=QUESTION), PostMeta(intent=RESPONSE_REQUEST)) == \
        PostMeta(intent=RESPONSE_REQUEST, to=OTHER, ask=QUESTION)
    # A question to somebody else is not the kind of what the handler asks DEV.
    assert combine(PostMeta(intent=RESPONSE_REQUEST, to=OTHER, ask=QUESTION),
                   PostMeta(intent=RESPONSE_REQUEST, to=DEV)) == PostMeta(intent=RESPONSE_REQUEST, to=DEV)


def test_references_are_the_union_and_seen_is_never_carried():
    combined = combine(PostMeta(intent=REPORT, re=(5,)), PostMeta(intent=RESPONSE_REQUEST, to=DEV, re=(6, 5), seen=9))
    assert combined == PostMeta(intent=RESPONSE_REQUEST, to=DEV, re=(5, 6))


@pytest.mark.parametrize("declared", [PostMeta(intent=PROGRESS), PostMeta(intent=REPORT),
                                      PostMeta(intent=RESPONSE_REQUEST, ask=QUESTION)])
def test_without_a_requirement_the_run_keeps_its_declared_intent(declared):
    assert combine(declared, PostMeta(intent=REPORT)) == declared
    assert combine(declared, None) == declared


def test_a_handler_default_classifies_words_that_declared_nothing():
    assert combine(None, PostMeta(intent=REPORT)) == PostMeta(intent=REPORT)
    assert combine(PostMeta(re=(7,)), PostMeta(intent=REPORT)) == PostMeta(intent=REPORT, re=(7,))
    assert combine(None, None) is None


# --- the real serving path -------------------------------------------------------------


def serve(output, meta=None, *, repair=None, history=None, handler=None):
    client = ScriptedClient(history or [human("build it", 1)])
    log = []
    handler = handler or (lambda ctx: topics.TopicResult(["task 2 is not closed: it closes when its requester "
                                                          "agrees here"], output=output, repair=repair, meta=meta))
    record = topics.serve_topic(client, "c", "t", handler, ack_text="ack", journal=serving.NullJournal(1),
                                log=log.append)
    return client, record, log


def test_a_report_from_the_run_still_asks_for_the_required_confirmation():
    client, record, log = serve("```ag-reply intent=report\nAll tests pass.\n```", REQ)
    parsed = parse_post(record.reply_text)
    assert plain(parsed.meta) == PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION, seen=501)
    assert "All tests pass." in parsed.text and "not closed" in parsed.text
    assert record.extra["intent"]["ask"] == CONFIRMATION
    assert any("stands over the reply's intent=report" in line for line in log)
    outstanding = read_requests(client.history, is_ack=lambda c: c == "ack")
    assert [(r.state, r.to, r.ask) for r in outstanding.requests] == [(PENDING, DEV, CONFIRMATION)]


def test_progress_and_a_question_from_the_run_become_the_required_confirmation():
    for fence in ("intent=progress", "intent=response_request ask=question"):
        _, record, _ = serve(f"```ag-reply {fence}\nStill going?\n```", REQ)
        assert plain(parse_post(record.reply_text).meta) == PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION,
                                                              seen=501)


def test_conflicting_recipients_keep_the_handlers():
    _, record, _ = serve(f"```ag-reply intent=response_request to={OTHER} ask=question\nWhich one?\n```",
                         PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION))
    assert parse_post(record.reply_text).meta.to == DEV


def test_a_repaired_report_and_a_failed_repair_keep_the_requirement():
    _, record, _ = serve("no mark", REQ, repair=lambda why: "```ag-reply intent=report\nDone.\n```")
    assert parse_post(record.reply_text).meta.ask == CONFIRMATION
    _, record, _ = serve("no mark", REQ, repair=lambda why: "still no mark")
    parsed = parse_post(record.reply_text)
    assert "produced no reply" in parsed.text
    assert plain(parsed.meta) == PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=CONFIRMATION, seen=501), \
        "the reply failed; the task still waits for its requester"


def test_a_failed_handler_invents_no_request():
    def broken(ctx):
        raise RuntimeError("disk full")

    _, record, _ = serve(None, handler=broken)
    assert plain(parse_post(record.reply_text).meta) == PostMeta(intent=REPORT)


def test_without_a_requirement_a_run_reports_and_asks_as_declared():
    _, record, _ = serve("```ag-reply intent=report\nDone.\n```", PostMeta(intent=REPORT))
    assert plain(parse_post(record.reply_text).meta) == PostMeta(intent=REPORT)
    _, record, _ = serve("```ag-reply intent=progress\nHalfway.\n```", PostMeta(intent=REPORT))
    assert plain(parse_post(record.reply_text).meta) == PostMeta(intent=PROGRESS)
    _, record, _ = serve("```ag-reply intent=response_request ask=question\nWhich?\n```", None)
    assert plain(parse_post(record.reply_text).meta) == PostMeta(intent=RESPONSE_REQUEST, to=DEV, ask=QUESTION, seen=501)


def test_a_requirement_nobody_can_be_asked_leaves_the_runs_report():
    # Only the bot itself has spoken: there is no requester to address.
    client = ScriptedClient([{"id": 1, "sender_id": BOT, "sender_full_name": "Bot", "content": "hello"}])
    log = []
    record = topics.serve_topic(
        client, "c", "t",
        lambda ctx: topics.TopicResult(["waiting"], output="```ag-reply intent=report\nDone.\n```", meta=REQ),
        ack_text="ack", journal=serving.NullJournal(1), log=log.append)
    assert plain(parse_post(record.reply_text).meta) == PostMeta(intent=REPORT)
    assert any("no requester to ask; posted as report" in line for line in log)
