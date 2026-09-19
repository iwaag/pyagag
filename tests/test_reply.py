"""The reply mark (`explicit_reply` p1 step 2, `agag.reply`).

Pinned: preamble plus reply posts only the reply; several blocks join in
order; a reply keeps the code fences it contains; missing, empty and
unclosed blocks are failures; machine blocks are the handler's and never
posted; system notices follow the reply; one repair, then a visible failure;
the outcome lands beside the run identity. The #7222, #7230 and #7262 posts
from the advice are the fixtures — they are the shape as it was observed,
not a model happening to omit its preamble.
"""

from __future__ import annotations

import json
from pathlib import Path

from agag import reply, serving, topics
from agag.reply import REPLY_GUIDE, ReplySplit, failure_line, repair_prompt, resolve_reply, split_reply

FIXTURES = Path(__file__).parent / "fixtures" / "reply"
BOT, DEV = 11, 8


def marked(*parts: str, fence: str = "```") -> str:
    return "\n\n".join(f"{fence}ag-reply\n{part}\n{fence}" for part in parts)


# --- the observed shape ------------------------------------------------------------


def test_7222_as_observed_has_no_mark_and_is_a_failed_reply():
    """The advice's example: two paragraphs of thought, then the reply, in
    one post. Under the contract that output is *not posted whole*: it is
    a missing mark, and the run is asked again."""
    output = (FIXTURES / "7222.md").read_text(encoding="utf-8")
    split = split_reply(output)
    assert not split.marked and not split.ok and "no ag-reply block" in split.error
    assert split.rest == output.strip()


def test_7222_with_the_reply_marked_posts_only_the_reply():
    output = (FIXTURES / "7222.md").read_text(encoding="utf-8")
    thought, _, said = output.partition("Let me post a reply asking for that.\n\n")
    thought += "Let me post a reply asking for that."
    split = split_reply(f"{thought}\n\n```ag-reply\n{said.strip()}\n```\n")
    assert split.ok and split.blocks == 1
    assert split.reply == said.strip()
    assert split.reply.startswith("Is the following a fair statement")
    assert "> **Desire draft:**" in split.reply and split.reply.endswith("as yours.")
    assert split.rest == thought, "the thought is the run's own, kept and not posted"


def test_7230_archsage_shape_marks_the_analysis_and_leaves_the_preface():
    output = (FIXTURES / "7230.md").read_text(encoding="utf-8")
    preface, _, analysis = output.partition("---\n\n")
    split = split_reply(f"{preface.strip()}\n\n```ag-reply\n{analysis.strip()}\n```")
    assert split.ok and split.reply == analysis.strip() and split.rest == preface.strip()
    assert split.reply.startswith("開発者の問い")


def test_7262_the_smoke_argue_is_one_turn_not_two():
    output = (FIXTURES / "7262.md").read_text(encoding="utf-8")
    thought, said = output.strip().split("\n\n", 1)
    split = split_reply(f"{thought}\n\n{marked(said)}")
    assert split.reply == said and split.rest == thought


# --- the splitter -----------------------------------------------------------------


def test_several_blocks_are_one_reply_in_order():
    split = split_reply("plan\n\n" + marked("first", "second") + "\n\nnote\n\n" + marked("third"))
    assert split.ok and split.blocks == 3
    assert split.reply == "first\n\nsecond\n\nthird"
    assert split.rest == "plan\n\nnote"


def test_a_reply_keeps_a_code_fence_it_contains():
    said = "Run this:\n\n```bash\nagentchat read pj-x workplan-a\n```\n\nthen tell me."
    split = split_reply(f"thinking\n\n````ag-reply\n{said}\n````\n")
    assert split.ok and split.reply == said


def test_a_three_backtick_reply_still_keeps_an_inner_fence_with_an_info_string():
    said = "```python\nprint('hi')\n```\ndone"
    split = split_reply(f"```ag-reply\n{said}\n```")
    assert split.ok and split.reply == said


def test_tilde_and_backtick_fences_do_not_close_each_other():
    said = "~~~\nraw\n~~~\nafter"
    split = split_reply(f"```ag-reply\n{said}\n```")
    assert split.ok and split.reply == said


def test_the_info_string_is_matched_whatever_its_case_and_spacing():
    assert split_reply("```  AG-Reply  \nhi\n```").reply == "hi"
    assert split_reply("``` ag-reply extra\nhi\n```").blocks == 0, "an info string with more is not the mark"


def test_missing_empty_and_unclosed_blocks_are_failures():
    assert "no ag-reply block" in split_reply("just prose").error
    assert "empty" in split_reply("```ag-reply\n\n   \n```").error
    unclosed = split_reply("preamble\n```ag-reply\nhalf a reply")
    assert "not closed" in unclosed.error and unclosed.reply == "half a reply" and unclosed.blocks == 1
    assert split_reply("").error and split_reply(None).error


def test_a_machine_block_is_the_handlers_business_and_the_splitter_leaves_it_alone():
    from agag.argue import split_block

    output = "thought\n\n```ag-reply\nrecorded.\n```\n\n```ag-argue\ndesire: 92\n```"
    text, fields, error = split_block(output)
    assert fields == {"desire": "92"} and error is None
    split = split_reply(text)
    assert split.reply == "recorded." and "ag-argue" not in split.reply
    # And inside the mark, it would be posted — which is why the handler
    # strips it from the whole output first.
    inside = split_reply("```ag-reply\nrecorded.\n```ag-argue\ndesire: 92\n```\n```")
    assert inside.ok


