"""Ported deterministic protocol and failure tests for the process seam."""

import json
import stat
import sys
import textwrap
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agag.agent_config import ResolvedAgent
from agag.harness import _extract_agcode, build_argv, run_harness, write_run_record


def stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def agent(
    command: Path,
    harness: str,
    environment: dict[str, str] | None = None,
    base_url: str = "http://127.0.0.1:11434",
) -> ResolvedAgent:
    model = (
        "anthropic/claude-sonnet-5"
        if harness == "claude_code"
        else "ollama/qwen3.6:35b-a3b-coding-nvfp4"
    )
    return ResolvedAgent(
        "coding", "test-profile", harness, model.split("/", 1)[0], model, {},
        str(command), base_url, environment or {},
    )


@pytest.fixture
def messages_backend():
    """A stub Anthropic Messages endpoint, so the agcode tests drive the real
    module through the real process seam without a model behind it."""

    class Backend:
        text = "the answer"
        delay_s = 0.0

    backend = Backend()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            time.sleep(backend.delay_s)
            body = json.dumps({
                "content": [{"type": "text", "text": backend.text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 11, "output_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    backend.url = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield backend
    server.shutdown()
    server.server_close()


def test_claude_json_extraction_and_identity(tmp_path):
    command = stub(tmp_path / "claude", '''
        import json, sys
        sys.stdin.read()
        print(json.dumps({"result": "done", "is_error": False, "duration_ms": 12,
                          "num_turns": 3, "total_cost_usd": 0.04,
                          "usage": {"input_tokens": 7}}))
    ''')
    result = run_harness(agent(command, "claude_code"), "prompt", cwd=tmp_path, timeout=5)
    assert result.output == "done"
    assert result.exit_code == 0
    assert result.meta == {
        "role": "coding", "profile": "test-profile", "harness": "claude_code",
        "provider": "anthropic", "model": "anthropic/claude-sonnet-5",
        "duration_ms": 12, "num_turns": 3, "is_error": False,
        "cost_usd": 0.04, "usage": {"input_tokens": 7}, "outcome": "done",
    }


def test_fake_argv_and_environment_injection(tmp_path):
    command = stub(tmp_path / "fake", '''
        import os, sys
        sys.stdin.read()
        print(os.environ["INJECTED"] + " " + os.environ["AGENT_PROVIDER_OLLAMA_BASE_URL"])
    ''')
    resolved = agent(command, "fake", {"INJECTED": "yes"})
    assert build_argv(resolved) == [str(command)]
    result = run_harness(resolved, "prompt", cwd=tmp_path, timeout=5)
    assert result.output == "yes http://127.0.0.1:11434"


def test_run_harness_replaces_stale_pwd_with_cwd(tmp_path, monkeypatch):
    command = stub(tmp_path / "fake", '''
        import os, sys
        sys.stdin.read()
        print(os.getcwd())
        print(os.environ["PWD"])
    ''')
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("PWD", str(tmp_path / "stale-parent"))

    result = run_harness(agent(command, "fake"), "prompt", cwd=target, timeout=5)

    assert result.output.splitlines() == [str(target), str(target)]


@pytest.mark.parametrize("kind", ["launch", "timeout", "empty", "is_error"])
def test_failure_paths_are_normalized(tmp_path, kind):
    if kind == "launch":
        command = tmp_path / "missing"
    elif kind == "timeout":
        command = stub(tmp_path / "claude", "import time; time.sleep(5)\n")
    elif kind == "empty":
        command = stub(tmp_path / "claude", "pass\n")
    else:
        command = stub(tmp_path / "claude", '''
            import json
            print(json.dumps({"result": "refused", "is_error": True,
                              "subtype": "permission"}))
        ''')
    result = run_harness(
        agent(command, "claude_code"), "p", cwd=tmp_path,
        timeout=0.05 if kind == "timeout" else 5,
    )
    assert result.exit_code != 0
    assert result.meta["outcome"] in {"failed", "aborted"}
    assert result.meta["failure"]


def test_model_argv_mapping_and_smuggling_rejected(tmp_path):
    command = tmp_path / "agent"
    claude = build_argv(agent(command, "claude_code"))
    assert claude[claude.index("--model") + 1] == "claude-sonnet-5"
    with pytest.raises(ValueError, match="resolved profile"):
        build_argv(agent(command, "claude_code"), extra_args=["--model", "wrong"])


def test_an_unsupported_harness_is_rejected(tmp_path):
    """The harness set is closed: build_argv names the four it drives and
    refuses anything else, rather than guessing an argv shape."""
    with pytest.raises(ValueError, match="unsupported harness"):
        build_argv(agent(tmp_path / "agent", "opencode"))


def test_raw_output_tail_and_run_record(tmp_path):
    command = stub(tmp_path / "fake", "print('0123456789'); raise SystemExit(2)\n")
    transcript = tmp_path / "raw.jsonl"
    result = run_harness(
        agent(command, "fake"), "p", cwd=tmp_path, timeout=5,
        transcript_path=transcript, output_tail_chars=4,
    )
    assert result.meta["outcome"] == "failed"
    assert "6789" in result.meta["failure"]
    assert transcript.read_text(encoding="utf-8") == "0123456789\n"

    record_path = tmp_path / "record.json"
    write_run_record(record_path, request_id="request-1", meta=result.meta)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["schema"] == "ag.agent-run.v1"
    assert record["request_id"] == "request-1"
    assert record["outcome"] == "failed"
    assert record["harness"] == "fake"


def test_agcode_argv_shape_and_model_smuggling_rejected(tmp_path):
    resolved = agent(Path(sys.executable), "agcode")
    assert build_argv(resolved, extra_args=["--max-turns", "3"]) == [
        sys.executable, "-m", "agag.agcode",
        "--model", "qwen3.6:35b-a3b-coding-nvfp4",
        "--base-url", "http://127.0.0.1:11434",
        "--max-turns", "3",
    ]
    # The canonical ID stays in records; only the wire name is stripped.
    assert resolved.model == "ollama/qwen3.6:35b-a3b-coding-nvfp4"
    # No endpoint resolved: agcode falls back to its own default.
    without = agent(Path(sys.executable), "agcode", base_url=None)
    assert "--base-url" not in build_argv(without)
    with pytest.raises(ValueError, match="resolved profile"):
        build_argv(resolved, extra_args=["--model", "wrong"])


def test_agcode_runs_through_the_process_seam(tmp_path, messages_backend):
    messages_backend.text = "marker: WD-42"
    transcript = tmp_path / "wire.jsonl"
    resolved = agent(Path(sys.executable), "agcode", base_url=messages_backend.url)

    result = run_harness(
        resolved, "read marker.txt and answer with the marker",
        cwd=tmp_path, timeout=30,
        extra_args=["--transcript", str(transcript)],
    )

    assert result.exit_code == 0
    assert result.output == "marker: WD-42"
    assert result.meta["outcome"] == "done"
    assert result.meta["num_turns"] == 1
    assert result.meta["usage"] == {"input_tokens": 11, "output_tokens": 2}
    assert result.meta["transcript"] == str(transcript)
    assert isinstance(result.meta["duration_ms"], int)
    # Identity survives the merge, and nothing outside the record contract
    # (run_id, truncated, malformed_tool_calls, failure_kind) leaks into meta.
    assert result.meta["harness"] == "agcode"
    assert result.meta["model"] == "ollama/qwen3.6:35b-a3b-coding-nvfp4"
    assert not {"run_id", "truncated", "malformed_tool_calls", "failure_kind"} & set(result.meta)

    record_path = tmp_path / "record.json"
    write_run_record(record_path, request_id="request-agcode", meta=result.meta)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["schema"] == "ag.agent-run.v1"
    assert record["harness"] == "agcode"
    assert record["outcome"] == "done"
    assert "failure" not in record
    # The system prompt named the working directory run_harness was given.
    header = json.loads(transcript.read_text(encoding="utf-8").splitlines()[0])
    assert header["working_dir"] == str(tmp_path.resolve())


def test_agcode_deadline_keeps_the_aborted_outcome(tmp_path, messages_backend):
    """agcode ends itself on its own deadline and says so; run_harness would
    otherwise recompute every nonzero exit into "failed". The subprocess
    timeout sits above the deadline so agcode gets to return its document."""
    messages_backend.delay_s = 3.0
    resolved = agent(Path(sys.executable), "agcode", base_url=messages_backend.url)

    result = run_harness(
        resolved, "anything", cwd=tmp_path, timeout=30,
        extra_args=["--deadline-s", "0.5"],
    )

    assert result.exit_code == 2
    assert result.meta["outcome"] == "aborted"
    assert "deadline_exceeded" in result.meta["failure"]


def test_agcode_extractor_tolerates_non_json_stdout():
    assert _extract_agcode("crashed before printing") == ("crashed before printing", {})
    assert _extract_agcode("[1, 2]") == ("[1, 2]", {})
    assert _extract_agcode(json.dumps({"status": "ok"})) == ("", {})
    doc = {
        "output": "text", "status": "ok", "duration_ms": 5, "num_turns": 2,
        "usage": {"input_tokens": 1}, "outcome": "done", "run_id": "abc",
        "truncated": False, "malformed_tool_calls": 0,
    }
    assert _extract_agcode(json.dumps(doc)) == (
        "text",
        {"duration_ms": 5, "num_turns": 2, "usage": {"input_tokens": 1}, "outcome": "done"},
    )
