"""Ported deterministic protocol and failure tests for the process seam."""

import json
import stat
import textwrap
from pathlib import Path

import pytest

from agag.agent_config import ResolvedAgent
from agag.harness import build_argv, run_harness, write_run_record


def stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def agent(command: Path, harness: str, environment: dict[str, str] | None = None) -> ResolvedAgent:
    model = (
        "anthropic/claude-sonnet-5"
        if harness == "claude_code"
        else "ollama/qwen3.6:35b-a3b-coding-nvfp4"
    )
    return ResolvedAgent(
        "coding", "test-profile", harness, model.split("/", 1)[0], model, {},
        str(command), "http://127.0.0.1:11434", environment or {},
    )


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


def test_opencode_jsonl_extraction_and_aggregation(tmp_path):
    command = stub(tmp_path / "opencode", '''
        import json, sys
        sys.stdin.read()
        print(json.dumps({"type": "text", "part": {"text": "worked"}}))
        print(json.dumps({"type": "step_finish", "part": {"cost": 0.02,
              "tokens": {"input": 3, "output": 4, "reasoning": 1,
                         "cache": {"read": 2, "write": 1}}}}))
    ''')
    result = run_harness(agent(command, "opencode"), "prompt", cwd=tmp_path, timeout=5)
    assert result.output == "worked"
    assert result.meta["cost_usd"] == 0.02
    assert result.meta["num_turns"] == 1
    assert result.meta["usage"] == {
        "input": 3, "output": 4, "reasoning": 1, "cache_read": 2, "cache_write": 1,
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
    opencode = build_argv(agent(command, "opencode"))
    assert claude[claude.index("--model") + 1] == "claude-sonnet-5"
    assert opencode[opencode.index("-m") + 1].startswith("ollama/")
    with pytest.raises(ValueError, match="resolved profile"):
        build_argv(agent(command, "opencode"), extra_args=["--model", "wrong"])


def test_raw_output_tail_and_run_record(tmp_path):
    command = stub(tmp_path / "opencode", "print('0123456789'); raise SystemExit(2)\n")
    transcript = tmp_path / "raw.jsonl"
    result = run_harness(
        agent(command, "opencode"), "p", cwd=tmp_path, timeout=5,
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
    assert record["harness"] == "opencode"
