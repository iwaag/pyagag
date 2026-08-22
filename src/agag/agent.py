"""The agag agent skeleton: what every standardized agent had copied.

Until `agag_builder` p1 each agent carried the same five modules —
`instance.py`, `intro.py`, `role_run.py`, `zulip_listener.py`,
`entrance_topic.py` — differing in a docstring, a prefix, and a root path.
A convention change (p8's selfnote, p10's entrance) had to be pasted into
each of them. This module is those five, written once, driven by an
`AgentSpec`; an agent is now its spec, its `agents.toml`, its guides and its
own topic handlers, and everything else arrives with a pyagag push.

    SPEC = AgentSpec("agecho", ROOT, plan_prefix="agechoplan-", run_prefix="agechorun-")
    listener_main(SPEC, {})      # serves the instance's own channel through the entrance

What the skeleton fixes, so a generated project has nothing to decide:

- `<root>/.local/zulip.env` — the bot's credentials, `<root>/.local/instance.toml`
  — its instance name (`<AGENT>_INSTANCE_NAME` overrides), `<root>/agents.toml`
  + `<root>/.local/agents.local.toml` — the config pair, `<root>/params/intro.md`
  — the introduction, `<root>/agent/guides/` — the guides.
- `.local/topics/<channel>/<topic>/<N>/<role>/` — serving workspaces,
  `.local/agent/<kind>/run-NNNN.json` — run records.
- A run gets `agentchat` on PATH, `AGENTCHAT_ZULIP_ENV` naming the instance's
  own credentials and `AGENTCHAT_HOME` naming the conversation it serves —
  the handover that makes a run speak as this instance and lets an exchange
  outlive the run (`agent_standardize` p6–p8).
- The role's tool grant comes from `agents.toml` (`ag.agent-config.v2`,
  `[roles.X] allowed_tools`). There is no table to forget a role in.
- `<AGENT>_ZULIP_LOG_ONLY=1` (or `AGAG_ZULIP_LOG_ONLY=1`) runs the listener as
  a passive observer.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import selfnote
from .agent_config import ResolvedAgent, load_config, resolve_role
from .harness import run_harness, write_run_record
from .instance import instance_name as read_instance_name
from .intro import post_intro
from .zulip import ZulipClient, channel_name, dm_partners, is_dm_for_us, log, serve, sweep_serve

#: The common sweep ack, shared wording across agents. Posted synchronously
#: on a topic match: it makes this bot the last poster, so the pull loop stops
#: re-matching the topic while the run is in flight.
SWEEP_ACK = "Message received. Please wait for the reply."
#: The older DM-route ack, still recognized as transport noise.
ACK_PREFIX = "On it — working on this now."
#: `agag.chat.ENV_VARIABLE`, spelled here so the run and the CLI agree.
AGENTCHAT_ENV_VARIABLE = "AGENTCHAT_ZULIP_ENV"
#: Passive-observer switch that works for every agent at once.
LOG_ONLY_ENV_VAR = "AGAG_ZULIP_LOG_ONLY"

__all__ = [
    "ACK_PREFIX",
    "AGENTCHAT_ENV_VARIABLE",
    "AgentSpec",
    "LOG_ONLY_ENV_VAR",
    "SWEEP_ACK",
    "chat_environment",
    "intro_main",
    "is_ack",
    "listener_main",
    "log_only",
    "resolve_spec_role",
    "run_role",
    "topic_filter",
]


def is_ack(content: str) -> bool:
    """Our own transport noise, which is not conversation."""
    return content.startswith(ACK_PREFIX) or content == SWEEP_ACK


@dataclass(frozen=True)
class AgentSpec:
    """One agent, as the skeleton needs to know it.

    `agent` is the short name (`agforge`); the instance name
    (`agforge-agstudio1`) is read from `.local/instance.toml`. `plan_prefix`
    and `run_prefix` are the topic prefixes the agent's own vocabulary uses;
    the default entrance guide names them, and `topic_filter` sweeps them in
    every subscribed channel.

    `extra_environment` is the agent's own addition to a run's environment —
    forge puts its tool directories on PATH there. It is called per run, after
    the role is resolved, and may read `PATH` from the environment it is
    handed.
    """

    agent: str
    root: Path
    plan_prefix: str = ""
    run_prefix: str = ""
    extra_environment: Callable[[Mapping[str, str]], Mapping[str, str]] | None = field(
        default=None, compare=False
    )

    # --- paths ----------------------------------------------------------
    @property
    def local(self) -> Path:
        return self.root / ".local"

    @property
    def zulip_env(self) -> Path:
        return self.local / "zulip.env"

    @property
    def instance_toml(self) -> Path:
        return self.local / "instance.toml"

    @property
    def agents_config(self) -> Path:
        return self.root / "agents.toml"

    @property
    def agents_local_config(self) -> Path:
        return self.local / "agents.local.toml"

    @property
    def intro_path(self) -> Path:
        return self.root / "params" / "intro.md"

    @property
    def guides(self) -> Path:
        return self.root / "agent" / "guides"

    @property
    def topics_root(self) -> Path:
        return self.local / "topics"

    @property
    def records_root(self) -> Path:
        return self.local / "agent"

    # --- names ----------------------------------------------------------
    @property
    def env_prefix(self) -> str:
        return self.agent.upper().replace("-", "_")

    @property
    def instance_env_var(self) -> str:
        return f"{self.env_prefix}_INSTANCE_NAME"

    @property
    def log_only_env_var(self) -> str:
        return f"{self.env_prefix}_ZULIP_LOG_ONLY"

    @property
    def sweep_prefixes(self) -> tuple[str, ...]:
        return tuple(p for p in (self.run_prefix, self.plan_prefix) if p)

    def instance_name(self) -> str:
        """This instance's name, from the env var or `.local/instance.toml`."""
        return read_instance_name(
            self.instance_toml, fallback=self.agent, env_var=self.instance_env_var
        )


