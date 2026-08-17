"""Listener-loop and credentials tests for the shared Zulip entrance.

No network: `serve` is driven by a fake client that scripts one call sequence
per test and stops the loop by raising `_Stop`.
"""

import email.message
import io
import urllib.error

import pytest

from agag.status import StatusWriter
from agag.zulip import (
    RATE_LIMIT_MAX_SECONDS,
    RESOLVED_TOPIC_PREFIX,
    RETRY_SECONDS,
    QueueExpired,
    RateLimited,
    ZulipClient,
    ZulipError,
    ZulipTimeout,
    rate_limit_backoff,
    retry_after_seconds,
    SWEEP_BUDGET_RESERVE,
    channel_name,
    dm_partners,
    is_channel_message_for_us,
    is_dm_for_us,
    read_env,
    serve,
    sweep_serve,
    sweep_topics,
    topic_dump,
    topic_from_event,
    topic_write,
)


def no_status():
    """A disabled StatusWriter: listener tests must not write into the repo."""
    return StatusWriter(None)


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
        # The budget surface the real client exposes; `calls` counts every
        # request the way `ZulipClient.call` does.
        self.calls = 0
        self.rate_limit_remaining = None

    def _next(self, results):
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def whoami(self):
        self.calls += 1
        self.whoami_calls += 1
        return self._next(self._whoami_results)

    def register(self):
        self.calls += 1
        self.registrations += 1
        return f"queue{self.registrations}", 0

    def poll(self, queue_id, last_event_id):
        self.calls += 1
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
        serve(client, lambda c, m, s: seen.append(m), log=lambda _: None, status=no_status())
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
            status=no_status(),
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


def test_unsubscribe_channels_sends_names_and_skips_an_empty_list():
    calls = []
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")
    client.call = lambda method, path, params=None, **kw: (
        calls.append((method, path, params)) or {"removed": ["pj-old"]}
    )
    assert client.unsubscribe_channels(["pj-old"]) == {"removed": ["pj-old"]}
    assert calls == [("DELETE", "users/me/subscriptions", {"subscriptions": ["pj-old"]})]
    client.unsubscribe_channels(["pj-old"], principals=[11])
    assert calls[-1][2] == {"subscriptions": ["pj-old"], "principals": [11]}
    before = len(calls)
    assert client.unsubscribe_channels([]) == {"removed": [], "not_removed": []}
    assert len(calls) == before  # no request for nothing to do


def test_archive_channel_deletes_the_stream_by_id():
    calls = []
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")
    client.call = lambda method, path, params=None, **kw: (
        calls.append((method, path, params)) or {"result": "success"}
    )
    assert client.archive_channel(23) == {"result": "success"}
    assert calls == [("DELETE", "streams/23", None)]


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
        serve(client, boom, log=logged.append, status=no_status())
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
        self.subscription_calls = 0

    def subscriptions(self):
        self.calls += 1
        self.subscription_calls += 1
        return [
            {"name": name, "stream_id": index}
            for index, name in enumerate(sorted(self.topics_by_channel), start=1)
        ]

    def channel_topics(self, stream_id):
        self.calls += 1
        name = sorted(self.topics_by_channel)[stream_id - 1]
        return list(self.topics_by_channel[name])

    def topic_history(self, channel, topic, num_before=50):
        self.calls += 1
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
            status=no_status(),
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


def test_sweep_serve_turns_an_event_into_one_targeted_check():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[channel_event(1, sender_id=99, channel="pj-demo", topic="mission-new")],
                      _Stop()],
        topics_by_channel={"pj-demo": []},  # the sweep finds nothing
        last_sender={("pj-demo", "mission-new"): 99},
    )
    assert sweep_run(client) == [("pj-demo", "mission-new")]
    # The topic the event named was checked; no channel listing was re-read.
    assert client.history_calls == [("pj-demo", "mission-new", 1)]


def test_sweep_serve_coalesces_a_burst_on_one_topic_into_one_check():
    burst = [
        channel_event(i, sender_id=99, channel="pj-demo", topic="mission-new", message_id=i)
        for i in range(1, 6)
    ]
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[burst, _Stop()],
        topics_by_channel={"pj-demo": []},
        last_sender={("pj-demo", "mission-new"): 99},
    )
    # Five messages, one entry in the pending set, one verification, one call.
    assert sweep_run(client) == [("pj-demo", "mission-new")]
    assert len(client.history_calls) == 1


