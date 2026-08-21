"""`agentchat`: identity comes from the environment, and posting participates.

What is pinned here is the contract an agent's run depends on — which
credentials file speaks, that reading makes exactly the Zulip calls it needs
and never touches subscriptions, that *posting* joins the channel and anchors
the topic with a root note so the answer can find this agent after the run is
over, and that a misconfigured environment fails with a message that names
what was missing rather than a traceback.
"""

import io

import pytest

from agag import chat, selfnote
from agag.zulip import ZulipError

CHANNEL = "agforge-agstudio1"
TOPIC = "assetplan-title-image-1"


class Client:
    """Records every call, and fails loudly on the ones that must not happen."""

    email = "front-bot@example.invalid"

    def __init__(self, calls, messages=None, topics=None):
        self.calls = calls
        self.messages = [] if messages is None else messages
        self.topics = [] if topics is None else topics

    self_id = 15

    def whoami(self):
        self.calls.append(("whoami",))
        return {"user_id": self.self_id, "full_name": "Front"}

    def send_to_channel(self, channel, topic, content):
        self.calls.append(("send", channel, topic, content))
        return 901

    def topic_history(self, channel, topic, num_before=50):
        self.calls.append(("history", channel, topic, num_before))
        return self.messages

    #: When set, only this topic name holds the messages — the others are
    #: empty, which is what a resolved (renamed) topic looks like.
    holder = None

    def _holds(self, topic):
        return self.holder is None or topic == self.holder

    def topic_last_id(self, channel, topic):
        self.calls.append(("last_id", channel, topic))
        if self.holders is not None:
            return self.holders.get(topic, 0)
        if not self._holds(topic) or not self.messages:
            return 0
        return self.messages[-1]["id"]

    def topic_since(self, channel, topic, after_id, num_after=100):
        self.calls.append(("since", channel, topic, after_id))
        if not self._holds(topic):
            return []
        return [m for m in self.messages if m["id"] > after_id]

    def stream_id(self, name):
        self.calls.append(("stream_id", name))
        return 42

    def channel_topics(self, stream_id):
        self.calls.append(("topics", stream_id))
        return self.topics

    #: What `channels` sees. Descriptions matter: they are the half a
    #: reader is after.
    channel_rows = ()

    def channels(self):
        self.calls.append(("channels",))
        return [dict(row) for row in self.channel_rows]

    #: Topic names that hold messages, mapped to their last message id.
    holders = None

    def resolve_topic(self, message_id, topic):
        self.calls.append(("resolve", message_id, topic))

    subscribed = ("some-other-channel",)

    def subscriptions(self):
        self.calls.append(("subscriptions",))
        return [{"name": name} for name in self.subscribed]

    def subscribe_channels(self, names, principals=None):
        self.calls.append(("subscribe", tuple(names)))
        return {"subscribed": list(names)}

    def ensure_subscribed(self, channel):
        for subscription in self.subscriptions():
            if subscription["name"] == channel:
                return False
        self.subscribe_channels([channel])
        return True


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    """One `agentchat` invocation is one process, so its caches start empty."""
    chat._ANCHORED.clear()
    monkeypatch.delenv(selfnote.HOME_VARIABLE, raising=False)


def run(monkeypatch, argv, client):
    monkeypatch.setattr(chat, "client_from_environment", lambda: client)
    out, err = io.StringIO(), io.StringIO()
    code = chat.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def message(sender="Forge", content="on it", timestamp=1755600000, sender_id=13, id=5):
    return {
        "id": id,
        "sender_id": sender_id,
        "sender_full_name": sender,
        "timestamp": timestamp,
        "content": content,
    }


# --- identity --------------------------------------------------------------


def test_client_reads_the_file_the_variable_names(tmp_path, monkeypatch):
    env_file = tmp_path / "zulip.env"
    env_file.write_text(
        "ZULIP_URL=https://zulip.invalid\n"
        "ZULIP_EMAIL=front-bot@example.invalid\n"
        "ZULIP_API_KEY=secret\n",
        encoding="utf-8",
    )
    client = chat.client_from_environment({chat.ENV_VARIABLE: str(env_file)})
    assert client.email == "front-bot@example.invalid"
    assert client.base_url == "https://zulip.invalid"


def test_unset_variable_is_named_in_the_error():
    with pytest.raises(chat.AgentChatError) as error:
        chat.client_from_environment({})
    assert chat.ENV_VARIABLE in str(error.value)


