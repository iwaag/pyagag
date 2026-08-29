"""`agag init <agent>`: a new agag agent, as little of it as possible.

The value of the generated project is measured by how small it is. Nothing
here is a listener, an entrance or a role run — those are `agag.agent` and
arrive with a pyagag push. What is generated is what is the agent's own:
its name and prefixes (`src/<agent>/listener.py`, ~20 lines), its roles and
their grants (`agents.toml`), its introduction (`params/intro.md`), one
guide stub, and the files that make it a `uv` project. Version control is
the caller's: the project may be a new repo, a folder in an existing one, or
nothing yet — `agag init` only writes files.

    agag init agecho                  # asks four questions, defaults shown
    agag init agecho --yes            # all defaults
    agag init agecho --instance agecho-lab1 --roles front,worker --dest ~/agents

`--provision` immediately creates the Zulip bot, its local credentials and
channels with the owner-class credential path named by
`AGAG_ZULIP_ADMIN_ENV`. `--like <root>` carries a sibling's local harness
overlay into the new instance. The printed human checklist is only the work
that cannot be automated here.
"""

from __future__ import annotations

import argparse
import re
import socket
import string
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

#: The grant every generated role starts with: enough to read the chat, write
#: its own workspace and speak as the instance. Widen it in `agents.toml`.
DEFAULT_ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash(agentchat:*)"
DEFAULT_PROFILE = "sonnet"
DEFAULT_ROLES = ("front",)
AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

__all__ = ["InitError", "Plan", "add_init_parser", "checklist", "generate", "plan_from_args"]


class InitError(RuntimeError):
    """The request cannot be generated as asked."""


@dataclass(frozen=True)
class Plan:
    """Everything `agag init` decided, before a file is written."""

    agent: str
    instance: str
    plan_prefix: str
    run_prefix: str
    roles: tuple[str, ...] = DEFAULT_ROLES
    profile: str = DEFAULT_PROFILE
    dest: Path = field(default_factory=Path.cwd)

    @property
    def root(self) -> Path:
        return self.dest / self.agent


def _hostname() -> str:
    name = socket.gethostname().split(".", 1)[0].lower()
    return re.sub(r"[^a-z0-9]", "", name) or "host"


def defaults(agent: str) -> dict[str, str]:
    return {
        "instance": f"{agent}-{_hostname()}1",
        "plan_prefix": f"{agent}plan-",
        "run_prefix": f"{agent}run-",
        "roles": ",".join(DEFAULT_ROLES),
        "profile": DEFAULT_PROFILE,
        "dest": ".",
    }


def _ask(question: str, default: str, *, interactive: bool) -> str:
    if not interactive:
        return default
    answer = input(f"{question} [{default}]: ").strip()
    return answer or default


def plan_from_args(args: argparse.Namespace) -> Plan:
    """Fill what the flags left open, asking when there is a terminal."""
    agent = args.agent
    if not AGENT_NAME_RE.fullmatch(agent):
        raise InitError(f"agent name {agent!r} must match {AGENT_NAME_RE.pattern} (it is a Python package)")
    base = defaults(agent)
    interactive = not args.yes and sys.stdin.isatty()
    instance = args.instance or _ask("instance name", base["instance"], interactive=interactive)
    plan_prefix = args.plan_prefix or _ask("plan topic prefix", base["plan_prefix"], interactive=interactive)
    run_prefix = args.run_prefix or _ask("run topic prefix", base["run_prefix"], interactive=interactive)
    roles = args.roles or _ask("roles (comma-separated)", base["roles"], interactive=interactive)
    profile = args.profile or _ask("profile for every role", base["profile"], interactive=interactive)
    dest = args.dest or _ask("output directory (the project goes in <dir>/<agent>)", base["dest"], interactive=interactive)
    role_list = tuple(r.strip() for r in roles.split(",") if r.strip())
    if "front" not in role_list:
        raise InitError("roles must include 'front': the entrance runs it")
    return Plan(agent, instance, plan_prefix, run_prefix, role_list, profile, Path(dest).expanduser().resolve())


# --- rendering ------------------------------------------------------------


def _template(name: str) -> string.Template:
    text = resources.files("agag.templates").joinpath(name).read_text(encoding="utf-8")
    return string.Template(text)


def _roles_toml(plan: Plan) -> str:
    blocks = []
    for role in plan.roles:
        blocks.append(
            f"[roles.{role}]\n"
            f'profile = "{plan.profile}"\n'
            "requires = []\n"
            f'allowed_tools = "{DEFAULT_ALLOWED_TOOLS}"\n'
        )
    return "\n".join(blocks)