def test_sweep_serve_spends_nothing_on_an_event_that_cannot_match():
    events = [
        channel_event(1, sender_id=99, channel="pj-demo", topic="create-other"),   # prefix
        channel_event(2, sender_id=7, channel="pj-demo", topic="mission-mine"),    # our echo
        channel_event(3, sender_id=99, channel="pj-demo",
                      topic=f"{RESOLVED_TOPIC_PREFIX}mission-done"),               # resolved
        message_event(4, sender_id=99),                                            # a DM
    ]
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[events, _Stop()],
        topics_by_channel={"pj-demo": []},
        last_sender={},
    )
    assert sweep_run(client) == []
    assert client.history_calls == []  # not one API call was spent on them


def test_sweep_serve_rechecks_before_serving_an_event_it_already_answered():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[channel_event(1, sender_id=99, channel="pj-demo", topic="mission-new")],
                      _Stop()],
        topics_by_channel={"pj-demo": []},
        last_sender={("pj-demo", "mission-new"): 7},  # the bot posted last after all
    )
    # The event is a hint, not the truth: the check overrules it.
    assert sweep_run(client) == []
    assert len(client.history_calls) == 1


def test_topic_from_event_applies_the_same_rules_as_the_sweep():
    def message(**overrides):
        base = channel_event(1, sender_id=99, channel="pj-demo", topic="mission-new")["message"]
        return {**base, **overrides}

    assert topic_from_event(message(), 7, "mission-") == ("pj-demo", "mission-new")
    assert topic_from_event(message(), 7, ("run-", "mission-")) == ("pj-demo", "mission-new")
    assert topic_from_event(message(sender_id=7), 7, "mission-") is None
    assert topic_from_event(message(subject="create-x"), 7, "mission-") is None
    assert topic_from_event(message(type="private"), 7, "mission-") is None
    assert topic_from_event(message(display_recipient=""), 7, "mission-") is None
    assert topic_from_event(message(subject=""), 7, "") is None


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
        sweep_serve(client, handler, topic_filter="mission-", log=logged.append, status=no_status())
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


class RecordingStatus:
    """Records the StatusWriter calls a listener loop makes, in order."""

    def __init__(self):
        self.calls = []

    def record_poll_ok(self, queue_id):
        self.calls.append(("ok", queue_id))

    def record_error(self, error):
        self.calls.append(("error", str(error)))


def test_serve_records_a_poll_only_when_the_poll_returned():
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[
            [],                                   # returned, nothing in it -> still alive
            ZulipTimeout("events timed out"),     # never returned -> not alive
            QueueExpired("bad event queue id"),   # failed -> not alive
            [message_event(1, sender_id=99)],     # returned -> alive
            _Stop(),
        ],
    )
    status = RecordingStatus()
    with pytest.raises(_Stop):
        serve(client, lambda c, m, s: None, log=lambda _: None, status=status)
    assert [kind for kind, _ in status.calls] == ["ok", "error", "error", "ok"]
    assert status.calls[0] == ("ok", "queue1")


# --- rate limiting (lighter_agag_listen p1 step 1) -------------------------


def http_error(code, body, headers=None):
    """A urllib HTTPError shaped the way urlopen hands one to `call`."""
    message = email.message.Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(
        "https://zulip.example.invalid/api/v1/events", code, "error",
        message, io.BytesIO(body.encode("utf-8")),
    )


def client_whose_calls_raise(error, monkeypatch):
    """A real `ZulipClient` whose transport always raises `error`."""
    def urlopen(*args, **kwargs):
        raise error

    monkeypatch.setattr("agag.zulip.urllib.request.urlopen", urlopen)
    return ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")


def test_call_raises_rate_limited_with_the_servers_retry_after(monkeypatch):
    client = client_whose_calls_raise(
        http_error(
            429,
            '{"result":"error","code":"RATE_LIMIT_HIT","msg":"API usage exceeded rate limit"}',
            {"Retry-After": "34.2", "x-ratelimit-remaining": "0"},
        ),
        monkeypatch,
    )
    with pytest.raises(RateLimited) as error:
        client.call("GET", "events")
    assert error.value.retry_after == pytest.approx(34.2)
    assert "429" in str(error.value)


