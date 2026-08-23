"""Portable offline suite for agcode — one file, no network, no ollama.

Runs unchanged whether ``agcode`` is imported as ``agag.agcode`` (installed
package) or as a top-level ``agcode`` module sitting on the import path, so
the same file can be exercised standalone and inside the package suite.

The fake Messages backend (threaded stdlib http.server, scripted responses,
verbatim request capture) is inlined below as fixtures, so the file is
self-contained. Live-model batches against a real backend are out of scope
here; everything below runs offline.
"""

from __future__ import annotations

import json
import os
import re
import string
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

try:
    from agag import agcode  # installed as part of the package
except ImportError:
    import agcode  # standalone module on the import path

# Module spelling and import root, valid for both homes — the CLI tests run
# ``python -m <module>`` as a subprocess and need the right PYTHONPATH.
MODULE = agcode.__name__
PKG_ROOT = Path(agcode.__file__).resolve().parents[len(MODULE.split(".")) - 1]


# --- Fake Messages backend (inlined so the file stands alone) ----------------


class RawResponse:
    """A scripted response sent verbatim — for misbehaving-backend tests."""

    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "application/json"):
        self.body = body
        self.status = status
        self.content_type = content_type


class FakeBackend:
    def __init__(self):
        self.requests: list[dict] = []  # parsed JSON bodies, in order
        self.headers: list[dict] = []  # lower-cased header dicts, in order
        self.responses: list[dict | RawResponse] = []  # queue; popped per request
        self.delay_s = 0.0  # applied before every response; deadline tests use it

        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                assert self.path == "/v1/messages"
                length = int(self.headers["Content-Length"])
                backend.requests.append(json.loads(self.rfile.read(length)))
                backend.headers.append({k.lower(): v for k, v in self.headers.items()})
                if backend.delay_s:
                    time.sleep(backend.delay_s)
                if backend.responses:
                    status, body = 200, backend.responses.pop(0)
                else:
                    status, body = 500, {"error": "fake backend: no scripted response"}
                if isinstance(body, RawResponse):
                    status, data, ctype = body.status, body.body, body.content_type
                else:
                    data, ctype = json.dumps(body).encode(), "application/json"
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def backend():
    b = FakeBackend()
    yield b
    b.close()


def tool_use_response(
    name: str, args: dict, *, tool_id: str = "tu_1", stop_reason: str = "tool_use"
) -> dict:
    """A Messages response asking for one tool call (with a thinking block,
    matching the shape observed live from qwen3.6 via ollama)."""
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "let me use a tool"},
            {"type": "tool_use", "id": tool_id, "name": name, "input": args},
        ],
        "model": "fake",
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 20, "output_tokens": 15},
    }


def text_response(text: str, *, stop_reason: str = "end_turn") -> dict:
    """A minimal well-formed Messages API response with one text block."""
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "fake",
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def run(backend, tmp_path, **kw):
    kw.setdefault("model", "fake-model")
    return agcode.run("the task", str(tmp_path), base_url=backend.url, **kw)


# --- Loop happy path ---------------------------------------------------------


def test_single_turn_done(backend, tmp_path):
    backend.responses.append(text_response("all done"))
    r = run(backend, tmp_path)
    assert (r.status, r.output) == ("ok", "all done")
    assert r.meta["outcome"] == "done"
    assert r.meta["num_turns"] == 1
    assert r.meta["usage"] == {"input_tokens": 10, "output_tokens": 5}
    [req] = backend.requests
    assert req["messages"] == [{"role": "user", "content": "the task"}]
    assert req["tools"] == agcode.TOOLS_V0
    assert str(tmp_path.resolve()) in req["system"]
    [headers] = backend.headers
    assert headers["anthropic-version"] == agcode.ANTHROPIC_VERSION


def test_tool_roundtrip(backend, tmp_path):
    (tmp_path / "note.txt").write_text("marker-123")
    backend.responses.append(tool_use_response("read", {"path": "note.txt"}))
    backend.responses.append(text_response("marker-123"))

    r = run(backend, tmp_path)
    assert r.output == "marker-123"
    assert r.meta["num_turns"] == 2
    assert r.meta["usage"] == {"input_tokens": 30, "output_tokens": 20}

    second = backend.requests[1]["messages"]
    # user task, assistant tool_use (thinking block included verbatim), tool_result
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"][0]["type"] == "thinking"
    [tool_result] = second[2]["content"]
    assert tool_result == {
        "type": "tool_result",
        "tool_use_id": "tu_1",
        "content": "marker-123",
    }


def test_on_event_stream(backend, tmp_path):
    """Events mirror the conversation as it happens, in claude_code's
    stream-json spellings, and a consumer that raises never fails the run."""
    (tmp_path / "note.txt").write_text("marker-123")
    backend.responses.append(tool_use_response("read", {"path": "note.txt"}))
    backend.responses.append(text_response("marker-123"))
    events = []

    def consume(event):
        events.append(event)
        raise RuntimeError("a progress rendering bug")

    r = run(backend, tmp_path, on_event=consume)

    assert r.meta["outcome"] == "done"
    assert [e["type"] for e in events] == ["assistant", "user", "assistant"]
    assert events[0]["message"]["content"][1]["type"] == "tool_use"
    [tool_result] = events[1]["message"]["content"]
    assert tool_result["content"] == "marker-123"
    assert events[2]["message"]["content"] == [{"type": "text", "text": "marker-123"}]