# --- running a role --------------------------------------------------------


def chat_environment(
    spec: AgentSpec,
    *,
    home: tuple[str, str] | None = None,
    base_path: str | None = None,
    bin_dir: Path | None = None,
) -> dict[str, str]:
    """`agentchat` reachable by name, speaking as this instance.

    The bin directory is the one holding the interpreter that runs the
    listener — in a `uv` project that is `.venv/bin`, where the `agentchat`
    console script is installed — so no deployment path is written down.

    `home` is the conversation being served. `agentchat send` writes it into
    whatever topic the run posts in as a root note, which is how an exchange
    outlives the run that started it.
    """
    directory = Path(sys.executable).parent if bin_dir is None else bin_dir
    environment = {AGENTCHAT_ENV_VARIABLE: str(spec.zulip_env)}
    if home is not None:
        environment[selfnote.HOME_VARIABLE] = str(selfnote.Conversation(*home))
    if directory.is_dir():
        base = os.environ.get("PATH", "") if base_path is None else base_path
        environment["PATH"] = os.pathsep.join([str(directory), base])
    return environment


def resolve_spec_role(
    spec: AgentSpec,
    role: str,
    *,
    profile_override: str | None = None,
    check_available: bool = True,
    config_path: Path | None = None,
    overlay_path: Path | None = None,
    home: tuple[str, str] | None = None,
) -> ResolvedAgent:
    """Resolve one role against the agent's config pair, with its handover.

    The config pair is an argument, not a fixed fact: a caller that owns its
    own pair (a test pointed at the `fake` harness) passes it, and nothing
    here can silently fall back to the committed config and launch a real,
    paid harness.
    """
    config, overlay = load_config(
        config_path or spec.agents_config,
        spec.agents_local_config if overlay_path is None else overlay_path,
    )
    agent = resolve_role(
        config, overlay, role,
        profile_override=profile_override,
        check_available=check_available,
    )
    environment = dict(agent.environment)
    if spec.extra_environment is not None:
        environment.update(spec.extra_environment(environment))
    environment.update(
        chat_environment(spec, home=home, base_path=environment.get("PATH"))
    )
    return replace(agent, environment=environment)


def run_role(
    spec: AgentSpec,
    role: str,
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    profile: str | None = None,
    transcript: Path | None = None,
    record: Path | None = None,
    home: tuple[str, str] | None = None,
    stream: bool = False,
    skip_permissions: bool = False,
    extra_args: list[str] | None = None,
    on_event=None,
) -> tuple[str, dict, int]:
    """Resolve `role`, run it once, and return output, record, and exit code.

    The tool grant is the role's own (`allowed_tools` in `agents.toml`). The
    remaining keyword arguments pass straight to `run_harness` for the agent
    that needs them (autolab's agcode budget, its permission bypass).
    """
    agent = resolve_spec_role(spec, role, profile_override=profile, home=home)
    result = run_harness(
        agent,
        prompt,
        cwd=cwd,
        timeout=timeout,
        allowed_tools=agent.allowed_tools,
        stream=stream,
        transcript_path=transcript,
        skip_permissions=skip_permissions,
        extra_args=extra_args,
        on_event=on_event,
    )
    run_record = {"schema": "ag.agent-run.v1", **result.meta}
    if record:
        write_run_record(record, request_id=record.stem, meta=result.meta)
    return result.output, run_record, result.exit_code


