"""The shared topic-serving skeleton."""

import pytest

from agag import topics
from agag.zulip import ZulipError

BOT_ID = 11
HUMAN_ID = 8
CHANNEL = "pj-demo"
TOPIC = "workplan-one"


def message(sender_id=HUMAN_ID, name="Developer", content="Build it", id=1):
    return {
        "id": id,
        "sender_id": sender_id,
        "sender_full_name": name,
        "content": content,
    }


class Client:
    email = "bot@example.invalid"

    def __init__(self, calls, history=None):
        self.calls = calls
        self.history = [message()] if history is None else history

    def whoami(self):
        return {"user_id": BOT_ID, "full_name": "Autolab"}

    def topic_history(self, channel, topic, num_before):
        self.calls.append(("history", num_before))
        return self.history

    def send_to_channel(self, channel, topic, content):
        self.calls.append(("post", topic, content))
        return 1

    def resolve_topic(self, message_id, topic):
        self.calls.append(("resolve", message_id, topic))


def serve(client, handler, **kwargs):
    kwargs.setdefault("ack_text", "ack")
    kwargs.setdefault("log", lambda text: None)
    topics.serve_topic(client, CHANNEL, TOPIC, handler, **kwargs)


# --- the discipline --------------------------------------------------------


def test_the_ack_precedes_every_step():
    calls = []
    serve(Client(calls), lambda ctx: topics.TopicResult(["done"]))
    # chatlog, then the handoff lookup, then the reply, then the re-check.
    assert [call[0] for call in calls] == [
        "post", "history", "history", "post", "history",
    ]
    assert calls[0][2] == "ack"
    assert calls[3][2] == "@**Developer**\n\ndone"


def test_a_failure_is_reported_with_the_step_the_handler_had_named():
    calls = []

    def handler(ctx):
        ctx.step = "front"
        raise RuntimeError("claude_code timed out")

    serve(Client(calls), handler)
    assert calls[-1][2] == (
        "@**Developer**\n\nfailed during front: claude_code timed out"
    )
    # A failing topic is not retried; a human post re-arms it.
    assert [call[0] for call in calls].count("post") == 2


def test_a_failure_before_the_handler_names_the_chatlog_step():
    calls = []

    class Broken(Client):
        def topic_history(self, channel, topic, num_before):
            raise RuntimeError("zulip is down")

    serve(Broken(calls), lambda ctx: topics.TopicResult(["never"]))
    assert "failed during chatlog: zulip is down" in calls[-1][2]


def test_the_handler_can_post_before_the_final_reply():
    calls = []

    def handler(ctx):
        ctx.post("an interim answer")
        return topics.TopicResult(["the rest"])

    serve(Client(calls), handler)
    posted = [call[2] for call in calls if call[0] == "post"]
    assert posted == ["ack", "an interim answer", "@**Developer**\n\nthe rest"]


def test_no_sections_means_no_final_post():
    """A handler that already said everything it had to say must not post an
    empty message on the way out."""
    calls = []
    serve(Client(calls), lambda ctx: topics.TopicResult([]))
    assert [call[2] for call in calls if call[0] == "post"] == ["ack"]


def test_resolve_after_renames_the_topic_after_the_final_reply():
    calls = []
    serve(Client(calls), lambda ctx: topics.TopicResult(["cancelled"], resolve_after=True))
    assert [call[0] for call in calls][-3:] == ["post", "history", "resolve"]
    assert calls[-1] == ("resolve", 1, TOPIC)


def test_a_human_post_during_the_run_serves_the_topic_again():
    calls = []
    later = message(content="one more thing", id=2)

    class Scripted(Client):
        def __init__(self):
            super().__init__(calls)
            # chatlog, handoff lookup, re-check — twice.
            self.scripts = [[message()], [message()], [message(), later],
                            [message(), later], [message(), later],
                            [message(), later]]

        def topic_history(self, channel, topic, num_before):
            calls.append(("history", num_before))
            return self.scripts.pop(0)

    seen = []
    serve(Scripted(), lambda ctx: seen.append(len(ctx.history)) or topics.TopicResult(["ok"]))
    assert seen == [1, 2]
    assert [call[2] for call in calls if call[0] == "post"].count("ack") == 2


def test_our_own_post_during_the_run_does_not_re_arm_the_topic():
    calls = []
    ours = message(sender_id=BOT_ID, name="Autolab", content="an answer", id=9)
    client = Client(calls, history=[message(), ours])
    serve(client, lambda ctx: topics.TopicResult(["ok"]))
    # chatlog, handoff lookup, re-check: one pass only.
    assert [call[0] for call in calls].count("history") == 3


# --- the empty topic -------------------------------------------------------


def test_an_empty_topic_is_answered_without_running_the_handler():
    """`sweep_topics` skips a topic whose *last* poster is this bot; a topic
    with no messages has no last poster and matches every sweep forever."""
    calls = []
    ran = []
    serve(
        Client(calls, history=[]),
        lambda ctx: ran.append(1) or topics.TopicResult(["never"]),
        empty_reply="nothing here yet",
    )
    assert ran == []
    assert [call[2] for call in calls if call[0] == "post"] == ["ack", "nothing here yet"]