def test_malformed_tool_calls_continue_and_count(backend, tmp_path):
    """Unknown tool / bad arguments come back as error tool_results (loop
    continues) and count as malformed; a legitimate call failing at runtime
    (missing file) does not count."""
    backend.responses.append(tool_use_response("teleport", {"to": "moon"}))
    backend.responses.append(tool_use_response("read", {"wrong_arg": True}))
    backend.responses.append(tool_use_response("read", {"path": "missing.txt"}))
    backend.responses.append(text_response("gave up gracefully"))

    r = run(backend, tmp_path)
    assert r.meta["outcome"] == "done"
    assert r.meta["num_turns"] == 4
    assert r.meta["malformed_tool_calls"] == 2
    for i, expected in [(1, "unknown tool"), (2, "bad arguments"), (3, "FileNotFoundError")]:
        [tool_result] = backend.requests[i]["messages"][-1]["content"]
        assert tool_result["is_error"] is True
        assert expected in tool_result["content"]


def test_response_text_skips_thinking_blocks():
    resp = {
        "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "agcode"},
        ]
    }
    assert agcode.response_text(resp) == "hello agcode"


def test_transcript_verbatim_with_header(backend, tmp_path):
    (tmp_path / "wd").mkdir()
    backend.responses.append(tool_use_response("list", {}))
    backend.responses.append(text_response("done"))
    tpath = tmp_path / "t" / "run.jsonl"

    r = run(
        backend,
        tmp_path / "wd",
        transcript_path=str(tpath),
        transcript_meta={"wd_marker": "WD-X-1"},
    )
    lines = [json.loads(l) for l in tpath.read_text().splitlines()]
    header = lines[0]
    assert header["record"] == "meta"
    assert header["format"] == agcode.TRANSCRIPT_FORMAT
    assert header["run_id"] == r.meta["run_id"]
    assert header["working_dir"] == str((tmp_path / "wd").resolve())
    assert header["wd_marker"] == "WD-X-1"
    body_lines = lines[1:]
    assert [l["direction"] for l in body_lines] == ["request", "response"] * 2
    # Requests on the wire and in the transcript are byte-identical dicts.
    assert [l["body"] for l in body_lines if l["direction"] == "request"] == backend.requests
    assert r.meta["transcript"] == str(tpath)


def test_no_transcript_path_still_runs(backend, tmp_path):
    backend.responses.append(text_response("done"))
    r = run(backend, tmp_path, transcript_meta={"ignored": True})
    assert r.meta["outcome"] == "done"
    assert "transcript" not in r.meta


def test_run_requires_model(monkeypatch, tmp_path):
    monkeypatch.delenv("AGCODE_MODEL", raising=False)
    with pytest.raises(ValueError, match="model"):
        agcode.run("do nothing", str(tmp_path))


# --- Tool layer: path resolution -------------------------------------------


@pytest.fixture
def base(tmp_path):
    (tmp_path / "sub" / "deep").mkdir(parents=True)
    (tmp_path / "top.txt").write_text("top")
    (tmp_path / "sub" / "inner.txt").write_text("inner")
    return tmp_path


def test_resolve_path_rules(base):
    assert agcode.resolve_path(base, "top.txt") == base / "top.txt"
    assert agcode.resolve_path(base, "sub/inner.txt") == base / "sub" / "inner.txt"
    assert agcode.resolve_path(base / "sub", "../top.txt") == base / "top.txt"
    assert agcode.resolve_path(base, "./sub/./inner.txt") == base / "sub" / "inner.txt"
    # absolute passthrough (pathlib semantics of /)
    assert agcode.resolve_path(base / "sub", str(base / "top.txt")) == base / "top.txt"


def test_tools_read_write_list(base):
    assert agcode.tool_read(base, "sub/inner.txt") == "inner"
    with pytest.raises(FileNotFoundError):
        agcode.tool_read(base, "nope.txt")
    agcode.tool_write(base, "new/dir/file.txt", "hi")
    assert (base / "new" / "dir" / "file.txt").read_text() == "hi"
    assert agcode.tool_list(base) == "new/\nsub/\ntop.txt"


def test_tool_run_cwd_and_pwd_agree(base):
    out = json.loads(agcode.tool_run(base, "echo cwd=$(pwd) pwd=$PWD"))
    assert out["exit_code"] == 0
    resolved = str(Path(base).resolve())
    assert out["stdout"].strip() == f"cwd={resolved} pwd={resolved}"
    out = json.loads(agcode.tool_run(base, "cat sub/inner.txt"))
    assert out["stdout"] == "inner"


# --- Failure kinds: every member of the vocabulary, offline ------------------


def assert_failure(r, outcome, kind):
    assert r.meta["outcome"] == outcome
    assert r.status == {"done": "ok"}.get(outcome, outcome)
    assert r.meta["failure_kind"] == kind
    assert kind in agcode.FAILURE_KINDS
    # P4 record conformance: the kind prefixes the free-text failure string.
    assert r.meta["failure"].startswith(f"{kind}: ")
    assert r.meta["failure"][len(kind) + 2 :]  # human-readable detail stays


def test_garbage_json_body(backend, tmp_path):
    backend.responses.append(RawResponse(b"<html>ollama exploded</html>"))
    r = run(backend, tmp_path)
    assert_failure(r, "failed", "malformed_response")
    assert "non-JSON" in r.meta["failure"]


