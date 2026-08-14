"""Listener-loop and credentials tests for the shared Zulip entrance.

No network: `serve` is driven by a fake client that scripts one call sequence
per test and stops the loop by raising `_Stop`.
"""

import pytest

from agag.zulip import (
    RESOLVED_TOPIC_PREFIX,
    QueueExpired,
    ZulipClient,
    ZulipError,
    ZulipTimeout,
    channel_name,
    dm_partners,
    is_channel_message_for_us,
    is_dm_for_us,
    read_env,
    serve,
    sweep_serve,
    sweep_topics,
    topic_dump,
    topic_write,
)


class _Stop(Exception):
    """Ends the otherwise infinite listener loop from inside a fake call."""


class FakeClient:
    """Scripted stand-in for `ZulipClient`, one queued outcome per call."""

    email = "bot@example.invalid"

    def __init__(self, whoami_results, poll_results):
        self._whoami_results = list(whoami_results)
        self._poll_results = list(poll_results)
        self.registrations = 0
        self.whoami_calls = 0

    def _next(self, results):
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def whoami(self):
        self.whoami_calls += 1
        return self._next(self._whoami_results)

    def register(self):
        self.registrations += 1
        return f"queue{self.registrations}", 0

    def poll(self, queue_id, last_event_id):
        return self._next(self._poll_results)


def message_event(event_id, sender_id, content="hello", message_id=1):
    return {
        "id": event_id,
        "type": "message",
        "message": {
            "id": message_id,
            "type": "private",
            "sender_id": sender_id,
            "content": content,
            "display_recipient": [{"id": 7}, {"id": 99}],
        },
    }


def run(client):
    """Drive `serve` until a fake call raises `_Stop`; collect handled DMs."""
    seen = []
    with pytest.raises(_Stop):
        serve(client, lambda c, m, s: seen.append(m), log=lambda _: None)
    return seen


def test_read_env_ignores_comments_and_shell(tmp_path):
    path = tmp_path / "zulip.env"
    path.write_text(
        "# a comment\n"
        "ZULIP_URL=https://zulip.example.invalid\n"
        "export SOMETHING=ignored-because-two-tokens\n"
        "ZULIP_API_KEY='quoted-key'\n",
        encoding="utf-8",
    )
    env = read_env(path)
    assert env["ZULIP_URL"] == "https://zulip.example.invalid"
    assert env["ZULIP_API_KEY"] == "quoted-key"
    assert "SOMETHING" not in env


def test_from_env_names_the_missing_keys(tmp_path):
    path = tmp_path / "zulip.env"
    path.write_text("ZULIP_URL=https://zulip.example.invalid\n", encoding="utf-8")
    with pytest.raises(ZulipError) as error:
        ZulipClient.from_env(path)
    assert "ZULIP_EMAIL" in str(error.value)
    assert "ZULIP_API_KEY" in str(error.value)


def test_from_env_missing_file_names_the_path(tmp_path):
    with pytest.raises(ZulipError) as error:
        ZulipClient.from_env(tmp_path / "absent.env")
    assert "absent.env" in str(error.value)


def test_dm_partners_excludes_the_bot():
    message = {"display_recipient": [{"id": 7}, {"id": 99}]}
    assert dm_partners(message, self_id=7) == [99]


def test_is_dm_for_us_rejects_our_own_echo():
    assert is_dm_for_us({"type": "private", "sender_id": 99}, self_id=7)
    assert not is_dm_for_us({"type": "private", "sender_id": 7}, self_id=7)
    assert not is_dm_for_us({"type": "stream", "sender_id": 99}, self_id=7)


def channel_event(event_id, sender_id, channel="create-x", topic="request", message_id=1):
    return {
        "id": event_id,
        "type": "message",
        "message": {
            "id": message_id,
            "type": "stream",
            "sender_id": sender_id,
            "content": "hello",
            "display_recipient": channel,
            "subject": topic,
        },
    }


