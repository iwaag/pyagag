"""agent_guide p2 ex1: the trial kit every agent's driver shares."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agag import agent, topics
from agag.agent import RECORDS_ROOT_VARIABLE, AgentSpec, run_role
from agag.fixture.board import BOARD_VERSION
from agag.fixture.run import (DRY_REPLY, Trial, TrialError, guides_at, newest, record_facts, tool_calls,
                              trial_parser)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    repository = tmp_path / "repo"
    guide = repository / "agent" / "guides" / "desk" / "guide.md"
    guide.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repository))
    _git(repository, "config", "user.email", "t@example.invalid")
    _git(repository, "config", "user.name", "t")
    guide.write_text("old guide\n", encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "old")
    guide.write_text("new guide\n", encoding="utf-8")
    _git(repository, "commit", "-qam", "new")
    return repository


def test_guides_at_a_revision_are_the_tree_of_that_commit(repository, tmp_path):
    tree = guides_at(repository, "HEAD~1", tmp_path / "old")
    assert (tree / "desk" / "guide.md").read_text(encoding="utf-8") == "old guide\n"
    assert (tmp_path / "old" / "REVISION").read_text(encoding="utf-8").startswith("HEAD~1 ")
    with pytest.raises(TrialError):
        guides_at(repository, "no-such-rev", tmp_path / "x")


def test_the_parser_offers_only_the_agents_probes():
    parser = trial_parser("t", "", "agautolab")
    assert parser.parse_args(["entrance-plans", "--out", "o"]).probe == "entrance-plans"
    with pytest.raises(SystemExit):
        parser.parse_args(["growbox-thing", "--out", "o"])
    with pytest.raises(SystemExit):
        parser.parse_args(["entrance-plans", "--out", "o", "--guides", "g", "--guides-rev", "HEAD"])


def test_a_trial_builds_its_board_and_keeps_records_under_out(repository, tmp_path, monkeypatch):
    monkeypatch.delenv(RECORDS_ROOT_VARIABLE, raising=False)
    args = trial_parser("t", "", "agfront").parse_args(["growbox-thing", "--out", str(tmp_path / "out"),
                                                        "--guides-rev", "HEAD~1", "--no-shared", "--dry-run"])
    trial = Trial.start(args, repository)
    monkeypatch.setenv(RECORDS_ROOT_VARIABLE, str(trial.records))  # restored by monkeypatch
    assert trial.store == tmp_path / "out" / "board" / "mirror.sqlite" and trial.store.is_file()
    assert trial.facts()["board_version"] == BOARD_VERSION  # agent_guide p3 ex1: a result says its board
    assert (trial.guides / "desk" / "guide.md").read_text(encoding="utf-8") == "old guide\n"
    spec = AgentSpec("front", tmp_path / "checkout")
    assert spec.records_root == tmp_path / "out" / "records" / "front" / ".local" / "agent"
    assert spec.topics_root == tmp_path / "out" / "records" / "front" / ".local" / "topics"
    assert spec.zulip_env == tmp_path / "checkout" / ".local" / "zulip.env"  # credentials stay the checkout's


def test_without_the_variable_runs_stay_in_the_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv(RECORDS_ROOT_VARIABLE, raising=False)
    assert AgentSpec("front", tmp_path).records_root == tmp_path / ".local" / "agent"


def test_a_dry_session_runs_no_harness_and_leaves_out_shared_sections(tmp_path, monkeypatch):
    args = trial_parser("t", "", "agfront").parse_args(["growbox-thing", "--out", str(tmp_path / "out"),
                                                        "--no-shared", "--dry-run"])
    trial = Trial.start(args, tmp_path)
    monkeypatch.setenv(RECORDS_ROOT_VARIABLE, str(trial.records))
    spec = AgentSpec("front", tmp_path / "checkout")
    monkeypatch.setattr(agent, "resolve_spec_role", lambda *a, **k: SimpleNamespace(allowed_tools=()))
    with trial.session():
        assert topics.prompt_with_guide(["x"], "guide", shared=("board",)) == "x\n\nguide"
        output, _, code = run_role(spec, "desk", "the prompt", cwd=tmp_path, timeout=1,
                                   record=topics.next_record_path(spec.records_root / "desk"))
    assert (output, code) == (DRY_REPLY, 0)
    assert (trial.out / "prompt.md").read_text(encoding="utf-8") == "the prompt"
    assert "board" in topics.prompt_with_guide(["x"], "guide", shared=("board",)).casefold()
    assert newest(spec.records_root / "desk", "run-*.json").parent == trial.records / "front" / ".local" / "agent" / "desk"
    assert record_facts(spec.records_root / "desk")["record"].endswith("run-0001.json")


def test_tool_calls_come_from_the_session_log(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text("\n".join(json.dumps(e) for e in (
        {"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "agentchat intro"}}]}},
        {"message": {"content": "text only"}},
        {"type": "result"},
    )) + "\nnot json\n", encoding="utf-8")
    assert tool_calls(log) == ["Bash: agentchat intro"]
    assert tool_calls(None) == [] and tool_calls(tmp_path / "missing") == []


def test_shared_guides_from_a_directory_replace_the_packages(tmp_path, monkeypatch):
    """agent_guide p3: a change to pyagag's shared sections is tried before it is released."""
    tree = tmp_path / "shared"
    tree.mkdir()
    for name in topics.SHARED_SECTIONS:
        (tree / f"{name}.md").write_text(f"# {name} as changed\n", encoding="utf-8")
    parser = trial_parser("t", "", "agfront")
    with pytest.raises(SystemExit):
        parser.parse_args(["growbox-thing", "--out", "o", "--no-shared", "--shared-guides", str(tree)])
    args = parser.parse_args(["growbox-thing", "--out", str(tmp_path / "out"), "--shared-guides", str(tree),
                              "--dry-run"])
    trial = Trial.start(args, tmp_path)
    monkeypatch.setenv(RECORDS_ROOT_VARIABLE, str(trial.records))
    with trial.session():
        assert topics.shared_sections(("board", "refs")) == "# board as changed\n\n# refs as changed"
        (tree / "refs.md").write_text("", encoding="utf-8")
        with pytest.raises(topics.GuideError):
            topics.shared_sections(("refs",))
    assert "as changed" not in topics.shared_sections(("board",))
    assert trial.facts()["shared_guides"] == str(tree)