def test_a_topic_holding_only_our_own_posts_counts_as_empty():
    calls = []
    ran = []
    serve(
        Client(calls, history=[message(sender_id=BOT_ID, content="old")]),
        lambda ctx: ran.append(1) or topics.TopicResult([]),
        empty_reply="nothing here yet",
    )
    assert ran == []


def test_without_an_empty_reply_the_handler_still_runs():
    """Opt-in: an agent that wants to act on an empty topic still can."""
    ran = []
    serve(Client([], history=[]), lambda ctx: ran.append(1) or topics.TopicResult([]))
    assert ran == [1]


# --- workspaces ------------------------------------------------------------


def test_topic_workspace_is_two_validated_components(tmp_path):
    assert topics.topic_workspace(tmp_path, CHANNEL, TOPIC) == tmp_path / CHANNEL / TOPIC
    for bad in ("../outside", "a/b", "", "."):
        with pytest.raises(ValueError):
            topics.topic_workspace(tmp_path, bad, TOPIC)
        with pytest.raises(ValueError):
            topics.topic_workspace(tmp_path, CHANNEL, bad)


def test_next_generation_reads_the_directory(tmp_path):
    assert topics.next_generation(tmp_path) == 1
    (tmp_path / "1").mkdir()
    (tmp_path / "4").mkdir()
    (tmp_path / "notes").mkdir()
    (tmp_path / "5").write_text("a file, not a generation")
    assert topics.next_generation(tmp_path) == 5


def test_generation_dir_creates_the_role_directory(tmp_path):
    directory = topics.generation_dir(tmp_path, CHANNEL, TOPIC, 3, "front")
    assert directory == tmp_path / CHANNEL / TOPIC / "3" / "front"
    assert directory.is_dir()


def test_next_record_path_numbers_from_one(tmp_path):
    assert topics.next_record_path(tmp_path).name == "run-0001.json"
    (tmp_path / "run-0001.json").write_text("{}")
    assert topics.next_record_path(tmp_path).name == "run-0002.json"


# --- chatlog and prompts ---------------------------------------------------


def test_chatlog_marks_our_own_lines():
    text = topics.format_chatlog(
        [message(), message(sender_id=BOT_ID, name="Autolab", content="hi")], BOT_ID
    )
    assert text == "[Developer] Build it\n[Autolab (you)] hi\n"


def test_chatlog_drops_only_our_own_noise():
    """A human quoting an ack is conversation, not noise."""
    text = topics.format_chatlog(
        [
            message(content="ack"),
            message(sender_id=BOT_ID, name="Autolab", content="ack"),
            message(sender_id=BOT_ID, name="Autolab", content="a real answer"),
        ],
        BOT_ID,
        drop=lambda content: content == "ack",
    )
    assert text == "[Developer] ack\n[Autolab (you)] a real answer\n"


def test_an_empty_chatlog_is_the_empty_string():
    assert topics.format_chatlog([], BOT_ID) == ""


def test_guide_refuses_a_missing_or_empty_file(tmp_path):
    with pytest.raises(topics.GuideError):
        topics.guide(tmp_path, "absent.md")
    (tmp_path / "empty.md").write_text("   \n")
    with pytest.raises(topics.GuideError):
        topics.guide(tmp_path, "empty.md")
    (tmp_path / "real.md").write_text("  GUIDE  \n")
    assert topics.guide(tmp_path, "real.md") == "GUIDE"


def test_prompt_is_the_placement_lines_then_the_guide():
    prompt = topics.prompt_with_guide(
        [topics.chatlog_placement("Forge"), "Extra line."], "GUIDE TEXT"
    )
    assert prompt == (
        "The chatlog is placed in the working directory. "
        "You are 'Forge' in the chatlog.\nExtra line.\n\nGUIDE TEXT"
    )


# --- the turn, handed over -------------------------------------------------


class Board:
    """A client holding several topics at once, recording where things land."""

    email = "bot@example.invalid"

    def __init__(self, histories):
        self.histories = dict(histories)
        self.posts = []
        self.resolved = []

    def whoami(self):
        return {"user_id": BOT_ID, "full_name": "Autolab"}

    def topic_history(self, channel, topic, num_before):
        return list(self.histories.get((channel, topic), []))

    def send_to_channel(self, channel, topic, content):
        self.posts.append((channel, topic, content))
        return 1

    def resolve_topic(self, message_id, topic):
        self.resolved.append(topic)


def test_the_reply_names_the_last_other_speaker():
    client = Board({(CHANNEL, TOPIC): [message(name="Front")]})
    topics.serve_topic(
        client, CHANNEL, TOPIC, lambda ctx: topics.TopicResult(["planned"]),
        ack_text="ack", log=lambda _: None,
    )
    assert client.posts[-1] == (CHANNEL, TOPIC, "@**Front**\n\nplanned")