# --- the listener ----------------------------------------------------------


def log_only(spec: AgentSpec) -> bool:
    return "1" in (
        os.environ.get(spec.log_only_env_var, ""),
        os.environ.get(LOG_ONLY_ENV_VAR, ""),
    )


def topic_filter(spec: AgentSpec):
    """Sweep every topic in this instance's channel, the prefixes elsewhere."""
    prefixes = spec.sweep_prefixes

    def matches(channel: str, topic: str) -> bool:
        return channel == spec.instance_name() or (
            bool(prefixes) and topic.startswith(prefixes)
        )

    return matches


def _observe_message(client: ZulipClient, message: dict, self_id: int) -> None:
    """Passive DM handler: log, answer nothing."""
    if message.get("type") == "stream":
        place = f"channel={channel_name(message)!r} topic={message.get('subject')!r}"
    else:
        place = f"partners={dm_partners(message, self_id)}"
    log(
        f"message #{message.get('id')} from {message.get('sender_full_name')!r} "
        f"(id={message.get('sender_id')}, {place}): "
        f"{str(message.get('content', ''))[:200]!r}"
    )


def _observe_topic(channel: str, topic: str) -> None:
    log(f"observed sweep match {channel!r}/{topic!r}")


def listener_main(
    spec: AgentSpec,
    dispatch: Mapping[str, Callable[[ZulipClient, str, str], None]] | None = None,
    *,
    entrance: Callable[[ZulipClient, str, str], None] | None = None,
    dm_handler=None,
    on_mention: Callable[[ZulipClient, str, str], None] | None = None,
) -> None:
    """Run the pull-sweep listener for `spec` until interrupted.

    `dispatch` maps a topic prefix to `handler(client, channel, topic)`; the
    longest matching prefix wins. A swept topic matching no prefix is in the
    instance's own channel (that is what `topic_filter` admits) and goes to
    `entrance` — by default `agag.entrance.handle_entrance`, the front run
    that answers about the instance's work by reading the board.

    `dm_handler(client, message, self_id)` serves DMs on a side thread;
    without one DMs are logged and left. `on_mention(client, channel, topic)`
    enables `sweep_serve`'s mention route for an agent that is served by being
    named in topics it does not own (front's shape).

    Under `<AGENT>_ZULIP_LOG_ONLY=1` every route is replaced by a logger.
    """
    from .entrance import handle_entrance

    client = ZulipClient.from_env(spec.zulip_env)
    dm_client = ZulipClient.from_env(spec.zulip_env)
    routes = dict(dispatch or {})
    answer = entrance or (lambda c, ch, t: handle_entrance(spec, c, ch, t))
    passive = log_only(spec)

    def route(channel: str, topic: str) -> None:
        for prefix in sorted(routes, key=len, reverse=True):
            if topic.startswith(prefix):
                routes[prefix](client, channel, topic)
                return
        answer(client, channel, topic)

    if passive:
        topic_handler, dm_route, mention_route = _observe_topic, _observe_message, None
    else:
        topic_handler = route
        dm_route = dm_handler or _observe_message
        mention_route = (
            (lambda ch, t: on_mention(client, ch, t)) if on_mention is not None else None
        )
    threading.Thread(
        target=serve, args=(dm_client, dm_route), kwargs={"accept": is_dm_for_us},
        daemon=True,
    ).start()
    log(
        f"{spec.agent} zulip listener starting"
        f"{' (log only)' if passive else ''} "
        f"(pull sweep: all topics in {spec.instance_name()!r}, "
        f"prefixes {spec.sweep_prefixes} elsewhere, routes {sorted(routes)} + DM thread)"
    )
    try:
        sweep_serve(
            client, topic_handler, topic_filter=topic_filter(spec), on_mention=mention_route
        )
    except KeyboardInterrupt:
        log("stopped")


# --- the introduction ------------------------------------------------------


def intro_main(spec: AgentSpec) -> str:
    """Append the current introduction to `#agents` for this instance."""
    client = ZulipClient.from_env(spec.zulip_env)
    return post_intro(
        client, instance=spec.instance_name(), intro_path=spec.intro_path, root=spec.root
    )