def test_is_channel_message_for_us_rejects_dms_and_our_own_echo():
    stream = channel_event(1, sender_id=99)["message"]
    assert is_channel_message_for_us(stream, self_id=7)
    assert not is_channel_message_for_us(stream, self_id=99)
    assert not is_channel_message_for_us({"type": "private", "sender_id": 99}, self_id=7)


def test_channel_name_is_empty_for_dms():
    assert channel_name(channel_event(1, sender_id=99)["message"]) == "create-x"
    assert channel_name({"display_recipient": [{"id": 7}]}) == ""


def test_serve_default_accept_still_ignores_channel_messages():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[channel_event(1, sender_id=99)], _Stop()],
    )
    assert run(client) == []


def test_serve_with_wider_accept_sees_channel_messages():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[
            [channel_event(1, sender_id=99), message_event(2, sender_id=99)],
            _Stop(),
        ],
    )
    seen = []
    with pytest.raises(_Stop):
        serve(
            client,
            lambda c, m, s: seen.append(m["type"]),
            log=lambda _: None,
            accept=lambda m, s: m.get("sender_id") != s,
        )
    assert seen == ["stream", "private"]


def test_resolve_topic_skips_an_already_resolved_topic():
    calls = []
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")
    client.call = lambda *a, **k: calls.append(a)
    client.resolve_topic(1, f"{RESOLVED_TOPIC_PREFIX}request")
    assert calls == []
    client.resolve_topic(1, "request")
    assert len(calls) == 1 and calls[0][0] == "PATCH"


def test_channel_discovery_and_subscription_wrappers():
    calls = []
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")

    def call(method, path, params=None, **kwargs):
        calls.append((method, path, params))
        if path == "streams":
            return {"streams": [{"name": "pj-one"}]}
        if path == "users/me/subscriptions" and method == "GET":
            return {"subscriptions": [{"name": "general"}]}
        return {"subscribed": {"pj-one": [7]}}

    client.call = call
    assert client.channels() == [{"name": "pj-one"}]
    assert client.subscriptions() == [{"name": "general"}]
    client.subscribe_channels(["pj-one"])
    assert calls[-1] == (
        "POST",
        "users/me/subscriptions",
        {"subscriptions": [{"name": "pj-one"}]},
    )
    before = len(calls)
    client.subscribe_channels([])
    assert len(calls) == before


def test_membership_wrappers_read_users_and_subscribe_principals():
    calls = []
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")

    def call(method, path, params=None, **kwargs):
        calls.append((method, path, params))
        if path == "users":
            return {"members": [{"user_id": 7, "is_active": True}]}
        if path == "streams/3/members":
            return {"subscribers": [7]}
        return {"subscribed": {"pj-one": [8, 9]}}

    client.call = call
    assert client.users() == [{"user_id": 7, "is_active": True}]
    assert client.channel_subscribers(3) == [7]
    client.subscribe_channels(["pj-one"], principals=[8, 9])
    assert calls[-1] == (
        "POST",
        "users/me/subscriptions",
        {"subscriptions": [{"name": "pj-one"}], "principals": [8, 9]},
    )


def test_serve_handles_a_dm_and_skips_its_own():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[
            [message_event(1, sender_id=7), message_event(2, sender_id=99)],
            _Stop(),
        ],
    )
    seen = run(client)
    assert [m["sender_id"] for m in seen] == [99]


def test_serve_survives_a_whoami_failure_at_startup():
    """The identity lookup sits inside the retry loop: a Zulip restart during
    startup must not be fatal."""
    client = FakeClient(
        whoami_results=[ZulipError("connection refused"), {"user_id": 7}],
        poll_results=[[message_event(1, sender_id=99)], _Stop()],
    )
    seen = run(client)
    assert client.whoami_calls == 2
    assert len(seen) == 1