def test_variable_pointing_nowhere_is_named_in_the_error(tmp_path):
    with pytest.raises(chat.AgentChatError) as error:
        chat.client_from_environment({chat.ENV_VARIABLE: str(tmp_path / "absent.env")})
    assert "absent.env" in str(error.value)


# --- send ------------------------------------------------------------------


def test_send_posts_the_joined_text_and_reports_the_message_id(monkeypatch):
    calls = []
    code, out, err = run(
        monkeypatch,
        ["send", CHANNEL, TOPIC, "a lighthouse", "at dusk"],
        Client(calls),
    )
    assert code == 0 and err == ""
    assert ("send", CHANNEL, TOPIC, "a lighthouse at dusk") in calls
    assert "901" in out and CHANNEL in out and TOPIC in out


def test_send_joins_the_channel_before_it_posts(monkeypatch):
    """The answer may arrive in seconds, and only a subscribed channel's
    messages reach the event stream — so joining cannot wait until after."""
    calls = []
    code, out, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "hello"], Client(calls))
    kinds = [call[0] for call in calls]
    assert code == 0
    assert kinds.index("subscribe") < kinds.index("send")
    assert ("subscribe", (CHANNEL,)) in calls
    assert f"joined #{CHANNEL}" in out


def test_send_into_a_channel_already_joined_subscribes_nothing(monkeypatch):
    calls = []

    class Member(Client):
        subscribed = (CHANNEL,)

    code, out, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "hello"], Member(calls))
    assert code == 0
    assert [call for call in calls if call[0] == "subscribe"] == []
    assert "joined" not in out


def test_send_anchors_the_topic_to_the_home_conversation(monkeypatch):
    """The root note goes in first, and the real message follows it."""
    calls = []
    monkeypatch.setenv(selfnote.HOME_VARIABLE, "front/front-title-image")
    code, _, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "please draw"], Client(calls))
    assert code == 0
    posts = [call for call in calls if call[0] == "send"]
    assert posts == [
        ("send", CHANNEL, TOPIC, "[selfnote][rootchat] front/front-title-image"),
        ("send", CHANNEL, TOPIC, "please draw"),
    ]


def test_send_anchors_a_topic_once(monkeypatch):
    """A topic this bot already anchored is not anchored again."""
    calls = []
    monkeypatch.setenv(selfnote.HOME_VARIABLE, "front/front-title-image")
    existing = [
        message(
            sender="Front", sender_id=15, id=3,
            content="[selfnote][rootchat] front/front-title-image",
        )
    ]
    code, _, _ = run(
        monkeypatch, ["send", CHANNEL, TOPIC, "and one more thing"],
        Client(calls, existing),
    )
    assert code == 0
    assert [call for call in calls if call[0] == "send"] == [
        ("send", CHANNEL, TOPIC, "and one more thing")
    ]


def test_send_without_a_home_writes_no_note(monkeypatch):
    """A run nobody will call back has nothing to be called back *to*."""
    calls = []
    monkeypatch.delenv(selfnote.HOME_VARIABLE, raising=False)
    code, _, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "hi"], Client(calls))
    assert code == 0
    assert [call for call in calls if call[0] == "send"] == [
        ("send", CHANNEL, TOPIC, "hi")
    ]
    assert [call for call in calls if call[0] == "history"] == []


def test_send_into_the_home_topic_writes_no_note(monkeypatch):
    """The home conversation is not somewhere else."""
    calls = []
    monkeypatch.setenv(selfnote.HOME_VARIABLE, f"{CHANNEL}/{TOPIC}")
    code, _, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "hi"], Client(calls))
    assert code == 0
    assert [call for call in calls if call[0] == "send"] == [
        ("send", CHANNEL, TOPIC, "hi")
    ]


def test_read_hides_selfnotes_and_all_shows_them(monkeypatch):
    """A note an agent wrote to itself is not part of the conversation."""
    monkeypatch.delenv(selfnote.HOME_VARIABLE, raising=False)
    messages = [
        message(sender="Front", sender_id=15, id=3,
                content="[selfnote][rootchat] front/front-title-image"),
        message(sender="Forge", sender_id=13, id=4, content="on it"),
    ]
    _, out, _ = run(monkeypatch, ["read", CHANNEL, TOPIC], Client([], messages))
    assert "selfnote" not in out and "on it" in out
    _, out_all, _ = run(monkeypatch, ["read", CHANNEL, TOPIC, "--all"], Client([], messages))
    assert "[selfnote][rootchat] front/front-title-image" in out_all