def files(plan: Plan) -> dict[str, str]:
    """Relative path → content, for every generated file."""
    values = {
        "agent": plan.agent,
        "plan_prefix": plan.plan_prefix,
        "run_prefix": plan.run_prefix,
        "roles": _roles_toml(plan),
    }
    guide_dir = plan.plan_prefix.rstrip("-") or "request"
    return {
        "pyproject.toml": _template("pyproject.toml.in").substitute(values),
        "agents.toml": _template("agents.toml.in").substitute(values),
        "instance.example.toml": _template("instance.example.toml.in").substitute(values),
        "params/intro.md": _template("intro.md.in").substitute(values),
        "params/channel.md": _template("channel.md.in").substitute(values),
        f"agent/guides/{guide_dir}_front/guide.md": _template("guide.md.in").substitute(values),
        f"src/{plan.agent}/__init__.py": "",
        f"src/{plan.agent}/listener.py": _template("listener.py.in").substitute(values),
        f"src/{plan.agent}/intro.py": _template("intro.py.in").substitute(values),
        "service/listen.sh": _template("listen.sh.in").substitute(values),
        ".gitignore": _template("gitignore.in").substitute(values),
        ".local/instance.toml": f'name = "{plan.instance}"\n',
    }


def generate(plan: Plan) -> Path:
    """Write the project. Refuses to touch an existing root."""
    root = plan.root
    if root.exists():
        raise InitError(f"{root} already exists; choose another --dest or remove it")
    for relative, content in files(plan).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "service" / "listen.sh").chmod(0o755)
    return root


def copy_compatible_overlay(source: Path, target: Path, roles: tuple[str, ...]) -> None:
    """Copy sibling machine facts without importing overrides for absent roles.

    A local harness path is portable across sibling agents on one machine;
    a role override is agent-specific.  Keep overrides only when the new
    committed config declares that role, otherwise the v2 overlay validator
    correctly rejects the generated agent at first run.
    """
    lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    skipping = False
    section = re.compile(r"^\s*\[([^]]+)]\s*(?:#.*)?$")
    for line in lines:
        match = section.match(line.rstrip("\r\n"))
        if match:
            name = match.group(1).strip()
            skipping = False
            if name.startswith("roles."):
                role = name.removeprefix("roles.").strip().strip('"').strip("'")
                skipping = role not in roles
        if not skipping:
            kept.append(line)
    target.write_text("".join(kept), encoding="utf-8")
    target.chmod(source.stat().st_mode & 0o777)


def checklist(plan: Plan) -> str:
    """The intentionally short list of work that still belongs to a human."""
    root = plan.root
    return f"""
Generated {root}

Human checklist for {plan.instance}:

 1. Once per realm: create the dedicated provisioner account and put its
    owner-class credentials in the path named by AGAG_ZULIP_ADMIN_ENV.
 2. Plane (only if this agent will register Work): create an account for it and
    AGAG_PLANE_ENV or {root / '.local' / 'plane-credentials.env'}.
 3. To keep the listener running permanently, install it with launchd or
    Ansible after the foreground/background trial.

Agent-side next step (unless --provision was used):
    agag provision {root}
"""


# --- CLI --------------------------------------------------------------------


def add_init_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "init", help="generate a new agag agent project",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("agent", help="short agent name, e.g. agecho (a Python package name)")
    parser.add_argument("--instance", help="instance name (default <agent>-<hostname>1)")
    parser.add_argument("--plan-prefix", help="request topic prefix (default <agent>plan-)")
    parser.add_argument("--run-prefix", help="run topic prefix (default <agent>run-)")
    parser.add_argument("--roles", help="comma-separated roles, must include front (default front)")
    parser.add_argument("--profile", help=f"profile for every role (default {DEFAULT_PROFILE})")
    parser.add_argument("--dest", help="directory to create <agent>/ in (default .)")
    parser.add_argument(
        "--like",
        help="copy <root>/.local/agents.local.toml into the generated agent",
    )
    parser.add_argument(
        "--provision",
        action="store_true",
        help="provision Zulip immediately after generation",
    )
    parser.add_argument(
        "--admin-env",
        help="owner-class Zulip env for --provision (default $AGAG_ZULIP_ADMIN_ENV)",
    )
    parser.add_argument("--description", help="own-channel description for --provision")
    parser.add_argument("--yes", "-y", action="store_true", help="take every default without asking")
    parser.set_defaults(func=run_init)


def run_init(args: argparse.Namespace) -> int:
    overlay_source = None
    if args.like:
        overlay_source = Path(args.like).expanduser().resolve() / ".local" / "agents.local.toml"
        if not overlay_source.is_file():
            print(f"agag init: --like source is missing {overlay_source}", file=sys.stderr)
            return 2
    try:
        plan = plan_from_args(args)
        root = generate(plan)
        if overlay_source is not None:
            overlay_target = root / ".local" / "agents.local.toml"
            copy_compatible_overlay(overlay_source, overlay_target, plan.roles)
    except InitError as error:
        print(f"agag init: {error}", file=sys.stderr)
        return 2
    print(checklist(plan))
    if args.provision:
        from agag.provision import AGENT_FOLDER, run_provision

        provision_args = argparse.Namespace(
            root=str(root),
            admin_env=args.admin_env,
            instance=None,
            out=None,
            description=args.description,
            folder=AGENT_FOLDER,
        )
        return run_provision(provision_args)
    return 0