def test_serve_reregisters_after_the_queue_expires():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[QueueExpired("bad event queue id"), [], _Stop()],
    )
    run(client)
    assert client.registrations == 2


def test_serve_treats_a_poll_timeout_as_nothing_happened():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[ZulipTimeout("long poll ended"), _Stop()],
    )
    run(client)
    assert client.registrations == 1  # the queue is still good


def test_serve_keeps_going_when_a_handler_raises():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[message_event(1, sender_id=99)], _Stop()],
    )
    logged = []

    def boom(client, message, self_id):
        raise RuntimeError("handler exploded")

    with pytest.raises(_Stop):
        serve(client, boom, log=logged.append)
    assert any("handler exploded" in line for line in logged)


def test_serve_sleeps_and_reregisters_after_a_failed_poll(monkeypatch):
    slept = []
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: slept.append(seconds))
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[ZulipError("RemoteDisconnected"), [], _Stop()],
    )
    run(client)
    assert slept and client.registrations == 2


class SweepClient(FakeClient):
    """FakeClient plus the channel-state surface `sweep_topics` reads."""

    def __init__(self, whoami_results, poll_results, topics_by_channel, last_sender):
        super().__init__(whoami_results, poll_results)
        # {channel: [topic, ...]} and {(channel, topic): sender_id of last post}
        self.topics_by_channel = dict(topics_by_channel)
        self.last_sender = dict(last_sender)
        self.history_calls = []

    def subscriptions(self):
        return [
            {"name": name, "stream_id": index}
            for index, name in enumerate(sorted(self.topics_by_channel), start=1)
        ]

    def channel_topics(self, stream_id):
        name = sorted(self.topics_by_channel)[stream_id - 1]
        return list(self.topics_by_channel[name])

    def topic_history(self, channel, topic, num_before=50):
        self.history_calls.append((channel, topic, num_before))
        sender = self.last_sender.get((channel, topic))
        return [] if sender is None else [{"sender_id": sender}]


def sweep_run(client, topic_filter="mission-"):
    """Drive `sweep_serve` until a fake call raises `_Stop`; collect matches."""
    handled = []
    with pytest.raises(_Stop):
        sweep_serve(
            client,
            lambda channel, topic: handled.append((channel, topic)),
            topic_filter=topic_filter,
            log=lambda _: None,
        )
    return handled


def test_stream_id_and_channel_topics_wrappers():
    calls = []
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")

    def call(method, path, params=None, **kwargs):
        calls.append((method, path, params))
        if path == "get_stream_id":
            return {"stream_id": 31}
        return {"topics": [{"name": "mission-two", "max_id": 9}, {"name": "mission-one", "max_id": 3}]}

    client.call = call
    assert client.stream_id("pj-demo") == 31
    assert client.channel_topics(31) == ["mission-two", "mission-one"]
    assert calls == [
        ("GET", "get_stream_id", {"stream": "pj-demo"}),
        ("GET", "users/me/31/topics", None),
    ]


def test_sweep_topics_applies_prefix_resolved_and_last_poster_rules():
    client = SweepClient(
        whoami_results=[],
        poll_results=[],
        topics_by_channel={
            "pj-demo": [
                "mission-open",                              # match
                "mission-answered",                          # bot posted last
                f"{RESOLVED_TOPIC_PREFIX}mission-done",      # resolved
                "create-other",                              # wrong prefix
                "mission-empty",                             # no history at all
            ],
        },
        last_sender={
            ("pj-demo", "mission-open"): 99,
            ("pj-demo", "mission-answered"): 7,
        },
    )
    matches = sweep_topics(client, self_id=7, topic_filter="mission-")
    assert matches == [("pj-demo", "mission-open"), ("pj-demo", "mission-empty")]
    # The last-poster check reads exactly one message per candidate topic.
    assert all(call[2] == 1 for call in client.history_calls)