def test_call_falls_back_to_the_json_body_then_to_the_default(monkeypatch):
    client = client_whose_calls_raise(
        http_error(429, '{"msg":"slow down","retry-after":12}'), monkeypatch
    )
    with pytest.raises(RateLimited) as error:
        client.call("GET", "events")
    assert error.value.retry_after == pytest.approx(12)

    client = client_whose_calls_raise(http_error(429, "not json at all"), monkeypatch)
    with pytest.raises(RateLimited) as error:
        client.call("GET", "events")
    assert error.value.retry_after == pytest.approx(60)


def test_call_still_distinguishes_a_dead_queue_from_a_rate_limit(monkeypatch):
    client = client_whose_calls_raise(
        http_error(400, '{"code":"BAD_EVENT_QUEUE_ID","msg":"gone"}'), monkeypatch
    )
    with pytest.raises(QueueExpired):
        client.call("GET", "events")


def test_retry_after_prefers_the_header_over_the_body():
    headers = email.message.Message()
    headers["x-ratelimit-reset"] = "9"
    assert retry_after_seconds(headers, {"retry-after": 99}) == pytest.approx(9)
    assert retry_after_seconds(None, None) == pytest.approx(60)


def test_rate_limit_backoff_floors_at_retry_seconds_doubles_and_is_capped():
    plain = lambda low, high: 0.0  # noqa: E731 - jitter off for the arithmetic
    assert rate_limit_backoff(1, strikes=1, jitter=plain) == RETRY_SECONDS
    assert rate_limit_backoff(30, strikes=1, jitter=plain) == 30
    assert rate_limit_backoff(30, strikes=3, jitter=plain) == 120
    assert rate_limit_backoff(30, strikes=9, jitter=plain) == RATE_LIMIT_MAX_SECONDS
    assert rate_limit_backoff(30, strikes=1) > 30  # jitter is additive


def test_serve_keeps_its_queue_and_honours_retry_after_on_429(monkeypatch):
    slept = []
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: slept.append(seconds))
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[RateLimited("429", retry_after=42), [], _Stop()],
    )
    run(client)
    assert client.registrations == 1  # the queue was never dropped
    assert slept[0] >= 42


