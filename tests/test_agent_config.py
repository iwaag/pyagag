"""Ported agent-config contract and resolution tests."""

import os
import sys
from pathlib import Path

import pytest

from agag.agent_config import AgentConfigError, load_config, resolve_role

BASE = '''schema = "ag.agent-config.v1"
[models."ollama/local-model"]
[models."anthropic/claude-sonnet-5"]
[profiles.local]
harness = "agcode"
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
        (BASE.replace('harness = "agcode"', 'harness = "ollama"'), "E_UNKNOWN_HARNESS"),
        (BASE.replace('model = "ollama/local-model"', 'model = "ollama/absent"'), "E_UNKNOWN_MODEL"),
        (BASE.replace('harness = "agcode"', 'harness = "claude_code"', 1), "E_INCOMPATIBLE"),
        (BASE.replace('harness = "agcode"', 'harness = "gemini_cli"', 1), "E_INCOMPATIBLE"),
        (BASE.replace('harness = "agcode"', 'harness = "agy"', 1), "E_INCOMPATIBLE"),
        (BASE.replace('harness = "agcode"', 'harness = "codex"', 1), "E_INCOMPATIBLE"),
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
harness = "agcode"
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


def test_a_declared_provider_endpoint_reaches_the_resolved_agent(tmp_path):
    """The endpoint is machine-local, so it lives in the overlay and travels
    on the resolved agent — the canonical model ID never carries it."""
    main, local = files(tmp_path, overlay='''schema = "ag.agent-config.v1"
[local.provider.ollama]
base_url = "http://ollama.example:11434"
''')
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator")
    assert resolved.model == "ollama/local-model"
    assert resolved.provider_base_url == "http://ollama.example:11434"


def test_an_absent_provider_endpoint_is_not_an_error(tmp_path):
    """Resolution used to refuse an ollama profile with no declared endpoint,
    because the harness of the day could not be pointed at one without it.
    agcode carries its own local default, so absence is now legal and the
    caller simply omits --base-url."""
    main, local = files(tmp_path, overlay='''schema = "ag.agent-config.v1"\n''')
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").provider_base_url is None


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


AGCODE = BASE + '''[profiles.agcode]
harness = "agcode"
model = "ollama/local-model"
'''


def test_agcode_profile_resolves_to_this_interpreter_without_an_endpoint(tmp_path):
    """agcode ships inside the package: its command is the interpreter, and no
    local.provider.ollama.base_url is required, because the module has a
    working default endpoint."""
    main, local = files(tmp_path, AGCODE, overlay='''schema = "ag.agent-config.v1"
[roles.generator]
profile = "agcode"
''')
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator")

    assert resolved.harness == "agcode"
    assert resolved.command == sys.executable
    assert resolved.provider_base_url is None
    # The canonical ID is what records carry; the CLI gets the native name.
    assert resolved.model == "ollama/local-model"
    assert resolved.native_model == "local-model"
    # Intrinsic capabilities cover a role that asks for them.
    main.write_text(AGCODE.replace("requires = []", 'requires = ["agentic_tools", "workspace_fs"]'), encoding="utf-8")
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").harness == "agcode"


def test_agcode_command_overlay_points_at_a_foreign_interpreter(tmp_path):
    interpreter = tmp_path / "python3-elsewhere"
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    interpreter.chmod(0o755)
    main, local = files(tmp_path, AGCODE, overlay=f'''schema = "ag.agent-config.v1"
[local.harness.agcode]
command = "{interpreter}"
[roles.generator]
profile = "agcode"
''')
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").command == str(interpreter)


V2 = BASE.replace('schema = "ag.agent-config.v1"', 'schema = "ag.agent-config.v2"')


def test_v2_requires_a_grant_per_role(tmp_path):
    main, local = files(tmp_path, V2)
    with pytest.raises(AgentConfigError) as caught:
        load_config(main, local)
    assert caught.value.code == "E_SCHEMA"
    assert "allowed_tools" in str(caught.value)


def test_v2_grant_reaches_the_resolved_agent_in_either_spelling(tmp_path):
    body = V2.replace("requires = []", 'requires = []\nallowed_tools = "Read,Write"') + (
        '[roles.reader]\nprofile = "local"\nallowed_tools = ["Read", "Grep"]\n'
    )
    main, local = files(tmp_path, body)
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator", check_available=False).allowed_tools == "Read,Write"
    assert resolve_role(config, overlay, "reader", check_available=False).allowed_tools == "Read,Grep"


def test_v1_roles_carry_no_grant(tmp_path):
    main, local = files(tmp_path)
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator", check_available=False).allowed_tools is None


GEMINI = BASE + '''[models."google/gemini-2.5-flash"]
[profiles.gemini]
harness = "gemini_cli"
model = "google/gemini-2.5-flash"
'''


def test_gemini_profile_resolves_to_the_gemini_command(tmp_path, monkeypatch):
    """gemini_cli takes the native name, serves google only, defaults to the
    `gemini` on PATH, and carries no secret: the CLI owns its own key."""
    gemini = tmp_path / "bin" / "gemini"
    gemini.parent.mkdir()
    gemini.write_text("#!/bin/sh\n", encoding="utf-8")
    gemini.chmod(0o755)
    monkeypatch.setenv("PATH", str(gemini.parent) + os.pathsep + os.environ["PATH"])
    main, local = files(tmp_path, GEMINI, overlay='''schema = "ag.agent-config.v1"
