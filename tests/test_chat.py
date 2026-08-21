"""`agentchat`: identity comes from the environment, and posting participates.

What is pinned here is the contract an agent's run depends on — which
credentials file speaks, that reading makes exactly the Zulip calls it needs
and never touches subscriptions, that *posting* joins the channel and records
the participation so the answer can find this agent after the run is over,
and that a misconfigured environment fails with a message that names what was
missing rather than a traceback.
"""

import io

import pytest

from agag import chat, participation
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


def test_send_records_the_participation_against_the_home_conversation(
    monkeypatch, tmp_path
):
    ledger = tmp_path / "participations.jsonl"
    monkeypatch.setenv(participation.HOME_VARIABLE, "work-s2-10/workrun-task1-s2-10")
    monkeypatch.setenv(participation.LEDGER_VARIABLE, str(ledger))
    code, _, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "please draw"], Client([]))
    assert code == 0
    rows = participation.entries(ledger)
    assert rows == [
        {
            "remote": f"{CHANNEL}/{TOPIC}",
            "home": "work-s2-10/workrun-task1-s2-10",
            "message_id": 901,
            "at": rows[0]["at"],
        }
    ]


def test_send_without_a_home_records_nothing(monkeypatch, tmp_path):
    """A run nobody will call back has nothing to be called back *to*."""
    ledger = tmp_path / "participations.jsonl"
    monkeypatch.delenv(participation.HOME_VARIABLE, raising=False)
    monkeypatch.setenv(participation.LEDGER_VARIABLE, str(ledger))
    code, _, _ = run(monkeypatch, ["send", CHANNEL, TOPIC, "hi"], Client([]))
    assert code == 0 and not ledger.exists()


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
    for command in ("send", "read", "topics"):
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