def test_sweep_serve_keeps_its_queue_and_its_pending_sweep_on_429(monkeypatch):
    slept = []
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: slept.append(seconds))
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[RateLimited("429", retry_after=42), _Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    # Startup sweep, then a 429 on the poll: no re-register, no second sweep
    # (the first one completed), and the wait honours the header.
    assert sweep_run(client) == [("pj-demo", "mission-waiting")]
    assert client.registrations == 1
    assert slept == [pytest.approx(42, abs=4.3)]


def test_sweep_serve_resumes_a_sweep_that_a_429_cut_short(monkeypatch):
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: None)

    class Interrupted(SweepClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.subscription_calls = 0

        def subscriptions(self):
            self.subscription_calls += 1
            if self.subscription_calls == 1:
                raise RateLimited("429 mid-sweep", retry_after=1)
            return super().subscriptions()

    client = Interrupted(
        whoami_results=[{"user_id": 7}],
        poll_results=[_Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    # The startup sweep dies on its first call; `dirty` survives the backoff,
    # so the retry sweeps rather than dropping the work on the floor.
    assert sweep_run(client) == [("pj-demo", "mission-waiting")]
    assert client.registrations == 1


def test_rate_limit_backoff_grows_while_429s_repeat_and_resets_after_success(monkeypatch):
    slept = []
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr("agag.zulip.random.uniform", lambda low, high: 0.0)
    client = FakeClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[
            RateLimited("429", retry_after=10),
            RateLimited("429", retry_after=10),
            RateLimited("429", retry_after=10),
            [],                                    # a poll returns: strike count resets
            RateLimited("429", retry_after=10),
            _Stop(),
        ],
    )
    run(client)
    assert slept == [10, 20, 40, 10]


# --- budget visibility and resumable pending (p1 step 2) -------------------


class FakeResponse:
    """The bits of an `http.client.HTTPResponse` that `call` touches."""

    def __init__(self, body, headers):
        self._body = body.encode("utf-8")
        self.headers = email.message.Message()
        for name, value in headers.items():
            self.headers[name] = value

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_call_records_the_quota_headers_from_a_successful_response(monkeypatch):
    monkeypatch.setattr(
        "agag.zulip.urllib.request.urlopen",
        lambda *a, **k: FakeResponse(
            '{"result":"success"}',
            {"x-ratelimit-remaining": "173", "x-ratelimit-limit": "200"},
        ),
    )
    client = ZulipClient("https://zulip.example.invalid", "bot@example.invalid", "key")
    assert client.rate_limit_remaining is None
    client.call("GET", "users/me")
    assert client.rate_limit_remaining == 173
    assert client.rate_limit_limit == 200
    assert client.calls == 1


def test_call_records_a_spent_budget_from_the_429_itself(monkeypatch):
    client = client_whose_calls_raise(
        http_error(429, '{"msg":"slow"}', {"Retry-After": "7", "x-ratelimit-remaining": "0"}),
        monkeypatch,
    )
    with pytest.raises(RateLimited):
        client.call("GET", "events")
    assert client.rate_limit_remaining == 0  # zero is a reading, not a missing one


def test_sweep_serve_defers_the_full_sweep_when_the_window_is_nearly_spent():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[], _Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    client.rate_limit_remaining = SWEEP_BUDGET_RESERVE - 1
    logged = []
    with pytest.raises(_Stop):
        sweep_serve(
            client, lambda channel, topic: None, topic_filter="mission-",
            log=logged.append, status=no_status(),
        )
    # Deferred, not slept through and not errored: the long poll is the wait.
    assert client.subscription_calls == 0
    assert any("full sweep deferred" in line for line in logged)


def test_sweep_serve_runs_the_deferred_sweep_once_the_window_slides():
    class Recovering(SweepClient):
        def poll(self, queue_id, last_event_id):
            self.rate_limit_remaining = 200  # the window slid while we polled
            return super().poll(queue_id, last_event_id)

    client = Recovering(
        whoami_results=[{"user_id": 7}],
        poll_results=[[], _Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"]},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    client.rate_limit_remaining = SWEEP_BUDGET_RESERVE - 1
    assert sweep_run(client) == [("pj-demo", "mission-waiting")]
    assert client.subscription_calls == 1


def test_sweep_serve_logs_one_line_per_full_sweep_with_its_cost():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[_Stop()],
        topics_by_channel={"pj-demo": ["mission-waiting"], "general": []},
        last_sender={("pj-demo", "mission-waiting"): 99},
    )
    client.rate_limit_remaining = 190
    logged = []
    with pytest.raises(_Stop):
        sweep_serve(
            client, lambda channel, topic: None, topic_filter="mission-",
            log=logged.append, status=no_status(),
        )
    line = [entry for entry in logged if entry.startswith("full sweep:")]
    # 1 subscriptions + 2 channel_topics + 1 topic_history for the candidate.
    assert line == ["full sweep: 1 awaiting, 4 calls spent, 190 left in the window"]


def test_sweep_serve_serves_the_rest_of_pending_after_a_rate_limit(monkeypatch):
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: None)

    class Limited(SweepClient):
        def topic_history(self, channel, topic, num_before=50):
            if not self.history_calls:
                self.history_calls.append((channel, topic, num_before))
                raise RateLimited("429 mid-serve", retry_after=1)
            return super().topic_history(channel, topic, num_before)

    events = [
        channel_event(1, sender_id=99, channel="pj-demo", topic="mission-a", message_id=1),
        channel_event(2, sender_id=99, channel="pj-demo", topic="mission-b", message_id=2),
    ]
    client = Limited(
        whoami_results=[{"user_id": 7}],
        poll_results=[events, _Stop()],
        topics_by_channel={"pj-demo": []},
        last_sender={("pj-demo", "mission-a"): 99, ("pj-demo", "mission-b"): 99},
    )
    # The 429 lands on the first pending entry; both are still served.
    assert sweep_run(client) == [("pj-demo", "mission-a"), ("pj-demo", "mission-b")]
    assert client.registrations == 1


def test_sweep_serve_records_a_poll_only_when_the_poll_returned():
    client = SweepClient(
        whoami_results=[{"user_id": 7}],
        poll_results=[[], ZulipError("boom"), _Stop()],
        topics_by_channel={},
        last_sender={},
    )
    status = RecordingStatus()
    with pytest.raises(_Stop):
        sweep_serve(
            client, lambda channel, topic: None, topic_filter="mission-",
            log=lambda _: None, status=status,
        )
    assert [kind for kind, _ in status.calls] == ["ok", "error"]