[roles.generator]
profile = "gemini"
''')
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator")

    assert resolved.harness == "gemini_cli"
    assert resolved.provider == "google"
    assert resolved.command == str(gemini)
    assert resolved.model == "google/gemini-2.5-flash"
    assert resolved.native_model == "gemini-2.5-flash"
    assert resolved.environment == {}
    main.write_text(GEMINI.replace("requires = []", 'requires = ["agentic_tools", "workspace_fs"]'), encoding="utf-8")
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").harness == "gemini_cli"


def test_gemini_profile_without_the_executable_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    main, local = files(tmp_path, GEMINI, overlay='''schema = "ag.agent-config.v1"
[roles.generator]
profile = "gemini"
''')
    config, overlay = load_config(main, local)
    with pytest.raises(AgentConfigError) as caught:
        resolve_role(config, overlay, "generator")
    assert caught.value.code == "E_UNAVAILABLE"


def test_google_secret_references_become_gemini_api_key(tmp_path, monkeypatch):
    gemini = tmp_path / "bin" / "gemini"
    gemini.parent.mkdir()
    gemini.write_text("#!/bin/sh\n", encoding="utf-8")
    gemini.chmod(0o755)
    monkeypatch.setenv("PATH", str(gemini.parent) + os.pathsep + os.environ["PATH"])
    key_file = tmp_path / "gemini.key"
    key_file.write_text("file-key\n", encoding="utf-8")
    main, local = files(tmp_path, GEMINI, overlay=f'''schema = "ag.agent-config.v1"
[local.secrets]
google_api_key_file = "{key_file}"
[roles.generator]
profile = "gemini"
''')
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").environment == {"GEMINI_API_KEY": "file-key"}

    monkeypatch.setenv("MY_GEMINI", "env-key")
    main, local = files(tmp_path, GEMINI, overlay='''schema = "ag.agent-config.v1"
[local.secrets]
google_api_key_env = "MY_GEMINI"
[roles.generator]
profile = "gemini"
''')
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").environment == {"GEMINI_API_KEY": "env-key"}

    key_file.unlink()
    main, local = files(tmp_path, GEMINI, overlay=f'''schema = "ag.agent-config.v1"
[local.secrets]
google_api_key_file = "{key_file}"
[roles.generator]
profile = "gemini"
''')
    config, overlay = load_config(main, local)
    with pytest.raises(AgentConfigError) as caught:
        resolve_role(config, overlay, "generator")
    assert caught.value.code == "E_UNAVAILABLE"


AGY = BASE + '''[models."antigravity/claude-sonnet-4-6"]
[profiles.agy]
harness = "agy"
model = "antigravity/claude-sonnet-4-6"
'''


def test_agy_profile_resolves_to_the_agy_command_and_pushes_no_secret(tmp_path, monkeypatch):
    """agy serves the `antigravity` provider (the CLI's own catalog, Claude
    included), takes the native name, defaults to `agy` on PATH, and carries
    no secret even when the overlay names a Google key: the CLI owns its
    OAuth token."""
    agy = tmp_path / "bin" / "agy"
    agy.parent.mkdir()
    agy.write_text("#!/bin/sh\n", encoding="utf-8")
    agy.chmod(0o755)
    monkeypatch.setenv("PATH", str(agy.parent) + os.pathsep + os.environ["PATH"])
    main, local = files(tmp_path, AGY, overlay='''schema = "ag.agent-config.v1"
[local.secrets]
google_api_key_file = "/nonexistent/gemini"
[roles.generator]
profile = "agy"
''')
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator")
    assert resolved.harness == "agy"
    assert resolved.provider == "antigravity"
    assert resolved.command == str(agy)
    assert resolved.native_model == "claude-sonnet-4-6"
    assert resolved.environment == {}
    # And the overlay's command spelling expands `~`.
    local.write_text('''schema = "ag.agent-config.v1"
[local.harness.agy]
command = "~/.local/bin/agy"
[roles.generator]
profile = "agy"
''', encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".local" / "bin" / "agy").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / ".local" / "bin" / "agy").chmod(0o755)
    config, overlay = load_config(main, local)
    assert resolve_role(config, overlay, "generator").command == str(tmp_path / ".local" / "bin" / "agy")


CODEX = BASE + '''[models."openai/gpt-5.4-mini"]
effort = "low"
[profiles.codex]
harness = "codex"
model = "openai/gpt-5.4-mini"
'''


def test_codex_profile_resolves_to_the_codex_command_with_its_effort_and_no_secret(tmp_path, monkeypatch):
    """codex serves the `openai` provider (the ChatGPT account's catalog),
    takes the native name, defaults to `codex` on PATH, carries the model's
    declared `effort` in `model_options`, and pushes no secret: the CLI owns
    its own auth."""
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", str(codex.parent) + os.pathsep + os.environ["PATH"])
    main, local = files(tmp_path, CODEX, overlay='''schema = "ag.agent-config.v1"
[local.secrets]
google_api_key_file = "/nonexistent/gemini"
[roles.generator]
profile = "codex"
''')
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator")
    assert resolved.harness == "codex"
    assert resolved.provider == "openai"
    assert resolved.command == str(codex)
    assert resolved.native_model == "gpt-5.4-mini"
    assert resolved.model_options == {"effort": "low"}
    assert resolved.environment == {}
    # And the overlay's command spelling expands `~`.
    local.write_text('''schema = "ag.agent-config.v1"
[local.harness.codex]
command = "~/.local/bin/codex"
[roles.generator]
profile = "codex"
''', encoding="utf-8")
    config, overlay = load_config(main, local)
    resolved = resolve_role(config, overlay, "generator", check_available=False)
    assert resolved.command == os.path.expanduser("~/.local/bin/codex")
