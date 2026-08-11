"""Loader and resolver for the language-neutral ag.agent-config.v1 contract."""

from __future__ import annotations

import glob
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "ag.agent-config.v1"
CANONICAL_HARNESSES = {"opencode", "claude_code", "fake"}
INTRINSIC_CAPABILITIES = {
    "opencode": {"agentic_tools", "workspace_fs"},
    "claude_code": {"agentic_tools", "workspace_fs"},
    "fake": set(),
}
MODEL_ID_RE = re.compile(r"^[a-z0-9_-]+/[^/\s]+$")


class AgentConfigError(Exception):
    """Configuration or availability failure with a stable contract code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ResolvedAgent:
    role: str
    profile: str
    harness: str
    provider: str
    model: str
    model_options: dict[str, Any]
    command: str
    provider_base_url: str | None
    environment: dict[str, str] = field(default_factory=dict)

    @property
    def native_model(self) -> str:
        return self.model.split("/", 1)[1] if self.harness == "claude_code" else self.model


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise AgentConfigError("E_SCHEMA", f"missing committed config: {path}")
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AgentConfigError("E_SCHEMA", f"cannot read {path}: {error}") from error
    if data.get("schema") != SCHEMA:
        raise AgentConfigError("E_SCHEMA", f"{path} must declare schema = {SCHEMA!r}")
    return data


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise AgentConfigError("E_SCHEMA", f"{key} must be a table")
    return value


def _string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AgentConfigError("E_SCHEMA", f"{key} must be an array of strings")
    return value


def _validate_overlay(overlay: dict[str, Any]) -> None:
    if not overlay:
        return
    extras = set(overlay) - {"schema", "local", "roles", "capabilities"}
    if extras:
        raise AgentConfigError("E_OVERLAY_SCOPE", f"overlay keys not allowed: {sorted(extras)}")
    local = _table(overlay, "local")
    extras = set(local) - {"harness", "provider", "secrets"}
    if extras:
        raise AgentConfigError("E_OVERLAY_SCOPE", f"overlay local keys not allowed: {sorted(extras)}")
    for harness, facts in _table(local, "harness").items():
        if harness not in CANONICAL_HARNESSES:
            raise AgentConfigError("E_UNKNOWN_HARNESS", f"unknown harness {harness!r}")
        if not isinstance(facts, dict) or set(facts) - {"command", "command_glob"}:
            raise AgentConfigError("E_OVERLAY_SCOPE", f"invalid local.harness.{harness} keys")
        if bool(facts.get("command")) == bool(facts.get("command_glob")):
            raise AgentConfigError(
                "E_OVERLAY_SCOPE",
                f"local.harness.{harness} must set exactly one command key",
            )
    for provider, facts in _table(local, "provider").items():
        if not isinstance(facts, dict) or set(facts) - {"base_url"}:
            raise AgentConfigError("E_OVERLAY_SCOPE", f"invalid local.provider.{provider} keys")
    for key, value in _table(local, "secrets").items():
        if not key.endswith(("_file", "_env")) or not isinstance(value, str):
            raise AgentConfigError("E_SECRET_VALUE", f"secret {key!r} must be a string reference")
    for role, override in _table(overlay, "roles").items():
        if not isinstance(override, dict) or set(override) != {"profile"}:
            raise AgentConfigError("E_OVERLAY_SCOPE", f"role override {role!r} may only set profile")
    capabilities = _table(overlay, "capabilities")
    if set(capabilities) - {"provides"}:
        raise AgentConfigError("E_OVERLAY_SCOPE", "overlay capabilities may only set provides")
    _string_list(capabilities.get("provides"), "capabilities.provides")


def _validate_committed(config: dict[str, Any]) -> None:
    models = _table(config, "models")
    profiles = _table(config, "profiles")
    roles = _table(config, "roles")
    for model, options in models.items():
        if not MODEL_ID_RE.fullmatch(model):
            raise AgentConfigError("E_BAD_MODEL_ID", f"malformed model ID {model!r}")
        if not isinstance(options, dict):
            raise AgentConfigError("E_SCHEMA", f"model {model!r} options must be a table")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise AgentConfigError("E_SCHEMA", f"profile {name!r} must be a table")
        harness, model = profile.get("harness"), profile.get("model")
        if harness not in CANONICAL_HARNESSES:
            raise AgentConfigError("E_UNKNOWN_HARNESS", f"profile {name!r}: {harness!r}")
        if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise AgentConfigError("E_BAD_MODEL_ID", f"profile {name!r}: malformed model {model!r}")
        if model not in models:
            raise AgentConfigError("E_UNKNOWN_MODEL", f"profile {name!r}: undeclared model {model!r}")
        if harness == "claude_code" and model.split("/", 1)[0] != "anthropic":
            raise AgentConfigError("E_INCOMPATIBLE", f"profile {name!r}: {harness} cannot serve {model}")
    for role, settings in roles.items():
        if not isinstance(settings, dict):
            raise AgentConfigError("E_SCHEMA", f"role {role!r} must be a table")
        if settings.get("profile") not in profiles:
            raise AgentConfigError("E_UNKNOWN_PROFILE", f"role {role!r}: unknown profile {settings.get('profile')!r}")
        _string_list(settings.get("requires"), f"roles.{role}.requires")
    _string_list(_table(config, "capabilities").get("provides"), "capabilities.provides")


def load_config(committed_path: Path, overlay_path: Path | None = None) -> tuple[dict, dict]:
    config = _read_toml(committed_path, required=True)
    overlay = _read_toml(overlay_path, required=False) if overlay_path else {}
    _validate_committed(config)
    _validate_overlay(overlay)
    return config, overlay


def _resolve_command(harness: str, overlay: dict[str, Any], *, check_available: bool) -> str:
    facts = _table(_table(_table(overlay, "local"), "harness"), harness)
    command = facts.get("command")
    if not command and facts.get("command_glob"):
        matches = glob.glob(os.path.expanduser(facts["command_glob"]))
        if matches:
            command = max(matches, key=lambda item: Path(item).stat().st_mtime)
    defaults = {"opencode": "opencode", "claude_code": "claude"}
    command = os.path.expanduser(command or defaults.get(harness, ""))
    resolved = shutil.which(command) if command and not Path(command).is_absolute() else command
    if check_available and (not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK)):
        raise AgentConfigError("E_UNAVAILABLE", f"executable for {harness!r} is unavailable: {command!r}")
    return str(resolved or command)


def _resolve_secret_environment(
    provider: str, overlay: dict[str, Any], *, check_available: bool
) -> dict[str, str]:
    if provider != "anthropic":
        return {}
    secrets = _table(_table(overlay, "local"), "secrets")
    if reference := secrets.get("anthropic_api_key_env"):
        value = os.environ.get(reference, "")
        if check_available and not value:
            raise AgentConfigError("E_UNAVAILABLE", f"secret environment variable {reference!r} is unavailable")
        return {"ANTHROPIC_API_KEY": value} if value else {}
    if reference := secrets.get("anthropic_api_key_file"):
        path = Path(os.path.expanduser(reference))
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            if check_available:
                raise AgentConfigError("E_UNAVAILABLE", f"secret file {reference!r} is unavailable: {error}") from error
            return {}
        if check_available and not value:
            raise AgentConfigError("E_UNAVAILABLE", f"secret file {reference!r} is empty")
        return {"ANTHROPIC_API_KEY": value} if value else {}
    return {}


def resolve_role(
    config: dict[str, Any],
    overlay: dict[str, Any],
    role: str,
    *,
    profile_override: str | None = None,
    check_available: bool = True,
    project_name: str = "application",
) -> ResolvedAgent:
    roles = _table(config, "roles")
    if role not in roles:
        raise AgentConfigError("E_UNKNOWN_PROFILE", f"unknown role {role!r}")
    overlay_roles = _table(overlay, "roles")
    if set(overlay_roles) - set(roles):
        raise AgentConfigError("E_OVERLAY_SCOPE", "overlay may not introduce roles")
    profile_name = profile_override or overlay_roles.get(role, {}).get("profile") or roles[role]["profile"]
    profiles = _table(config, "profiles")
    if profile_name not in profiles:
        raise AgentConfigError("E_UNKNOWN_PROFILE", f"unknown profile {profile_name!r}")
    profile = profiles[profile_name]
    harness, model = profile["harness"], profile["model"]
    provided = set(_string_list(_table(config, "capabilities").get("provides"), "provides"))
    provided.update(_string_list(_table(overlay, "capabilities").get("provides"), "provides"))
    provided.update(INTRINSIC_CAPABILITIES[harness])
    required = set(_string_list(roles[role].get("requires"), f"roles.{role}.requires"))
    if missing := required - provided:
        raise AgentConfigError("E_CAPABILITY_UNMET", f"role {role!r} lacks {sorted(missing)}")
    provider = model.split("/", 1)[0]
    provider_base_url = _table(_table(_table(overlay, "local"), "provider"), provider).get("base_url")
    if check_available and harness == "opencode" and provider == "ollama" and not provider_base_url:
        raise AgentConfigError(
            "E_UNAVAILABLE",
            f"local.provider.ollama.base_url is required by {project_name} OpenCode",
        )
    return ResolvedAgent(
        role,
        profile_name,
        harness,
        provider,
        model,
        dict(_table(config, "models")[model]),
        _resolve_command(harness, overlay, check_available=check_available),
        provider_base_url,
        _resolve_secret_environment(provider, overlay, check_available=check_available),
    )