def test_send_refuses_an_empty_message(monkeypatch):
    calls = []
    code, _, err = run(monkeypatch, ["send", CHANNEL, TOPIC, "   "], Client(calls))
    assert code == 1 and calls == []
    assert "empty" in err


# --- read ------------------------------------------------------------------


def test_read_shows_sender_and_timestamp_oldest_first(monkeypatch):
    calls = []
    client = Client(calls, messages=[message(content="first"), message(sender="Front", content="second")])
    code, out, err = run(monkeypatch, ["read", CHANNEL, TOPIC], client)
    assert code == 0 and err == ""
    assert calls == [("history", CHANNEL, TOPIC, chat.DEFAULT_READ_COUNT)]
    assert out.index("first") < out.index("second")
    assert "Forge" in out and "Front" in out and "2025-08-19" in out


def test_read_count_is_passed_through(monkeypatch):
    calls = []
    run(monkeypatch, ["read", CHANNEL, TOPIC, "--count", "3"], Client(calls, messages=[message()]))
    assert calls == [("history", CHANNEL, TOPIC, 3)]


def test_read_says_so_when_the_topic_is_empty(monkeypatch):
    code, out, err = run(monkeypatch, ["read", CHANNEL, TOPIC], Client([]))
    assert code == 0 and err == ""
    assert "no messages" in out


def test_read_rejects_a_non_positive_count(monkeypatch):
    calls = []
    code, _, err = run(monkeypatch, ["read", CHANNEL, TOPIC, "--count", "0"], Client(calls))
    assert code == 1 and calls == [] and "--count" in err


def test_read_since_asks_only_for_what_is_newer(monkeypatch):
    calls = []
    client = Client(calls, messages=[message(id=5, content="old"), message(id=9, content="new")])
    code, out, err = run(monkeypatch, ["read", CHANNEL, TOPIC, "--since", "5"], client)
    assert code == 0 and err == ""
    assert calls == [("since", CHANNEL, TOPIC, 5)]
    assert "new" in out and "old" not in out


def test_read_since_with_nothing_newer_still_succeeds(monkeypatch):
    client = Client([], messages=[message(id=5)])
    code, out, err = run(monkeypatch, ["read", CHANNEL, TOPIC, "--since", "5"], client)
    assert code == 0 and err == "" and "nothing newer" in out


def test_reading_never_touches_subscriptions(monkeypatch):
    """Looking is free and invisible; only posting joins."""
    for argv in (["read", CHANNEL, TOPIC], ["topics", CHANNEL]):
        calls = []
        run(monkeypatch, argv, Client(calls, messages=[message()], topics=["t"]))
        assert [call for call in calls if call[0] in {"subscribe", "subscriptions"}] == []


def test_a_printed_message_carries_the_id_since_takes(monkeypatch):
    code, out, _ = run(monkeypatch, ["read", CHANNEL, TOPIC], Client([], messages=[message(id=77)]))
    assert code == 0 and "77" in out


# --- topics ----------------------------------------------------------------


def test_topics_lists_the_channel_topics(monkeypatch):
    calls = []
    client = Client(calls, topics=["assetplan-title-image-1", "✔ assetplan-old-1"])
    code, out, err = run(monkeypatch, ["topics", CHANNEL], client)
    assert code == 0 and err == ""
    assert calls == [("stream_id", CHANNEL), ("topics", 42)]
    assert out.splitlines() == ["assetplan-title-image-1", "✔ assetplan-old-1"]


def test_topics_says_so_when_the_channel_is_empty(monkeypatch):
    code, out, err = run(monkeypatch, ["topics", CHANNEL], Client([]))
    assert code == 0 and err == "" and "no topics" in out


# --- channels --------------------------------------------------------------


class Listing(Client):
    channel_rows = (
        {"name": "work-title-image", "description": "project: demo; mission: cover art"},
        {"name": "pj-demo", "description": "the demo project"},
        {"name": "random", "description": ""},
    )


def test_channels_prints_the_name_and_what_it_says_it_is_for(monkeypatch):
    code, out, err = run(monkeypatch, ["channels"], Listing([]))
    assert code == 0 and err == ""
    assert out.splitlines() == [
        "pj-demo — the demo project",
        "random",
        "work-title-image — project: demo; mission: cover art",
    ]


def test_channels_prefix_narrows_the_listing(monkeypatch):
    code, out, _ = run(monkeypatch, ["channels", "--prefix", "pj-"], Listing([]))
    assert code == 0 and out.splitlines() == ["pj-demo — the demo project"]


