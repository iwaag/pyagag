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
