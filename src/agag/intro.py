"""Post an agent instance's committed introduction to the shared agents board.

Every standardized agent announces itself the same way: a fixed Markdown file
in its repository, appended to the `agents` channel under an
`intro-<instance>` topic with a date and revision stamp. That topic is
append-only history — nothing here deduplicates, so re-running after a
behavior change makes the newest introduction the easy one to find while the
older announcements stay readable.

The introduction is the *contract* another agent reads. It travels as content,
never as code: a consumer learns the entrance by reading this post, so no
routing vocabulary needs to be compiled into anyone else's guide.

`{instance}` in the file is replaced with this instance's name as it is
posted. The instance label is the host, which tracked files are not supposed
to carry (`devdocs/README_DEV.md`), and one introduction that any instance of
the agent can post is the better shape anyway.

An agent wires this up with one call:

    from agag.intro import post_intro

    post_intro(client, instance="agforge-agstudio1",
               intro_path=ROOT / "params" / "intro.md", root=ROOT)

The other side of the board is the harvest: `write_agents_md` collects the
latest post of every live `intro-*` topic and drops them, verbatim, into a
run's `tools/agents.md`. That is how an agent learns who else exists without
any routing vocabulary being compiled into its own guide — the posting side
and the reading side of one contract, so they live in one module.

Since `operation_room` p2 the post also carries a small **machine-readable
roster block** — the instance's own name, the Zulip name it is mentioned by,
the channel it answers every topic in, and the topic prefixes it sweeps
elsewhere. An outside observer (the operation room's state engine) cannot read
any of that: prefixes are compiled into an `AgentSpec` and the instance name
lives in that node's ignored `.local/instance.toml`. Guessing them produced
p1's two worst errors — 66 phantom stalled rows between them — so the roster
travels the way every other piece of routing vocabulary in this system does:
as posted content, on the contract that is already re-posted after a behavior
change.

The block is a fenced ```agag-roster``` section of `key: value` lines, which
keeps it out of the prose a human reads and makes it a single-pass parse.
`roster_block` writes one, `parse_roster` reads one, and they live beside each
other for the same reason the harvest does.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from agag.zulip import RESOLVED_TOPIC_PREFIX

AGENTS_CHANNEL = "agents"
INTRO_TOPIC_PREFIX = "intro-"
TOOLS_DIRNAME = "tools"
AGENTS_FILENAME = "agents.md"

#: The roster block's own version, so a reader can tell an old post from a
#: shape it does not understand. Bump it when a field changes meaning.
ROSTER_SCHEMA = "ag.agent-roster.v1"
#: The fence language. A fenced block is not prose and not a mention, which is
#: what keeps this out of a human's way and out of Zulip's notification path.
ROSTER_FENCE = "agag-roster"
#: A field with nothing to say. Absent and empty are different things to a
#: reader that must not invent a roster, so "none" is written down.
ROSTER_NONE = "-"
ROSTER_HEADING = "## Roster"
ROSTER_PREAMBLE = (
    "For an observer, not a reader. This is what routes messages to me: the "
    "name I am\nmentioned by, the channel where every topic is mine, and the "
    "topic prefixes I sweep\nanywhere I am subscribed. It is generated from "
    "my own configuration when I post."
)

HEADING = "# Other agents"
PREAMBLE = (
    "Each section below is one agent's own introduction, copied verbatim from\n"
    "the shared `#agents` board in Zulip. Talk to them with `agentchat`."
)
NO_AGENTS = (
    "No agent has introduced itself on the `#agents` board, so there is "
    "nobody to ask."
)

__all__ = [
    "AGENTS_CHANNEL",
    "AGENTS_FILENAME",
    "INTRO_TOPIC_PREFIX",
    "ROSTER_FENCE",
    "ROSTER_NONE",
    "ROSTER_SCHEMA",
    "Roster",
    "TOOLS_DIRNAME",
    "agents_file_path",
    "harvest_intros",
    "intro_text",
    "intro_topic",
    "parse_roster",
    "post_intro",
    "render_agents_md",
    "revision",
    "roster_block",
    "write_agents_md",
]


@dataclass(frozen=True)
class Roster:
    """How this instance is addressed and what it answers for.

    Every field is read off the running instance — `AgentSpec` for the
    prefixes and the channel, Zulip's own profile for the name and id — so a
    re-post is always current and nothing here is written by hand.

    `channel` is the channel whose *every* topic this instance serves
    (`agag.agent.topic_filter`: `channel == instance_name`). It is stated even
    when no such channel exists, because that is exactly what the listener
    matches on; a reader checks the realm rather than being told a comforting
    answer. Front is the live case — its instance is `front-agstudio1`, no
    channel of that name exists, and it is served by its `front-` prefix
    alone.
    """

    instance: str
    agent: str
    bot: str
    bot_id: int | None = None
    channel: str = ""
    prefixes: tuple[str, ...] = ()


def roster_block(roster: Roster) -> str:
    """The fenced `key: value` block a post carries for the observer."""
    lines = [
        f"schema: {ROSTER_SCHEMA}",
        f"instance: {roster.instance}",
        f"agent: {roster.agent}",
        f"bot: {roster.bot}",
        f"bot_id: {roster.bot_id if roster.bot_id is not None else ROSTER_NONE}",
        f"channel: {roster.channel or ROSTER_NONE}",
        f"prefixes: {', '.join(roster.prefixes) if roster.prefixes else ROSTER_NONE}",
    ]
    body = "\n".join(lines)
    return f"{ROSTER_HEADING}\n\n{ROSTER_PREAMBLE}\n\n```{ROSTER_FENCE}\n{body}\n```"


_ROSTER_PATTERN = re.compile(
    rf"^[ \t]*```[ \t]*{ROSTER_FENCE}[ \t]*$\n(.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def parse_roster(text: str) -> Roster | None:
    """The `Roster` an introduction declares, or None when it declares none.

    None is a real answer and the caller must keep it as one: an instance
    whose post carries no block is an instance whose routing is *unknown*, not
    one with no prefixes. A reader that defaults here re-creates the guessed
    roster this block exists to abolish.

    The **last** block wins, because an `intro-` topic is append-only and a
    quoted example in the prose above would otherwise outrank the real one.
    """
    matches = _ROSTER_PATTERN.findall(text or "")
    if not matches:
        return None
    fields: dict[str, str] = {}
    for line in matches[-1].splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    instance = fields.get("instance", "")
    if not instance:
        return None  # a block that cannot say who it is about says nothing
    raw_id = fields.get("bot_id", ROSTER_NONE)
    prefixes = fields.get("prefixes", ROSTER_NONE)
    channel = fields.get("channel", ROSTER_NONE)
    return Roster(
        instance=instance,
        agent=fields.get("agent", ""),
        bot=fields.get("bot", "") or instance,
        bot_id=int(raw_id) if raw_id.isdigit() else None,
        channel="" if channel == ROSTER_NONE else channel,
        prefixes=tuple(
            part.strip() for part in prefixes.split(",")
            if part.strip() and part.strip() != ROSTER_NONE
        ),
    )


def revision(root: Path) -> str:
    """The checked-out short revision of `root`, or an honest marker outside Git."""
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def intro_topic(instance: str) -> str:
    """The append-only topic this instance introduces itself in."""
    return f"{INTRO_TOPIC_PREFIX}{instance}"


def intro_text(
    intro_path: Path,
    root: Path,
    instance: str,
    today: date | None = None,
    commit: str | None = None,
    roster: Roster | None = None,
) -> str:
    """The committed Markdown, with `{instance}` filled in, plus the stamp.

    The roster block sits between the agent's own prose and the stamp: last,
    so it never interrupts what a human came to read, and inside the post, so
    it is re-stated every time the contract is re-posted. Without a `roster`
    the post is exactly what it was before.
    """
    posted = today or date.today()
    current_revision = commit if commit is not None else revision(root)
    body = intro_path.read_text(encoding="utf-8").rstrip().replace("{instance}", instance)
    if roster is not None:
        body = f"{body}\n\n{roster_block(roster)}"
    return f"{body}\n\n---\nPosted: {posted.isoformat()}\nRevision: `{current_revision}`\n"


def post_intro(
    client, *, instance: str, intro_path: Path, root: Path, roster: Roster | None = None
) -> str:
    """Append this instance's current introduction to the shared board.

    Returns the posted text, so a caller can log or test what it announced.
    """
    text = intro_text(intro_path, root, instance, roster=roster)
    client.send_to_channel(AGENTS_CHANNEL, intro_topic(instance), text)
    return text


def _agent_name(topic: str) -> str:
    return topic[len(INTRO_TOPIC_PREFIX):].strip() or topic


def harvest_intros(client) -> list[tuple[str, str]]:
    """`(agent name, latest intro body)` for every live `intro-*` topic.

    Resolved (`✔`) topics are skipped — retiring an agent's introduction is
    how it leaves the board — as is anything that is not an introduction.
    Sorted by agent name so two harvests of the same board are the same file.
    """
    entries: list[tuple[str, str]] = []
    for topic in client.channel_topics(client.stream_id(AGENTS_CHANNEL)):
        if topic.startswith(RESOLVED_TOPIC_PREFIX) or not topic.startswith(INTRO_TOPIC_PREFIX):
            continue
        history = client.topic_history(AGENTS_CHANNEL, topic, num_before=1)
        if not history:
            continue
        body = str(history[-1].get("content", "")).strip()
        if not body:
            continue
        entries.append((_agent_name(topic), body))
    return sorted(entries)


def render_agents_md(entries: list[tuple[str, str]], generated_at: datetime | None = None) -> str:
    """The whole file, by string operations. No model call on this path."""
    stamp = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    parts = [HEADING, "", PREAMBLE, "", f"Generated: {stamp.isoformat(timespec='seconds')}", ""]
    if not entries:
        # An empty board is a fact, not a crash: the run can read this and say
        # so, which is a better answer than a failed run.
        parts.append(NO_AGENTS)
    else:
        for name, body in entries:
            parts.extend([f"## {name}", "", body, ""])
    return "\n".join(parts).rstrip() + "\n"


def agents_file_path(workspace: Path) -> Path:
    """Where a run reads about the other agents, inside its own workspace."""
    return workspace / TOOLS_DIRNAME / AGENTS_FILENAME


def write_agents_md(client, workspace: Path, generated_at: datetime | None = None) -> Path:
    """Harvest the board and drop it in this run's `tools/`."""
    path = agents_file_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_agents_md(harvest_intros(client), generated_at), encoding="utf-8"
    )
    return path