def test_channels_says_so_when_the_prefix_matches_nothing(monkeypatch):
    code, out, err = run(monkeypatch, ["channels", "--prefix", "nope-"], Listing([]))
    assert code == 0 and err == "" and "no channels starting with nope-" in out


def test_a_multiline_description_stays_on_its_one_line(monkeypatch):
    class Wrapped(Client):
        channel_rows = ({"name": "work-x", "description": "project: demo;\nmission: art"},)

    code, out, _ = run(monkeypatch, ["channels"], Wrapped([]))
    assert out.splitlines() == ["work-x — project: demo; mission: art"]


def test_listing_channels_never_touches_subscriptions(monkeypatch):
    calls = []
    run(monkeypatch, ["channels"], Listing(calls))
    assert [call for call in calls if call[0] in {"subscribe", "subscriptions"}] == []


# --- resolve ---------------------------------------------------------------


def test_resolve_renames_the_topic_through_its_last_message(monkeypatch):
    calls = []
    client = Client(calls)
    client.holders = {TOPIC: 77}
    code, out, err = run(monkeypatch, ["resolve", CHANNEL, TOPIC], client)
    assert code == 0 and err == ""
    assert ("resolve", 77, TOPIC) in calls
    assert f"resolved #{CHANNEL} > {TOPIC}" in out


def test_resolve_says_so_when_it_was_already_resolved(monkeypatch):
    """Idempotent, and the reader is told which it was."""
    calls = []
    client = Client(calls)
    client.holders = {f"✔ {TOPIC}": 77}
    code, out, err = run(monkeypatch, ["resolve", CHANNEL, TOPIC], client)
    assert code == 0 and err == "" and "already resolved" in out
    assert [call for call in calls if call[0] == "resolve"] == []


def test_resolve_takes_the_resolved_name_too(monkeypatch):
    calls = []
    client = Client(calls)
    client.holders = {f"✔ {TOPIC}": 77}
    code, out, _ = run(monkeypatch, ["resolve", CHANNEL, f"✔ {TOPIC}"], client)
    assert code == 0 and "already resolved" in out


def test_resolving_an_empty_topic_is_an_error_not_a_rename(monkeypatch):
    calls = []
    client = Client(calls)
    client.holders = {}
    code, _, err = run(monkeypatch, ["resolve", CHANNEL, TOPIC], client)
    assert code == 1 and "nothing" not in err
    assert "no conversation here to resolve" in err or "no messages" in err
    assert [call for call in calls if call[0] == "resolve"] == []


# --- failures --------------------------------------------------------------


def test_a_zulip_failure_is_a_message_not_a_traceback(monkeypatch):
    class Failing(Client):
        def send_to_channel(self, channel, topic, content):
            raise ZulipError("channel not found")

    code, _, err = run(monkeypatch, ["send", CHANNEL, TOPIC, "hi"], Failing([]))
    assert code == 1 and "channel not found" in err


def test_a_subscription_that_fails_still_lets_the_message_through(monkeypatch):
    """The post matters more than the callback: say it, then say so."""
    calls = []

    class Unjoinable(Client):
        def subscribe_channels(self, names, principals=None):
            raise ZulipError("cannot subscribe")

    code, out, err = run(monkeypatch, ["send", CHANNEL, TOPIC, "hi"], Unjoinable(calls))
    assert code == 0 and err == ""
    assert ("send", CHANNEL, TOPIC, "hi") in calls
    assert "could not subscribe" in out


# --- documentation ---------------------------------------------------------


def test_help_is_a_usage_document_with_examples(capsys):
    with pytest.raises(SystemExit):
        chat.build_parser().parse_args(["--help"])
    text = capsys.readouterr().out
    assert "Examples" in text
    for command in ("send", "read", "topics", "channels", "resolve"):
        assert command in text
    # Waiting is not a thing an agent does any more, so it is not offered.
    assert "wait" not in text


def test_help_names_no_real_agent_channel_or_topic(capsys):
    """The examples must not hand out somebody's routing.

    A caller that copies a channel or a topic prefix out of this help has
    learned it from the tool rather than from the agent that owns it — which
    is exactly the attribution `agent_standardize` p2 exists to establish. It
    happened once, live, before the examples were made abstract.
    """
    with pytest.raises(SystemExit):
        chat.build_parser().parse_args(["--help"])
    text = capsys.readouterr().out
    for leak in ("agforge", "agfront", "agautolab", "cagent", "assetplan-", "intro-"):
        assert leak not in text
