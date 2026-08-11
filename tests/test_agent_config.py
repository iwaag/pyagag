"""Ported agent-config contract and resolution tests."""

import os
from pathlib import Path

import pytest

from agag.agent_config import AgentConfigError, load_config, resolve_role

BASE = '''schema = "ag.agent-config.v1"
[models."ollama/local-model"]
[models."anthropic/claude-sonnet-5"]
[profiles.local]
harness = "opencode"
model = "ollama/local-model"
[profiles.sonnet]
harness = "claude_code"
model = "anthropic/claude-sonnet-5"
[roles.generator]
profile = "local"
requires = []
[capabilities]
provides = []
'''


def files(tmp_path: Path, body: str = BASE, overlay: str | None = None) -> tuple[Path, Path]:
    main = tmp_path / "agents.toml"
    local = tmp_path / "agents.local.toml"
    main.write_text(body, encoding="utf-8")
    if overlay is not None:
        local.write_text(overlay, encoding="utf-8")
    return main, local


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (BASE.replace('harness = "opencode"', 'harness = "ollama"'), "E_UNKNOWN_HARNESS"),
        (BASE.replace('model = "ollama/local-model"', 'model = "ollama/absent"'), "E_UNKNOWN_MODEL"),
        (BASE.replace('harness = "opencode"', 'harness = "claude_code"', 1), "E_INCOMPATIBLE"),
        (BASE.replace('profile = "local"', 'profile = "absent"'), "E_UNKNOWN_PROFILE"),
        (BASE.replace("requires = []", 'requires = ["ui_actions"]'), "E_CAPABILITY_UNMET"),
    ],
)
def test_invalid_contract_classes(tmp_path, body, code):
    main, local = files(tmp_path, body)
    with pytest.raises(AgentConfigError) as caught:
        config, overlay = load_config(main, local)
        resolve_role(config, overlay, "generator", check_available=False)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("overlay_body", "code"),
    [
        (
            '''schema = "ag.agent-config.v1"
[profiles.sneaky]
harness = "opencode"
model = "ollama/local-model"
''',
            "E_OVERLAY_SCOPE",
        ),
        (
            '''schema = "ag.agent-config.v1"
[local.secrets]
anthropic_api_key = "fake-literal"
''',
            "E_SECRET_VALUE",
        ),
    ],
)
def test_invalid_overlay_classes(tmp_path, overlay_body, code):
    main, local = files(tmp_path, overlay=overlay_body)
    with pytest.raises(AgentConfigError) as caught:
        load_config(main, local)
    assert caught.value.code == code


def test_overlay_selects_profile_and_newest_command_glob(tmp_path):
    older = tmp_path / "claude-old"
    newer = tmp_path / "claude-new"
    for command in (older, newer):
        command.write_text("#!/bin/sh\n", encoding="utf-8")
        command.chmod(0o755)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    overlay_body = f'''schema = "ag.agent-config.v1"
[local.harness.claude_code]
command_glob = "{tmp_path}/claude-*"
[roles.generator]
profile = "sonnet"
'''
    main, local = files(tmp_path, overlay=overlay_body)
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator")
    assert resolved.profile == "sonnet"
    assert resolved.harness == "claude_code"
    assert resolved.command == str(newer)


def test_opencode_keeps_model_endpoint_and_project_name(tmp_path):
    command = tmp_path / "opencode"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(0o755)
    overlay_body = f'''schema = "ag.agent-config.v1"
[local.harness.opencode]
command = "{command}"
[local.provider.ollama]
base_url = "http://ollama.example:11434/v1"
'''
    main, local = files(tmp_path, overlay=overlay_body)
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator", project_name="testapp")
    assert resolved.model == "ollama/local-model"
    assert resolved.provider_base_url == "http://ollama.example:11434/v1"

    local.write_text(f'''schema = "ag.agent-config.v1"
[local.harness.opencode]
command = "{command}"
''', encoding="utf-8")
    config, overlay = load_config(main, local)
    with pytest.raises(AgentConfigError, match="required by testapp OpenCode"):
        resolve_role(config, overlay, "generator", project_name="testapp")


def test_anthropic_secret_references_become_process_environment(tmp_path, monkeypatch):
    command = tmp_path / "claude"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(0o755)
    secret = tmp_path / "anthropic-key"
    secret.write_text("deployment-secret\n", encoding="utf-8")
    main, local = files(tmp_path, overlay=f'''schema = "ag.agent-config.v1"
[local.harness.claude_code]
command = "{command}"
[local.secrets]
anthropic_api_key_file = "{secret}"
[roles.generator]
profile = "sonnet"
''')
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").environment == {
        "ANTHROPIC_API_KEY": "deployment-secret"
    }

    monkeypatch.setenv("TEST_ANTHROPIC_KEY", "environment-secret")
    local.write_text(f'''schema = "ag.agent-config.v1"
[local.harness.claude_code]
command = "{command}"
[local.secrets]
anthropic_api_key_env = "TEST_ANTHROPIC_KEY"
[roles.generator]
profile = "sonnet"
''', encoding="utf-8")
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").environment == {
        "ANTHROPIC_API_KEY": "environment-secret"
    }