# --- the repair ------------------------------------------------------------------


def test_resolve_reply_repairs_once_and_then_fails_visibly():
    asked = []

    def repair(reason):
        asked.append(reason)
        return marked("the reply, at last")

    text, split, repaired = resolve_reply("prose only", repair)
    assert text == "the reply, at last" and repaired and split.ok
    assert asked == ["the output contains no ag-reply block"]

    text, split, repaired = resolve_reply("prose only", lambda reason: "still prose")
    assert text == failure_line("the output contains no ag-reply block") and repaired and not split.ok

    text, split, repaired = resolve_reply("prose only", None)
    assert text.startswith("(this run produced no reply:") and not repaired


def test_the_repair_prompt_carries_the_previous_output_and_forbids_repeating_effects():
    prompt = repair_prompt("I did things\n```ag-argue\ndesire: 1\n```", "no block")
    assert "no block" in prompt and "I did things" in prompt
    assert "already been applied" in prompt and "do not include one again" in prompt
    assert REPLY_GUIDE in prompt
    assert "(the output was empty)" in repair_prompt("", "empty")


def test_a_repair_that_raises_is_the_failure_not_a_crash():
    def broken(reason):
        raise RuntimeError("harness down")

    text, split, repaired = resolve_reply("prose", broken, log=lambda line: None)
    assert text.startswith("(this run produced no reply") and repaired


# --- through the skeleton ---------------------------------------------------------


class Client:
    email = "bot@example.invalid"

    def __init__(self, history):
        self.history = history
        self.sent = []
        self.next_id = 500

    def whoami(self):
        return {"user_id": BOT, "full_name": "Front"}

    def topic_history(self, channel, topic, num_before):
        return list(self.history)

    def send_to_channel(self, channel, topic, content):
        self.next_id += 1
        self.history.append({"id": self.next_id, "sender_id": BOT, "sender_full_name": "Front", "content": content})
        self.sent.append(content)
        return self.next_id

    def resolve_topic(self, message_id, topic):
        pass


def human(content="hello", id=1):
    return {"id": id, "sender_id": DEV, "sender_full_name": "Developer", "content": content}


def serve(handler, client=None, **kwargs):
    client = client or Client([human()])
    kwargs.setdefault("ack_text", "ack")
    kwargs.setdefault("log", lambda t: None)
    kwargs.setdefault("handoff", False)
    record = topics.serve_topic(client, "argue", "argue-x", handler, journal=serving.NullJournal(), **kwargs)
    return client, record


def test_the_post_is_the_reply_then_sections_then_notices():
    output = "I should reflect it back first.\n\n" + marked("Is this fair?")
    client, record = serve(lambda ctx: topics.TopicResult(
        output=output, sections=["```ag-routinerun\n{}\n```"], notices=["— the desire is on record as message 7225."]))
    assert client.sent[-1] == "Is this fair?\n\n```ag-routinerun\n{}\n```\n\n— the desire is on record as message 7225."
    assert record.reply_marked is True and record.reply_blocks == 1 and record.reply_failure == ""


def test_an_unmarked_output_is_repaired_once_and_a_second_failure_is_posted_as_such():
    calls = []
    client, record = serve(lambda ctx: topics.TopicResult(
        output="I will answer now. Yes, that is fair.", repair=lambda why: calls.append(why) or marked("Yes, that is fair.")))
    assert client.sent[-1] == "Yes, that is fair." and calls == ["the output contains no ag-reply block"]
    assert record.reply_marked is True

    client, record = serve(lambda ctx: topics.TopicResult(output="prose", repair=lambda why: "more prose",
                                                          notices=["— note kept"]))
    assert client.sent[-1] == failure_line("the output contains no ag-reply block") + "\n\n— note kept"
    assert record.reply_marked is False and "no ag-reply block" in record.reply_failure
    assert record.state == serving.DELIVERED, "the failure is a delivered answer: the conversation is not left hanging"


def test_the_outcome_is_recorded_beside_the_run_identity(tmp_path):
    record_path = tmp_path / "run-0001.json"
    record_path.write_text(json.dumps({"schema": "ag.agent-run.v1", "role": "argue"}), encoding="utf-8")

    def handler(ctx):
        ctx.journal.record(str(record_path))
        return topics.TopicResult(output=marked("hi"))

    client, record = serve(handler)
    written = json.loads(record_path.read_text(encoding="utf-8"))
    assert written["reply"] == {"marked": True, "blocks": 1, "delivered_id": record.delivered_id,
                                "posted_to": "argue/argue-x"}
    assert record.run_record == str(record_path)


def test_sections_only_results_post_exactly_as_before():
    client, record = serve(lambda ctx: topics.TopicResult(["a deterministic line"]))
    assert client.sent[-1] == "a deterministic line" and record.reply_marked is None


def test_the_reply_guide_is_appended_once_and_only_on_request():
    prompt = topics.prompt_with_guide(["placement"], "the guide", reply=True)
    assert prompt.endswith(REPLY_GUIDE) and prompt.startswith("placement\n\nthe guide")
    assert topics.prompt_with_guide(["placement"], "the guide") == "placement\n\nthe guide"
    assert isinstance(ReplySplit("", "", 0), ReplySplit) and reply.REPLY_LANGUAGE == "ag-reply"
