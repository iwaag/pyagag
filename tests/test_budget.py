"""The per-credential budget at the transport boundary (`better_zulip_call`
p1 step 6): a 429 pauses every client on the credential, a different
credential is untouched, identical GETs in flight are joined, the pause is
shared across processes through the sidecar file, and calls after a pause
are spaced rather than released as a wave."""

from __future__ import annotations

import email.message
import io
import threading
import time
import urllib.error

import pytest

from agag.zulip import Budget, RateLimited, ZulipClient


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = body.encode("utf-8")
        self.headers = email.message.Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body, headers=None):
    message = email.message.Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError("https://zulip.invalid/api/v1/x", code, "err", message, io.BytesIO(body.encode()))


def client(email_address="bot@example.invalid"):
    return ZulipClient("https://zulip.invalid", email_address, "key")


def test_a_429_pauses_every_client_on_the_credential_and_no_other(monkeypatch):
    answers = [http_error(429, '{"result":"error","msg":"slow down"}', {"Retry-After": "3"}),
               FakeResponse('{"result":"success"}')]
    monkeypatch.setattr("agag.zulip.urllib.request.urlopen", lambda *a, **k: (_ for _ in ()).throw(answers[0])
                        if isinstance(answers[0], BaseException) else answers[0])
    first = client()
    with pytest.raises(RateLimited):
        first.call("GET", "users/me")
    # The pause is on the credential, not the client.
    second = client()
    assert second.budget is first.budget and second.budget.pause_until > time.time() + 2.0
    waited = []
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: waited.append(seconds))
    answers[0] = FakeResponse('{"result":"success"}')
    second.call("GET", "users/me")
    assert waited and sum(waited) >= 2.5, "the second client waited out the first one's 429"
    assert second.budget.waits == 1 and second.budget.refusals == 1
    # Another credential is not paused.
    other = client("other@example.invalid")
    waited.clear()
    other.call("GET", "users/me")
    assert waited == []


def test_identical_gets_in_flight_are_joined_not_repeated(monkeypatch):
    calls = []
    gate = threading.Event()

    def slow_urlopen(request, timeout=None, context=None):
        calls.append(request.full_url)
        gate.wait(2.0)
        return FakeResponse('{"result":"success","messages":[1]}')

    monkeypatch.setattr("agag.zulip.urllib.request.urlopen", slow_urlopen)
    c = client()
    results = []
    threads = [threading.Thread(target=lambda: results.append(c.call("GET", "streams"))) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(3.0)
    assert len(calls) == 1 and len(results) == 3 and c.budget.joined == 2
    # A POST is never joined, and a long poll is never joined.
    posts = []
    monkeypatch.setattr("agag.zulip.urllib.request.urlopen",
                        lambda request, timeout=None, context=None: posts.append(request.full_url) or FakeResponse('{"result":"success","id":1}'))
    c.call("POST", "messages", {"a": 1})
    c.call("POST", "messages", {"a": 1})
    assert len(posts) == 2


def test_the_pause_is_shared_across_processes_through_the_sidecar(tmp_path, monkeypatch):
    env = tmp_path / "bot.env"
    env.write_text("ZULIP_URL=https://zulip.invalid\nZULIP_EMAIL=bot@example.invalid\nZULIP_API_KEY=k\n")
    c = ZulipClient.from_env(env)
    assert c.budget.sidecar == tmp_path / "bot.env.ratelimit"
    # Another process wrote a pause a moment ago.
    c.budget.sidecar.write_text(f"{time.time() + 4:.3f}\n")
    waited = []
    monkeypatch.setattr("agag.zulip.time.sleep", lambda seconds: waited.append(seconds))
    monkeypatch.setattr("agag.zulip.urllib.request.urlopen",
                        lambda *a, **k: FakeResponse('{"result":"success"}'))
    c.call("GET", "users/me")
    assert waited and sum(waited) >= 3.0
    # Our own 429 is written for the others.
    monkeypatch.setattr("agag.zulip.urllib.request.urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(http_error(429, '{"msg":"no"}', {"Retry-After": "7"})))
    with pytest.raises(RateLimited):
        c.call("GET", "users/me")
    assert float(c.budget.sidecar.read_text()) > time.time() + 6.0


def test_calls_after_a_pause_are_spaced_not_released_as_a_wave():
    budget = Budget("spaced@example.invalid")
    now = [1000.0]
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    budget.pause(2.0, now=now[0])
    for _ in range(4):
        budget.wait_turn(now_fn=lambda: now[0], sleep=sleep)
    # The first waited out the pause; each next one waited a slot more.
    assert len(slept) >= 4
    assert all(s >= 0 for s in slept)
    assert slept[1:] and max(slept[1:]) <= Budget.SPACING_SECONDS * 4 + 0.001
    assert sum(slept) >= 2.0 and budget.waits >= 2
