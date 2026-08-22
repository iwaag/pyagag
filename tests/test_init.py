"""`agag init`: small output, a valid config, a listener that imports."""

import argparse
import importlib
import sys
from pathlib import Path

import pytest

from agag import init
from agag.agent_config import load_config, resolve_role
from agag.cli import main


def args(agent="agecho", **kw) -> argparse.Namespace:
    base = dict(agent=agent, instance=None, plan_prefix=None, run_prefix=None,
                roles=None, profile=None, dest=None, yes=True, no_git=True)
    base.update(kw)
    return argparse.Namespace(**base)


def test_defaults_follow_the_agent_name(tmp_path):
    plan = init.plan_from_args(args(dest=str(tmp_path)))
    assert plan.agent == "agecho"
    assert plan.instance.startswith("agecho-") and plan.instance.endswith("1")
    assert (plan.plan_prefix, plan.run_prefix) == ("agechoplan-", "agechorun-")
    assert plan.roles == ("front",)
    assert plan.root == tmp_path / "agecho"


def test_a_bad_name_or_a_missing_front_is_refused(tmp_path):
    with pytest.raises(init.InitError):
        init.plan_from_args(args(agent="Ag-Echo", dest=str(tmp_path)))
    with pytest.raises(init.InitError):
        init.plan_from_args(args(roles="worker", dest=str(tmp_path)))


def test_generated_project_is_small_and_loads(tmp_path, monkeypatch):
    plan = init.plan_from_args(args(dest=str(tmp_path), instance="agecho-lab1", roles="front,worker"))
    root = init.generate(plan)
    written = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert written == [
        ".gitignore", ".local/instance.toml", "agent/guides/agechoplan_front/guide.md",
        "agents.toml", "instance.example.toml", "params/intro.md", "pyproject.toml",
        "service/listen.sh", "src/agecho/__init__.py", "src/agecho/intro.py",
        "src/agecho/listener.py",
    ]
    # The listener is the only code, and it is short.
    listener = (root / "src/agecho/listener.py").read_text(encoding="utf-8")
    assert len([l for l in listener.splitlines() if l.strip() and not l.startswith(("#", '"'))]) <= 15
    # The config is a valid v2 with a grant per role.
    config, overlay = load_config(root / "agents.toml")
    for role in ("front", "worker"):
        assert resolve_role(config, overlay, role, profile_override="stub", check_available=False).allowed_tools
    # No template variable survived.
    for path in root.rglob("*"):
        if path.is_file():
            assert "$" not in path.read_text(encoding="utf-8") or path.name == "listen.sh"
    assert (root / ".local/instance.toml").read_text() == 'name = "agecho-lab1"\n'
    assert "{instance}" in (root / "params/intro.md").read_text()
    # The listener imports and its spec points at the project root.
    monkeypatch.syspath_prepend(str(root / "src"))
    module = importlib.import_module("agecho.listener")
    assert module.SPEC.root == root
    assert module.SPEC.instance_name() == "agecho-lab1"
    assert module.SPEC.plan_prefix == "agechoplan-"


def test_generate_refuses_an_existing_root(tmp_path):
    plan = init.plan_from_args(args(dest=str(tmp_path)))
    (tmp_path / "agecho").mkdir()
    with pytest.raises(init.InitError):
        init.generate(plan)


def test_cli_generates_and_prints_the_checklist_without_touching_git(tmp_path, capsys):
    code = main(["init", "agecho", "--yes", "--dest", str(tmp_path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "Human checklist" in out and ".local/zulip.env" in out and "#agents" in out
    root = tmp_path / "agecho"
    assert not (root / ".git").exists()  # version control is the caller's
    assert (root / "agents.toml").is_file()
    assert ".local/" in (root / ".gitignore").read_text()
