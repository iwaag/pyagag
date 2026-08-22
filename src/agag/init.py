"""`agag init <agent>`: a new agag agent, as little of it as possible.

The value of the generated project is measured by how small it is. Nothing
here is a listener, an entrance or a role run — those are `agag.agent` and
arrive with a pyagag push. What is generated is what is the agent's own:
its name and prefixes (`src/<agent>/listener.py`, ~20 lines), its roles and
their grants (`agents.toml`), its introduction (`params/intro.md`), one
guide stub, and the files that make it a `uv` project with `git init`.

    agag init agecho                  # asks four questions, defaults shown
    agag init agecho --yes            # all defaults
    agag init agecho --instance agecho-lab1 --roles front,worker --dest ~/agents

What a human still has to do is printed at the end as a checklist: the Zulip
bot account (its credentials go in `.local/zulip.env`), subscribing it to
`#agents` and to its own channel, and — if the agent will use Plane — the
account there. An agent cannot edit its own channel's description over the
API (HTTP 400), so that stays human too.
"""

from __future__ import annotations

import argparse
import re
import socket
import string
import subprocess
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
        f"agent/guides/{guide_dir}_front/guide.md": _template("guide.md.in").substitute(values),
        f"src/{plan.agent}/__init__.py": "",
        f"src/{plan.agent}/listener.py": _template("listener.py.in").substitute(values),
        f"src/{plan.agent}/intro.py": _template("intro.py.in").substitute(values),
        "service/listen.sh": _template("listen.sh.in").substitute(values),
        ".gitignore": _template("gitignore.in").substitute(values),
        ".local/instance.toml": f'name = "{plan.instance}"\n',
    }


def generate(plan: Plan, *, git: bool = True) -> Path:
    """Write the project and `git init` it. Refuses to touch an existing root."""
    root = plan.root
    if root.exists():
        raise InitError(f"{root} already exists; choose another --dest or remove it")
    for relative, content in files(plan).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "service" / "listen.sh").chmod(0o755)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    return root


def checklist(plan: Plan) -> str:
    """What a human does next, in order. Printed, never automated."""
    root = plan.root
    return f"""
Generated {root}

Human checklist for {plan.instance}:

 1. Zulip bot account: create a generic bot named {plan.instance!r} (Settings →
    Your bots), then write its credentials to {root / '.local' / 'zulip.env'}:
        ZULIP_URL=https://<your zulip>
        ZULIP_EMAIL=<bot email>
        ZULIP_API_KEY=<api key>
 2. Zulip channels: subscribe the bot to #agents (the introduction board) and
    create its own channel #{plan.instance} with a description saying what
    it is for. The bot cannot edit that description itself.
 3. Plane (only if this agent will register Work): an account for it, and
    AGAG_PLANE_ENV or {root / '.local' / 'plane-credentials.env'}.
 4. Fill the TODOs: params/intro.md (the contract others read) and
    agent/guides/*/guide.md. Widen allowed_tools in agents.toml as needed.
 5. Run it:
        cd {root}
        uv sync
        uv run python -m {plan.agent}.intro     # post the introduction
        service/listen.sh                       # or {plan.agent.upper()}_ZULIP_LOG_ONLY=1 first
 6. To keep it running, copy a plist from pj-agdev/devenv/launchd/*.plist.in.
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
    parser.add_argument("--yes", "-y", action="store_true", help="take every default without asking")
    parser.add_argument("--no-git", action="store_true", help="do not git init the project")
    parser.set_defaults(func=run_init)


def run_init(args: argparse.Namespace) -> int:
    try:
        plan = plan_from_args(args)
        generate(plan, git=not args.no_git)
    except (InitError, subprocess.CalledProcessError) as error:
        print(f"agag init: {error}", file=sys.stderr)
        return 2
    print(checklist(plan))
    return 0
