"""An agent instance's own name — read from one local file, not spelled out.

`agforge` is the agent; `agforge-agstudio1` is *this running instance of it*
(`<agent>-<instance label><N>`, the label being the host for now). The name is
what the Zulip and Plane accounts, the instance's own channel, and the
`intro-<name>` topic all agree on, so every agent keeps it in one file rather
than repeating it at each use site.

The file is local-only because the label carries host information (see
`devdocs/README_DEV.md`): `<root>/.local/instance.toml` holds the real name and
a committed `instance.example.toml` shows the shape. With no local file the
caller's plain agent name is used — wrong for an instance, but visibly wrong
rather than silently absent.

An agent wires this up with one call:

    from agag.instance import instance_name

    def my_name() -> str:
        return instance_name(MY_ROOT / ".local" / "instance.toml",
                             fallback="agforge", env_var="AGFORGE_INSTANCE_NAME")
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

__all__ = ["instance_name"]


def instance_name(path: Path, *, fallback: str, env_var: str | None = None) -> str:
    """The instance name from `env_var` or `path`, else `fallback`.

    The environment variable wins so a second instance on one host can be run
    without a second checkout.
    """
    if env_var:
        from_env = os.environ.get(env_var, "").strip()
        if from_env:
            return from_env
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback
    name = str(data.get("name", "")).strip()
    return name or fallback