def test_missing_content_key(backend, tmp_path):
    backend.responses.append(
        {"id": "msg", "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}}
    )
    r = run(backend, tmp_path)
    assert_failure(r, "failed", "malformed_response")


def test_tool_use_stop_without_tool_use_blocks(backend, tmp_path):
    backend.responses.append(text_response("thinking about tools", stop_reason="tool_use"))
    r = run(backend, tmp_path)
    assert_failure(r, "failed", "malformed_response")


def test_empty_final_text_is_a_done_run_that_says_so(backend, tmp_path):
    """A clean stop with no closing text is a fact, not a failure: what the
    run achieved is its files and flags, which only the caller can weigh."""
    backend.responses.append(text_response("  \n\t"))
    r = run(backend, tmp_path)
    assert (r.status, r.meta["outcome"]) == ("ok", "done")
    assert r.meta["empty_final"] is True
    assert "failure" not in r.meta
    assert r.output == "  \n\t"


def test_connection_refused(tmp_path):
    dead = FakeBackend()
    dead.close()  # port is now closed; connecting gets refused
    r = agcode.run("the task", str(tmp_path), base_url=dead.url, model="fake-model")
    assert_failure(r, "failed", "connect_error")
    assert r.meta["num_turns"] == 0


def test_http_error_is_connect_error(backend, tmp_path):
    r = run(backend, tmp_path)  # no scripted response -> 500
    assert_failure(r, "failed", "connect_error")
    assert "MessagesError" in r.meta["failure"]


def test_deadline_via_slow_response(backend, tmp_path):
    """A response slower than the whole deadline is cut off mid-request (P4ex1
    step 2: the deadline bounds wall-clock, so this no longer waits for the
    response first — the outcome and kind are unchanged)."""
    backend.delay_s = 0.3
    backend.responses.append(tool_use_response("list", {}))
    r = run(backend, tmp_path, deadline_s=0.2)
    assert_failure(r, "aborted", "deadline_exceeded")
    assert r.meta["num_turns"] == 0


def test_deadline_trips_between_requests(backend, tmp_path):
    """The between-requests check still fires when the responses themselves
    arrive in time: turn 1 lands, the deadline falls inside turn 2."""
    backend.delay_s = 0.3
    backend.responses.append(tool_use_response("list", {}))
    backend.responses.append(text_response("never sent"))
    r = run(backend, tmp_path, deadline_s=0.5)
    assert_failure(r, "aborted", "deadline_exceeded")
    assert r.meta["num_turns"] == 1


def test_deadline_aborts_before_first_request(backend, tmp_path):
    r = run(backend, tmp_path, deadline_s=-1)
    assert_failure(r, "aborted", "deadline_exceeded")
    assert backend.requests == []


def test_deadline_bounds_wall_clock_against_a_hung_backend(backend, tmp_path):
    """The deadline must bound wall-clock, not just the gap between requests:
    a backend that accepts the connection and then stalls used to block for
    the client's own 300 s timeout. The per-request timeout is wired to the
    remaining deadline, so the run ends near deadline_s."""
    backend.delay_s = 3.0
    backend.responses.append(text_response("too late"))

    started = time.monotonic()
    r = run(backend, tmp_path, deadline_s=0.5)
    elapsed = time.monotonic() - started

    assert_failure(r, "aborted", "deadline_exceeded")
    assert elapsed < 1.5, f"run outlived its deadline: {elapsed:.2f}s"
    assert r.meta["num_turns"] == 0  # the stalled response never arrived
    assert backend.requests  # ...but the request did reach the backend


def test_request_timeout_is_capped_by_the_client_timeout(tmp_path):
    """The wiring narrows the per-request timeout, never widens it past the
    client's own."""
    client = agcode.MessagesClient("http://127.0.0.1:1", "m", timeout_s=2.0)
    assert client.request_timeout(remaining_s=10.0) == 2.0
    assert client.request_timeout(remaining_s=0.5) == 0.5
    assert client.request_timeout(remaining_s=None) == 2.0


def test_a_slow_but_answering_backend_still_completes(backend, tmp_path):
    """The narrowed timeout must not cut off a backend that answers within
    the deadline."""
    backend.delay_s = 0.3
    backend.responses.append(text_response("in time"))
    r = run(backend, tmp_path, deadline_s=10)
    assert (r.meta["outcome"], r.output) == ("done", "in time")


def test_turn_budget_exhausted(backend, tmp_path):
    backend.responses.append(tool_use_response("list", {}))
    r = run(backend, tmp_path, max_turns=1)
    assert_failure(r, "aborted", "turn_budget_exhausted")


def test_clean_run_has_no_failure_keys(backend, tmp_path):
    backend.responses.append(text_response("done"))
    r = run(backend, tmp_path)
    assert r.meta["malformed_tool_calls"] == 0
    assert "failure" not in r.meta and "failure_kind" not in r.meta


# --- The never-raises promise: backend-shape surprises -----------------------
#
# AgcodeResult's docstring pins "once arguments are valid, run() returns, never
# raises". Each test below demonstrated an escape route before the P4ex1 step 1
# fixes; all of them normalize into the existing vocabulary (no new
# FAILURE_KINDS members).


def test_json_body_is_not_an_object(backend, tmp_path):
    """A body that parses but is a list, not an object: resp.get() used to
    raise AttributeError straight out of run()."""
    backend.responses.append(RawResponse(b"[1, 2]"))
    r = run(backend, tmp_path)
    assert_failure(r, "failed", "malformed_response")
    assert "list" in r.meta["failure"]


def test_json_body_is_a_bare_string(backend, tmp_path):
    backend.responses.append(RawResponse(b'"hi"'))
    r = run(backend, tmp_path)
    assert_failure(r, "failed", "malformed_response")


def test_null_usage_is_tolerated(backend, tmp_path):
    """``"usage": null`` used to call .get on None. Usage is telemetry, not
    contract: a missing/odd usage block must not fail an otherwise good run."""
    resp = text_response("all done")
    resp["usage"] = None
    backend.responses.append(resp)
    r = run(backend, tmp_path)
    assert (r.meta["outcome"], r.output) == ("done", "all done")
    assert r.meta["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_non_numeric_usage_values_are_tolerated(backend, tmp_path):
    resp = text_response("all done")
    resp["usage"] = {"input_tokens": "many", "output_tokens": None}
    backend.responses.append(resp)
    r = run(backend, tmp_path)
    assert r.meta["outcome"] == "done"
    assert r.meta["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_non_dict_content_block_with_text(backend, tmp_path):
    """A bare string inside content: response_text() used to call .get on it.
    The text blocks around it still make a valid answer."""
    resp = text_response("all done")
    resp["content"] = ["stray", {"type": "text", "text": "all done"}, 7]
    backend.responses.append(resp)
    r = run(backend, tmp_path)
    assert (r.meta["outcome"], r.output) == ("done", "all done")


def test_content_blocks_all_non_dict_leave_an_empty_final(backend, tmp_path):
    resp = text_response("ignored")
    resp["content"] = ["stray", 7]
    backend.responses.append(resp)
    r = run(backend, tmp_path)
    assert (r.meta["outcome"], r.output) == ("done", "")
    assert r.meta["empty_final"] is True


# The scripted-garbage corpus: deliberately malformed-but-parseable responses.
# Each must come back as a normal AgcodeResult — never an exception.
GARBAGE_CORPUS = {
    "list_body": [1, 2],
    "string_body": "hi",
    "number_body": 42,
    "null_body": None,
    "bool_body": True,
    "empty_object": {},
    "content_is_string": {"content": "hello", "stop_reason": "end_turn"},
    "content_is_dict": {"content": {"type": "text"}, "stop_reason": "end_turn"},
    "content_is_null": {"content": None, "stop_reason": "end_turn"},
    "content_blocks_are_strings": {"content": ["a", "b"], "stop_reason": "end_turn"},
    "content_blocks_are_null": {"content": [None], "stop_reason": "end_turn"},
    "text_block_without_text": {"content": [{"type": "text"}], "stop_reason": "end_turn"},
    "text_is_not_a_string": {"content": [{"type": "text", "text": 5}], "stop_reason": "end_turn"},
    "usage_null": {**text_response("x"), "usage": None},
    "usage_is_a_list": {**text_response("x"), "usage": [1]},
    "stop_reason_missing": {"content": [{"type": "text", "text": "x"}]},
    "stop_reason_null": {"content": [{"type": "text", "text": "x"}], "stop_reason": None},
    "stop_reason_unknown": {"content": [{"type": "text", "text": "x"}], "stop_reason": "wat"},
    "tool_use_stop_no_blocks": {"content": [{"type": "text", "text": "x"}], "stop_reason": "tool_use"},
    "tool_use_block_without_name": {
        "content": [{"type": "tool_use", "id": "t1"}],
        "stop_reason": "tool_use",
    },
    "tool_use_input_is_a_string": {
        "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": "note.txt"}],
        "stop_reason": "tool_use",
    },
    "tool_use_name_is_null": {
        "content": [{"type": "tool_use", "id": "t1", "name": None, "input": {}}],
        "stop_reason": "tool_use",
    },
    "deeply_nested_junk": {"content": [{"type": "text", "text": "x"}], "extra": {"a": [{"b": None}]}},
}


@pytest.mark.parametrize("name", sorted(GARBAGE_CORPUS))
def test_garbage_corpus_never_raises(backend, tmp_path, name):
    """Every entry: run() returns a normal AgcodeResult with a sensible
    outcome, no exception, and the loop is bounded (max_turns is a backstop
    for entries that keep the tool loop alive)."""
    backend.responses.append(GARBAGE_CORPUS[name])
    r = run(backend, tmp_path, max_turns=3)
    assert isinstance(r, agcode.AgcodeResult)
    assert r.meta["outcome"] in ("done", "failed", "aborted")
    assert r.status == {"done": "ok"}.get(r.meta["outcome"], r.meta["outcome"])
    if r.meta["outcome"] != "done":
        assert r.meta["failure_kind"] in agcode.FAILURE_KINDS
        assert r.meta["failure"].startswith(f"{r.meta['failure_kind']}: ")
    assert isinstance(r.output, str)
    assert isinstance(r.meta["usage"]["input_tokens"], int)


# --- The never-raises promise: tool-runtime surprises ------------------------


def test_read_non_utf8_file_is_an_error_tool_result(backend, tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — it used to escape
    dispatch_tool and end the run with an exception."""
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00binary")
    backend.responses.append(tool_use_response("read", {"path": "blob.bin"}))
    backend.responses.append(text_response("that file is not text"))

    r = run(backend, tmp_path)
    assert (r.meta["outcome"], r.meta["num_turns"]) == ("done", 2)
    [tool_result] = backend.requests[1]["messages"][-1]["content"]
    assert tool_result["is_error"] is True
    assert "UnicodeDecodeError" in tool_result["content"]
    # A decode failure is not a model-side call defect.
    assert r.meta["malformed_tool_calls"] == 0


def test_run_with_non_utf8_output_is_an_error_tool_result(backend, tmp_path):
    backend.responses.append(
        tool_use_response("run", {"command": r"printf '\xff\xfe'"})
    )
    backend.responses.append(text_response("that command emitted binary"))

    r = run(backend, tmp_path)
    assert (r.meta["outcome"], r.meta["num_turns"]) == ("done", 2)
    [tool_result] = backend.requests[1]["messages"][-1]["content"]
    assert tool_result["is_error"] is True
    assert "UnicodeDecodeError" in tool_result["content"]
    assert r.meta["malformed_tool_calls"] == 0


def test_write_non_string_content_is_an_error_tool_result(backend, tmp_path):
    backend.responses.append(
        tool_use_response("write", {"path": "out.txt", "content": 5})
    )
    backend.responses.append(text_response("recovered"))
    r = run(backend, tmp_path)
    assert r.meta["outcome"] == "done"
    [tool_result] = backend.requests[1]["messages"][-1]["content"]
    assert tool_result["is_error"] is True


def test_dispatch_tool_maps_decode_error_without_raising(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe")
    content, is_error, is_malformed = agcode.dispatch_tool(
        tmp_path, "read", {"path": "blob.bin"}, agcode.tool_table(agcode.DEFAULT_TOOLS)
    )
    assert (is_error, is_malformed) == (True, False)
    assert "UnicodeDecodeError" in content


def test_unknown_exception_hits_the_backstop(backend, tmp_path, monkeypatch):
    """The last-resort ``except`` around the loop: an unforeseen failure ends
    as malformed_response carrying the exception repr, never as a raise."""
    backend.responses.append(text_response("all done"))
    monkeypatch.setattr(
        agcode, "response_text", lambda resp: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    r = run(backend, tmp_path)
    assert_failure(r, "failed", "malformed_response")
    assert "RuntimeError('boom')" in r.meta["failure"]
    assert r.meta["duration_ms"] >= 0


# --- Truncation (stop_reason: max_tokens) ------------------------------------


def test_a_cut_off_turn_is_nudged_and_the_run_continues(backend, tmp_path):
    """A max_tokens stop means the model was mid-action, not finished. Ending
    there reads a cut-off preamble as the final answer and throws the turn
    away — what a local model's long thinking plus one whole-file write does
    to the response cap."""
    backend.responses.append(text_response("Now let me create the core:", stop_reason="max_tokens"))
    backend.responses.append(text_response("the whole answer"))

    r = run(backend, tmp_path)

    assert (r.meta["outcome"], r.output) == ("done", "the whole answer")
    assert r.meta["num_turns"] == 2
    # The flag stays for the whole run, not just its last response.
    assert r.meta["truncated"] is True
    [nudge] = backend.requests[1]["messages"][-1]["content"]
    assert nudge["type"] == "text" and "cut off" in nudge["text"]


def test_a_cut_off_tool_call_is_answered_as_an_error_never_executed(backend, tmp_path):
    """A tool call cut off at the limit has incomplete arguments: executing it
    would write half a file. It is answered as an error so the model can retry
    smaller, and the loop goes on."""
    backend.responses.append(
        tool_use_response("write", {"path": "big.ts", "content": "half a fi"},
                          stop_reason="max_tokens")
    )
    backend.responses.append(text_response("wrote it in pieces instead"))

    r = run(backend, tmp_path)

    assert (r.meta["outcome"], r.output) == ("done", "wrote it in pieces instead")
    assert not (tmp_path / "big.ts").exists()
    [result] = backend.requests[1]["messages"][-1]["content"]
    assert result["tool_use_id"] == "tu_1"
    assert result["is_error"] is True
    assert "NOT executed" in result["content"]


def test_untruncated_run_says_so(backend, tmp_path):
    backend.responses.append(text_response("whole answer"))
    r = run(backend, tmp_path)
    assert r.meta["truncated"] is False


def test_a_run_that_only_ever_gets_cut_off_ends_on_its_turn_budget(backend, tmp_path):
    """The nudge loop is bounded by the ordinary budgets, not by a special
    case: a backend that truncates forever exhausts the turns and aborts."""
    for _ in range(3):
        backend.responses.append(text_response("", stop_reason="max_tokens"))

    r = run(backend, tmp_path, max_turns=2)

    assert_failure(r, "aborted", "turn_budget_exhausted")
    assert (r.meta["truncated"], r.meta["empty_final"]) == (True, True)


# --- Content-handing mode ----------------------------------------------------


def test_task_input_becomes_second_text_block(backend, tmp_path):
    backend.responses.append(text_response("answer"))
    r = run(backend, tmp_path, task_input="line 1\nline 2\n")
    assert r.meta["outcome"] == "done"
    [req] = backend.requests
    assert req["messages"][0] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "the task"},
            {"type": "text", "text": "line 1\nline 2\n"},
        ],
    }


def test_without_task_input_message_shape_unchanged(backend, tmp_path):
    backend.responses.append(text_response("answer"))
    run(backend, tmp_path)
    [req] = backend.requests
    assert req["messages"][0] == {"role": "user", "content": "the task"}


def test_content_mode_keeps_tools_available(backend, tmp_path):
    (tmp_path / "extra.txt").write_text("extra")
    backend.responses.append(tool_use_response("read", {"path": "extra.txt"}))
    backend.responses.append(text_response("done"))
    r = run(backend, tmp_path, task_input="inline input")
    assert (r.meta["outcome"], r.meta["num_turns"]) == ("done", 2)
    assert backend.requests[0]["tools"] == agcode.TOOLS_V0


# --- Prompt audit ------------------------------------------------------------

PINNED_TEMPLATE = """\
You are agcode, a coding agent.

Working directory: {working_dir}

Complete the user's task using the provided tools. File paths passed to tools
are relative to the working directory; the tools resolve them — never convert
a path yourself. When the task is complete, reply with the final answer as
plain text."""


def test_template_pinned_byte_for_byte():
    assert agcode.SYSTEM_PROMPT == PINNED_TEMPLATE
    # One screen: ≤ 30 lines and ≤ 2000 chars.
    assert len(agcode.SYSTEM_PROMPT.splitlines()) <= 30
    assert len(agcode.SYSTEM_PROMPT) <= 2000
    fields = [f for _, f, _, _ in string.Formatter().parse(agcode.SYSTEM_PROMPT) if f]
    assert fields == ["working_dir"]


def test_rendered_prompt_is_template_plus_working_dir_only(backend, tmp_path):
    backend.responses.append(text_response("ok"))
    run(backend, tmp_path)
    [req] = backend.requests
    assert req["system"] == PINNED_TEMPLATE.replace("{working_dir}", str(tmp_path.resolve()))


def test_no_ambient_reads_in_module():
    """The only os.environ value reads are the two documented knobs; the
    remaining os.environ use is tool_run passing the environment through.
    Nothing resolves the invoking user's home."""
    source = Path(agcode.__file__).read_text()
    assert re.findall(r'os\.environ\.get\(\s*"([^"]+)"', source) == [
        "AGCODE_BASE_URL",
        "AGCODE_MODEL",
    ]
    assert len(re.findall(r"os\.environ", source)) == 3  # 2 gets + tool_run passthrough
    for forbidden in ("expanduser", "Path.home", "os.getenv", "~"):
        assert forbidden not in source, forbidden


def test_base_prompt_names_exactly_one_directory():
    """Prompt audit: the base system prompt names one directory and nothing
    about the operator — no host, user, project or path outside the single
    ``{working_dir}`` placeholder."""
    rendered = agcode.compose_system("/base/one")
    assert rendered.count("/base/one") == 1
    # Every path-looking token in the rendered prompt is that one directory.
    assert re.findall(r"(?<![\w.])/[\w./-]+", rendered) == ["/base/one"]
    for operator_ish in ("agstudio", "eiji", "localhost", "http", "Users", "home"):
        assert operator_ish not in agcode.SYSTEM_PROMPT, operator_ish


# --- Tool seam ---------------------------------------------------------------


def test_default_tools_are_the_v0_four_in_order():
    assert [t.name for t in agcode.DEFAULT_TOOLS] == [s["name"] for s in agcode.TOOLS_V0]
    assert [t.spec for t in agcode.DEFAULT_TOOLS] == agcode.TOOLS_V0


def test_readonly_preset_offers_no_writing_tool(backend, tmp_path):
    """Permission is the tool set: the write and run tools are not offered at
    all, so there is no denied call for a weak model to attempt."""
    assert [t.name for t in agcode.READONLY_TOOLS] == ["read", "list"]
    backend.responses.append(text_response("ok"))
    run(backend, tmp_path, tools=agcode.READONLY_TOOLS)
    [req] = backend.requests
    assert [t["name"] for t in req["tools"]] == ["read", "list"]


def test_readonly_preset_reports_write_as_unknown(tmp_path):
    content, is_error, is_malformed = agcode.dispatch_tool(
        tmp_path, "write", {"path": "x", "content": "y"},
        agcode.tool_table(agcode.READONLY_TOOLS),
    )
    assert (is_error, is_malformed) == (True, True)
    assert "unknown tool" in content and "read, list" in content
    assert not (tmp_path / "x").exists()


def test_custom_tool_is_offered_and_dispatched(backend, tmp_path):
    """A caller registers its own tool in-process: spec on the wire, callable
    in the loop. ``base`` arrives first even for a tool that ignores it."""
    calls = []

    def fetch(base, url):
        calls.append((base, url))
        return "fetched " + url

    spec = {
        "name": "fetch",
        "description": "Fetch a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    }
    backend.responses.append(tool_use_response("fetch", {"url": "http://x/"}))
    backend.responses.append(text_response("done"))
    r = run(backend, tmp_path, tools=[agcode.Tool(spec, fetch)])

    assert r.meta["outcome"] == "done"
    assert calls == [(tmp_path.resolve(), "http://x/")]
    assert backend.requests[0]["tools"] == [spec]
    [tool_result] = backend.requests[1]["messages"][-1]["content"]
    assert tool_result["content"] == "fetched http://x/"


def test_empty_tool_set_offers_nothing(backend, tmp_path):
    backend.responses.append(text_response("ok"))
    r = run(backend, tmp_path, tools=[])
    assert r.meta["outcome"] == "done"
    assert backend.requests[0]["tools"] == []


def test_duplicate_tool_names_are_rejected():
    with pytest.raises(ValueError, match="duplicate tool name"):
        agcode.tool_table([*agcode.DEFAULT_TOOLS, agcode.DEFAULT_TOOLS[0]])


# --- system_suffix -----------------------------------------------------------


def test_system_suffix_follows_the_pinned_template(backend, tmp_path):
    backend.responses.append(text_response("ok"))
    run(backend, tmp_path, system_suffix="You are the window door.\nBe brief.")
    [req] = backend.requests
    base = PINNED_TEMPLATE.replace("{working_dir}", str(tmp_path.resolve()))
    assert req["system"] == base + "\n\nYou are the window door.\nBe brief."


def test_blank_system_suffix_changes_nothing(backend, tmp_path):
    backend.responses.append(text_response("ok"))
    run(backend, tmp_path, system_suffix="   \n")
    [req] = backend.requests
    assert req["system"] == PINNED_TEMPLATE.replace("{working_dir}", str(tmp_path.resolve()))


def test_working_directory_sentence_stays_first(tmp_path):
    """A suffix cannot displace the one sentence the whole base rule rests on."""
    system = agcode.compose_system("/base/one", "Working directory: /somewhere/else")
    assert system.index("/base/one") < system.index("/somewhere/else")
    assert system.startswith("You are agcode, a coding agent.")


# --- Cancellation ------------------------------------------------------------


def test_stop_before_first_turn_aborts_without_calling_the_backend(backend, tmp_path):
    r = run(backend, tmp_path, stop=lambda: True)
    assert_failure(r, "aborted", "cancelled")
    assert (r.meta["num_turns"], backend.requests) == (0, [])


def test_stop_between_turns_ends_the_run(backend, tmp_path):
    """The check is between turns, so an in-flight turn always completes and
    its usage is kept."""
    backend.responses.append(tool_use_response("list", {}))
    backend.responses.append(text_response("never reached"))
    turns = []
    r = run(backend, tmp_path, stop=lambda: bool(turns) or turns.append(1))

    assert_failure(r, "aborted", "cancelled")
    assert r.meta["num_turns"] == 1
    assert r.meta["usage"] == {"input_tokens": 20, "output_tokens": 15}
    assert r.output == ""


def test_stop_that_stays_false_is_inert(backend, tmp_path):
    backend.responses.append(text_response("all done"))
    r = run(backend, tmp_path, stop=lambda: False)
    assert (r.meta["outcome"], r.output) == ("done", "all done")


# --- Record conformance (ag.agent-run.v1 §9) ---------------------------------

# The §9 record fields pyagag's write_run_record() copies from meta;
# spelling is the interface.
RECORD_KEYS = (
    "role", "profile", "harness", "provider", "model", "duration_ms",
    "cost_usd", "usage", "num_turns", "transcript",
)

# What a pyagag-style caller knows and agcode must not: resolved-profile
# identity, with the full canonical provider/name model ID.
IDENTITY = {
    "role": "front",
    "profile": "local",
    "harness": "agcode",
    "provider": "ollama",
    "model": "ollama/qwen3.6:35b-a3b-coding-nvfp4",
}


def record_from(meta: dict) -> dict:
    """The caller-side record write, as a merge + fixed-key copy."""
    merged = {**IDENTITY, **meta}
    record = {k: merged[k] for k in RECORD_KEYS if k in merged}
    record["outcome"] = merged["outcome"]
    if merged.get("failure"):
        record["failure"] = merged["failure"]
    return record


def test_done_run_record_is_a_plain_merge(backend, tmp_path):
    """A done run's meta + identity yields exactly the §9 fields, minus
    cost_usd (backend reports none; invented is worse than missing)."""
    (tmp_path / "wd").mkdir()
    backend.responses.append(tool_use_response("list", {}))
    backend.responses.append(text_response("all done"))
    r = run(backend, tmp_path / "wd", transcript_path=str(tmp_path / "t.jsonl"))

    record = record_from(r.meta)
    assert record == {
        **IDENTITY,
        "duration_ms": r.meta["duration_ms"],
        "usage": {"input_tokens": 30, "output_tokens": 20},
        "num_turns": 2,
        "transcript": str(tmp_path / "t.jsonl"),
        "outcome": "done",
    }
    assert "cost_usd" not in record


def test_meta_never_carries_identity_or_cost(backend, tmp_path):
    """Identity fields are caller-side: a collision would silently overwrite
    e.g. the canonical model ID with the provider-native spelling."""
    backend.responses.append(text_response("done"))
    r = run(backend, tmp_path)
    assert not frozenset(IDENTITY) & r.meta.keys()
    assert "cost_usd" not in r.meta


def test_failed_run_failure_is_kind_prefixed(backend, tmp_path):
    """failure_kind has no §9 field of its own; it survives into records as
    a machine-readable prefix of the failure string."""
    backend.responses.append({"id": "msg", "stop_reason": "end_turn"})  # no content key
    r = run(backend, tmp_path)

    record = record_from(r.meta)
    assert record["outcome"] == "failed"
    kind, _, detail = record["failure"].partition(": ")
    assert kind == r.meta["failure_kind"] == "malformed_response"
    assert kind in agcode.FAILURE_KINDS and detail
    # The separate key stays for in-process callers but is not a §9 field.
    assert "failure_kind" not in record


# --- CLI wire contract (python -m agcode) ------------------------------------


def run_cli(backend, *args, task="the task", cwd=None, env_extra=None):
    """Run the module the way run_harness() drives a harness: prompt on
    stdin, cwd= plus env["PWD"] pointing at the same directory."""
    cwd = Path(cwd) if cwd else PKG_ROOT
    env = {
        **os.environ,
        "PYTHONPATH": str(PKG_ROOT),
        "PWD": str(cwd.resolve()),
        **(env_extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", MODULE, "--base-url", backend.url, "--model", "fake-model", *args],
        input=task,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def extract_cli(proc):
    """The future caller-side ``_extract_agcode`` in its entirety: one
    json.loads plus a fixed-key meta copy."""
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout, {}
    if not isinstance(doc, dict):
        return proc.stdout, {}
    keys = ("duration_ms", "num_turns", "usage", "outcome", "failure", "transcript")
    return doc.get("output", ""), {k: doc[k] for k in keys if k in doc}


def test_cli_done_run(backend, tmp_path):
    backend.responses.append(text_response("all done"))
    proc = run_cli(backend, "--working-dir", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    output, meta = extract_cli(proc)
    assert output == "all done"
    assert (meta["outcome"], meta["num_turns"]) == ("done", 1)
    assert proc.stdout.count("\n") == 1  # exactly one JSON document


def test_cli_defaults_are_the_four_tools_and_no_suffix(backend, tmp_path):
    """The tool seam is library-side only: the CLI's wire payload is exactly
    what it was before the seam existed."""
    backend.responses.append(text_response("all done"))
    proc = run_cli(backend, "--working-dir", str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    [req] = backend.requests
    assert req["tools"] == agcode.TOOLS_V0
    assert req["system"] == PINNED_TEMPLATE.replace("{working_dir}", str(tmp_path.resolve()))


def test_cli_read_only_preset(backend, tmp_path):
    backend.responses.append(text_response("all done"))
    proc = run_cli(backend, "--working-dir", str(tmp_path), "--tools", "read-only")
    assert proc.returncode == 0, proc.stderr
    [req] = backend.requests
    assert [t["name"] for t in req["tools"]] == ["read", "list"]


def test_cli_rejects_unknown_tool_preset(backend, tmp_path):
    proc = run_cli(backend, "--working-dir", str(tmp_path), "--tools", "everything")
    assert proc.returncode == 2
    assert backend.requests == []


def test_cli_failed_run_exit_1(backend, tmp_path):
    backend.responses.append({"id": "msg", "stop_reason": "end_turn"})  # no content key
    proc = run_cli(backend, "--working-dir", str(tmp_path))
    assert proc.returncode == 1
    _, meta = extract_cli(proc)
    assert meta["outcome"] == "failed"
    assert meta["failure"].startswith("malformed_response: ")


def test_cli_empty_final_run_exit_0(backend, tmp_path):
    """A run that ends without a closing message is done: exit 0, empty
    output, and the fact reported for the caller to weigh."""
    backend.responses.append(text_response(""))
    proc = run_cli(backend, "--working-dir", str(tmp_path))
    assert proc.returncode == 0
    doc = json.loads(proc.stdout)
    assert (doc["output"], doc["outcome"], doc["empty_final"]) == ("", "done", True)
    assert "failure" not in doc


def test_cli_aborted_run_exit_2(backend, tmp_path):
    backend.responses.append(tool_use_response("list", {}))
    proc = run_cli(backend, "--working-dir", str(tmp_path), "--max-turns", "1")
    assert proc.returncode == 2
    _, meta = extract_cli(proc)
    assert meta["outcome"] == "aborted"
    assert meta["failure"].startswith("turn_budget_exhausted: ")


def test_cli_run_harness_style_drive(backend, tmp_path):
    """cwd (and PWD) set to the working directory, no --working-dir flag —
    the default '.' resolves there — and a tool reads a file relative to it."""
    (tmp_path / "note.txt").write_text("marker-777")
    backend.responses.append(tool_use_response("read", {"path": "note.txt"}))
    backend.responses.append(text_response("marker-777"))
    tpath = tmp_path / "t" / "run.jsonl"

    proc = run_cli(backend, "--transcript", str(tpath), cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    output, meta = extract_cli(proc)
    assert output == "marker-777"
    assert (meta["num_turns"], meta["transcript"]) == (2, str(tpath))
    [tool_result] = backend.requests[1]["messages"][-1]["content"]
    assert tool_result["content"] == "marker-777"
    assert str(tmp_path.resolve()) in backend.requests[0]["system"]


def test_cli_task_input_file_content_mode(backend, tmp_path):
    payload = tmp_path / "input.md"
    payload.write_text("marker: WD-CLI-1")
    backend.responses.append(text_response("marker: WD-CLI-1"))

    proc = run_cli(
        backend,
        "--working-dir", str(tmp_path),
        "--task-input-file", str(payload),
        task="reply with only the marker line",
    )
    assert proc.returncode == 0, proc.stderr
    [first] = backend.requests
    assert first["messages"][0]["content"] == [
        {"type": "text", "text": "reply with only the marker line"},
        {"type": "text", "text": "marker: WD-CLI-1"},
    ]


def test_cli_empty_stdin_is_usage_error(backend, tmp_path):
    proc = run_cli(backend, "--working-dir", str(tmp_path), task="")
    assert proc.returncode not in (0, 1)  # argparse usage error, not a run
    assert "no task on stdin" in proc.stderr
    assert backend.requests == []


def test_cli_usage_error_leaves_stdout_json_free(backend, tmp_path):
    """Usage errors exit 2 — the same code as an aborted run. The collision
    stays harmless because they are distinguishable at the caller: a usage
    error writes nothing to stdout, so the extractor returns empty meta and
    run_harness()'s nonzero->failed default applies, while a real aborted run
    always carries outcome=aborted in its JSON."""
    proc = run_cli(backend, "--nonsense-flag")
    assert proc.returncode == 2
    assert proc.stdout == ""
    output, meta = extract_cli(proc)
    assert (output, meta) == ("", {})

    backend.responses.append(tool_use_response("list", {}))
    aborted = run_cli(backend, "--working-dir", str(tmp_path), "--max-turns", "1")
    assert aborted.returncode == 2
    _, aborted_meta = extract_cli(aborted)
    assert aborted_meta["outcome"] == "aborted"


def test_cli_rejects_both_task_input_flags(backend, tmp_path):
    """Pinned: giving both is a usage error, not a silent precedence rule."""
    payload = tmp_path / "input.md"
    payload.write_text("from the file")
    proc = run_cli(
        backend,
        "--working-dir", str(tmp_path),
        "--task-input", "inline",
        "--task-input-file", str(payload),
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "not allowed with argument" in proc.stderr
    assert backend.requests == []


def test_cli_large_task_on_stdin_round_trips(backend, tmp_path):
    """Hundreds of KB of task text survive the stdin hand-off byte-for-byte
    (no pipe truncation, no encoding damage)."""
    big = "".join(f"line {i}: {'x' * 80} ünïcode ✓\n" for i in range(4000))
    assert len(big.encode()) > 300_000
    backend.responses.append(text_response("read it all"))

    proc = run_cli(backend, "--working-dir", str(tmp_path), task=big)
    assert proc.returncode == 0, proc.stderr[-2000:]
    [req] = backend.requests
    assert req["messages"][0]["content"] == big