def test_sweep_topics_accepts_several_prefixes():
    client = SweepClient(
        whoami_results=[],
        poll_results=[],
        topics_by_channel={
            "general": ["run-1", "mission-stray", "create-other"],
        },
        last_sender={
            ("general", "run-1"): 99,
            ("general", "mission-stray"): 99,
            ("general", "create-other"): 99,
        },
    )
    matches = sweep_topics(client, self_id=7, topic_filter=("mission-", "run-"))
    assert matches == [("general", "run-1"), ("general", "mission-stray")]


def test_sweep_serve_sweeps_on_startup_before_any_event():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[_Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    assert sweep_run(client) == [("pj-demo", "mission-waiting")]


def test_sweep_serve_resweeps_when_a_message_event_arrives():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[channel_event(1, sender_id=99)], _Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    # Once at startup, once after the event set the dirty flag.
    assert sweep_run(client) == [("pj-demo", "mission-waiting")] * 2


def test_sweep_serve_ignores_non_message_events():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[{"id": 1, "type": "heartbeat"}], _Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    assert sweep_run(client) == [("pj-demo", "mission-waiting")]  # startup only


def test_sweep_serve_sweeps_again_after_queue_expiry():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[QueueExpired("bad event queue id"), _Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    assert sweep_run(client) == [("pj-demo", "mission-waiting")] * 2
    assert client.registrations == 2


def test_sweep_serve_keeps_going_when_the_handler_raises():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[_Stop()],
        topics_by_channel={"pj-demo": ["mission-a", "mission-b"]},
        last_sender={("pj-demo", "mission-a"): 99, ("pj-demo", "mission-b"): 99},
    )
    logged = []
    handled = []

    def handler(channel, topic):
        if topic == "mission-a":
            raise RuntimeError("handler exploded")
        handled.append(topic)

    with pytest.raises(_Stop):
        sweep_serve(client, handler, topic_filter="mission-", log=logged.append)
    assert handled == ["mission-b"]
    assert any("handler exploded" in line for line in logged)


def test_topic_dump_preserves_numbered_versions(tmp_path):
    first = topic_dump("pj-demo", "mission-one", "first\n", cwd=tmp_path)
    second = topic_dump("pj-demo", "mission-one", "second\n", cwd=tmp_path)

    assert first == (
        ".local/topics/pj-demo/mission-one/1/chatlog.txt is the log of a chat "
        "you are participating in."
    )
    assert second.startswith(".local/topics/pj-demo/mission-one/2/chatlog.txt ")
    assert (tmp_path / ".local/topics/pj-demo/mission-one/1/chatlog.txt").read_text() == "first\n"
    assert (tmp_path / ".local/topics/pj-demo/mission-one/2/chatlog.txt").read_text() == "second\n"


@pytest.mark.parametrize("channel,topic", [("../outside", "mission"), ("pj-demo", "a/b")])
def test_topic_dump_rejects_path_traversal(tmp_path, channel, topic):
    with pytest.raises(ValueError):
        topic_dump(channel, topic, "content", cwd=tmp_path)


def test_topic_write_uses_an_existing_client():
    calls = []

    class Client:
        def send_to_channel(self, channel, topic, text):
            calls.append((channel, topic, text))
            return 42

    assert topic_write("mission-one", "done", channel="pj-demo", client=Client()) == "success"
    assert calls == [("pj-demo", "mission-one", "done")]


def test_topic_write_builds_client_from_shared_environment(monkeypatch, tmp_path):
    calls = []

    class Client:
        def send_to_channel(self, channel, topic, text):
            calls.append((channel, topic, text))

    credentials = tmp_path / "zulip.env"
    monkeypatch.setenv("ZULIP_CHANNEL", "pj-demo")
    monkeypatch.setenv("ZULIP_ENV", str(credentials))
    monkeypatch.setattr(ZulipClient, "from_env", lambda path: Client())

    assert topic_write("mission-one", "done") == "success"
    assert calls == [("pj-demo", "mission-one", "done")]
