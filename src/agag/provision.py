"""Provision the Zulip identity and channels for one generated agag agent."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agag.zulip import ZulipClient, ZulipError, read_env

ADMIN_ENV_VAR = "AGAG_ZULIP_ADMIN_ENV"

__all__ = [
    "ADMIN_ENV_VAR",
    "ProvisionError",
    "ProvisionResult",
    "add_provision_parser",
    "provision",
]


class ProvisionError(RuntimeError):
    """The requested agent cannot be provisioned safely."""


@dataclass(frozen=True)
class ProvisionResult:
    root: Path
    agent: str
    instance: str
    bot_user_id: int
    bot_email: str
    credential_path: Path
    channel_created: bool


def _project_identity(root: Path, instance_override: str | None = None) -> tuple[str, str]:
    config_path = root / "agents.toml"
    instance_path = root / ".local" / "instance.toml"
    if not config_path.is_file():
        raise ProvisionError(f"{root} is not an agag project: missing agents.toml")
    if instance_override is None and not instance_path.is_file():
        raise ProvisionError(f"{root} is not provisionable: missing .local/instance.toml")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        instance_config = (
            tomllib.loads(instance_path.read_text(encoding="utf-8"))
            if instance_override is None
            else {}
        )
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ProvisionError(f"cannot read agent identity: {error}") from error
    agent = str(config.get("project", "")).strip()
    instance = str(instance_override or instance_config.get("name", "")).strip()
    if not agent:
        raise ProvisionError(f"{config_path} has no project name")
    if not instance:
        raise ProvisionError(f"{instance_path} has no instance name")
    return agent, instance


def _description(root: Path, instance: str, override: str | None) -> str:
    if override is not None:
        description = override.strip()
    else:
        path = root / "params" / "channel.md"
        try:
            description = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as error:
            raise ProvisionError(f"missing channel description: {path}") from error
    if not description:
        raise ProvisionError("channel description must not be empty")
    return description.replace("{instance}", instance)


def _write_bot_env(path: Path, admin: dict[str, str], email: str, api_key: str) -> None:
    lines = [
        f"ZULIP_URL={admin['ZULIP_URL']}",
        f"ZULIP_EMAIL={email}",
        f"ZULIP_API_KEY={api_key}",
    ]
    if admin.get("ZULIP_CA_BUNDLE"):
        lines.append(f"ZULIP_CA_BUNDLE={admin['ZULIP_CA_BUNDLE']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staged_name = tempfile.mkstemp(prefix=".zulip.env.", dir=path.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
        staged.chmod(0o600)
        os.replace(staged, path)
    finally:
        if staged.exists():
            staged.unlink()


def provision(
    root: Path,
    *,
    admin_env: Path,
    instance: str | None = None,
    out: Path | None = None,
    description: str | None = None,
    client_factory: Callable[[Path], ZulipClient] = ZulipClient.from_env,
) -> ProvisionResult:
    """Create one bot, its credential file, and its two channel memberships."""
    root = Path(root).expanduser().resolve()
    admin_env = Path(admin_env).expanduser().resolve()
    agent, instance = _project_identity(root, instance)
    credential_path = (
        Path(out).expanduser().resolve() if out is not None else root / ".local" / "zulip.env"
    )
    rendered_description = _description(root, instance, description)
    admin = read_env(admin_env)
    missing = [key for key in ("ZULIP_URL", "ZULIP_EMAIL", "ZULIP_API_KEY") if not admin.get(key)]
    if missing:
        raise ProvisionError(f"{admin_env} is missing {', '.join(missing)}")
    if "@" not in admin["ZULIP_EMAIL"]:
        raise ProvisionError(f"{admin_env} has a malformed ZULIP_EMAIL")

    client = client_factory(admin_env)
    bot_email = f"{instance}-bot@{admin['ZULIP_EMAIL'].rsplit('@', 1)[1]}"
    existing = client.user_by_email(bot_email)
    if existing is not None:
        raise ProvisionError(
            f"refusing to provision {instance}: Zulip user {bot_email} already exists "
            f"(user_id={existing.get('user_id', 'unknown')})"
        )

    created = client.create_bot(instance, instance)
    bot_user_id = int(created["user_id"])
    bot_email = str(created["email"])
    _write_bot_env(credential_path, admin, bot_email, str(created["api_key"]))

    admin_user_id = int(client.whoami()["user_id"])
    client.subscribe_channels(["agents"], principals=[bot_user_id])
    channel = next((item for item in client.channels() if item.get("name") == instance), None)
    client.create_channel(
        instance,
        rendered_description,
        principals=[bot_user_id, admin_user_id],
    )
    if channel is not None:
        client.update_channel_description(int(channel["stream_id"]), rendered_description)

    return ProvisionResult(
        root=root,
        agent=agent,
        instance=instance,
        bot_user_id=bot_user_id,
        bot_email=bot_email,
        credential_path=credential_path,
        channel_created=channel is None,
    )


def add_provision_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "provision",
        help="create an agag agent's Zulip bot, credentials, and channels",
    )
    parser.add_argument("root", nargs="?", default=".", help="agent project root (default .)")
    parser.add_argument(
        "--admin-env",
        help=f"owner-class Zulip env (default ${ADMIN_ENV_VAR})",
    )
    parser.add_argument("--instance", help="instance name (default from .local/instance.toml)")
    parser.add_argument("--out", help="bot credential output (default <root>/.local/zulip.env)")
    parser.add_argument("--description", help="own-channel description override")
    parser.set_defaults(func=run_provision)


def run_provision(args: argparse.Namespace) -> int:
    admin_env = args.admin_env or os.environ.get(ADMIN_ENV_VAR)
    if not admin_env:
        print(
            f"agag provision: set {ADMIN_ENV_VAR} or pass --admin-env",
            file=sys.stderr,
        )
        return 2
    try:
        result = provision(
            Path(args.root),
            admin_env=Path(admin_env),
            instance=args.instance,
            out=Path(args.out) if args.out else None,
            description=args.description,
        )
    except (ProvisionError, ZulipError, OSError, KeyError, ValueError) as error:
        print(f"agag provision: {error}", file=sys.stderr)
        return 2
    channel_action = "created" if result.channel_created else "updated"
    print(
        f"Provisioned {result.instance}\n"
        f"  bot: {result.bot_email} (user_id={result.bot_user_id})\n"
        f"  credentials: {result.credential_path} (mode 0600)\n"
        f"  channels: subscribed to #agents; #{result.instance} {channel_action}\n\n"
        f"Next commands:\n"
        f"  cd {result.root}\n"
        f"  uv run python -m {result.agent}.intro\n"
        f"  service/listen.sh"
    )
    return 0