def test_a_topic_nobody_else_has_spoken_in_hands_the_turn_to_nobody():
    ours = message(sender_id=BOT_ID, name="Autolab", content="opened")
    client = Board({(CHANNEL, TOPIC): [ours]})
    topics.serve_topic(
        client, CHANNEL, TOPIC, lambda ctx: topics.TopicResult(["still here"]),
        ack_text="ack", log=lambda _: None,
    )
    assert client.posts[-1] == (CHANNEL, TOPIC, "still here")


def test_a_mention_serving_works_at_home_and_answers_where_it_was_asked():
    """The whole point of the second trigger: the task is this agent's own,
    and the answer belongs in the conversation that called it back."""
    remote = ("agforge-agstudio1", "assetplan-enemy-sprite")
    client = Board({
        (CHANNEL, TOPIC): [message(name="Front", content="task 1, go")],
        remote: [message(sender_id=13, name="Forge", content="Work registered")],
    })
    seen = {}

    def handler(ctx):
        seen["served"] = (ctx.channel, ctx.topic)
        seen["replies"] = (ctx.reply_channel, ctx.reply_topic)
        seen["chatlog"] = [m["content"] for m in ctx.history]
        seen["replies_here"] = ctx.replies_here
        return topics.TopicResult(["triggered the run"])

    topics.serve_topic(
        client, CHANNEL, TOPIC, handler,
        ack_text="ack", reply_to=remote, log=lambda _: None,
    )
    assert seen["served"] == (CHANNEL, TOPIC)
    assert seen["replies"] == remote
    assert seen["chatlog"] == ["task 1, go"]
    assert seen["replies_here"] is False
    # Both the ack and the reply went to the remote; home was left alone.
    assert client.posts == [
        (*remote, "ack"),
        (*remote, "@**Forge**\n\ntriggered the run"),
    ]


def test_a_mention_serving_does_not_loop_on_its_own_topic():
    """It never became the served topic's last poster, so what arrived there
    is the owner sweep's business, not this loop's."""
    remote = ("agforge-agstudio1", "assetplan-enemy-sprite")
    client = Board({
        (CHANNEL, TOPIC): [message(name="Front", content="go")],
        remote: [message(sender_id=13, name="Forge")],
    })
    servings = []
    topics.serve_topic(
        client, CHANNEL, TOPIC,
        lambda ctx: servings.append(1) or topics.TopicResult(["done"]),
        ack_text="ack", reply_to=remote, log=lambda _: None,
    )
    assert servings == [1]


def test_a_mention_serving_resolves_the_topic_it_answered_in():
    remote = ("agforge-agstudio1", "assetplan-enemy-sprite")
    client = Board({
        (CHANNEL, TOPIC): [message()],
        remote: [message(sender_id=13, name="Forge")],
    })
    topics.serve_topic(
        client, CHANNEL, TOPIC,
        lambda ctx: topics.TopicResult(["delivered"], resolve_after=True),
        ack_text="ack", reply_to=remote, log=lambda _: None,
    )
    assert client.resolved == [remote[1]]


def test_post_home_reaches_the_served_topic_while_the_reply_goes_elsewhere():
    remote = ("agforge-agstudio1", "assetplan-enemy-sprite")
    client = Board({
        (CHANNEL, TOPIC): [message()],
        remote: [message(sender_id=13, name="Forge")],
    })

    def handler(ctx):
        ctx.post_home("progress: cloning")
        return topics.TopicResult(["done"])

    topics.serve_topic(
        client, CHANNEL, TOPIC, handler,
        ack_text="ack", reply_to=remote, log=lambda _: None,
    )
    assert (CHANNEL, TOPIC, "progress: cloning") in client.posts


# --- the threads a run is party to ----------------------------------------


def test_write_threads_renders_each_conversation_beside_the_chatlog(tmp_path):
    client = Board({
        ("agforge-agstudio1", "assetplan-enemy-sprite"): [
            message(sender_id=13, name="Forge", content="Work registered"),
        ],
        ("work-s2-10", "workrun-task1-s2-10"): [message(name="Front", content="go")],
    })
    written = topics.write_threads(
        client, tmp_path,
        [("agforge-agstudio1", "assetplan-enemy-sprite"),
         ("work-s2-10", "workrun-task1-s2-10")],
        BOT_ID,
    )
    assert [path.relative_to(tmp_path).as_posix() for path in written] == [
        "threads/agforge-agstudio1/assetplan-enemy-sprite.md",
        "threads/work-s2-10/workrun-task1-s2-10.md",
    ]
    assert written[0].read_text(encoding="utf-8") == "[Forge] Work registered\n"


def test_a_thread_that_cannot_be_read_is_skipped_not_fatal(tmp_path):
    class Half(Board):
        def topic_history(self, channel, topic, num_before):
            if topic == "gone":
                raise ZulipError("no such topic")
            return super().topic_history(channel, topic, num_before)

    client = Half({("c", "here"): [message()]})
    logged = []
    written = topics.write_threads(
        client, tmp_path, [("c", "gone"), ("c", "here")], BOT_ID, log=logged.append
    )
    assert [path.name for path in written] == ["here.md"]
    assert logged and "gone" in logged[0]
