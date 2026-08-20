"""`agentchat`: identity comes from the environment, and nothing subscribes.

What is pinned here is the contract an agent's run depends on — which
credentials file speaks, that each subcommand makes exactly the Zulip calls
it needs and no subscription call ever, and that a misconfigured environment
fails with a message that names what was missing rather than a traceback.
"""

import io

import pytest

from agag import chat
from agag.zulip import ZulipError

CHANNEL = "agforge-agstudio1"
TOPIC = "create-title-image-1"


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

    def stream_id(self, name):
        self.calls.append(("stream_id", name))
        return 42

    def channel_topics(self, stream_id):
        self.calls.append(("topics", stream_id))
        return self.topics

    def subscribe_channels(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("agentchat must never touch subscriptions")


def run(monkeypatch, argv, client):
    monkeypatch.setattr(chat, "client_from_environment", lambda: client)
    out, err = io.StringIO(), io.StringIO()
    code = chat.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def message(sender="Forge", content="on it", timestamp=1755600000, sender_id=13):
    return {
        "id": 5,
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
    assert calls == [("send", CHANNEL, TOPIC, "a lighthouse at dusk")]
    assert "901" in out and CHANNEL in out and TOPIC in out


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


# --- topics ----------------------------------------------------------------


def test_topics_lists_the_channel_topics(monkeypatch):
    calls = []
    client = Client(calls, topics=["create-title-image-1", "✔ create-old-1"])
    code, out, err = run(monkeypatch, ["topics", CHANNEL], client)
    assert code == 0 and err == ""
    assert calls == [("stream_id", CHANNEL), ("topics", 42)]
    assert out.splitlines() == ["create-title-image-1", "✔ create-old-1"]


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


# --- documentation ---------------------------------------------------------


def test_help_is_a_usage_document_with_examples(capsys):
    with pytest.raises(SystemExit):
        chat.build_parser().parse_args(["--help"])
    text = capsys.readouterr().out
    assert "Examples" in text
    for command in ("send", "read", "topics"):
        assert command in text


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
    for leak in ("agforge", "agfront", "agautolab", "cagent", "create-", "intro-"):
        assert leak not in text
