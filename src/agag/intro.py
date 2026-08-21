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
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from agag.zulip import RESOLVED_TOPIC_PREFIX

AGENTS_CHANNEL = "agents"
INTRO_TOPIC_PREFIX = "intro-"
TOOLS_DIRNAME = "tools"
AGENTS_FILENAME = "agents.md"

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
    "TOOLS_DIRNAME",
    "agents_file_path",
    "harvest_intros",
    "intro_text",
    "intro_topic",
    "post_intro",
    "render_agents_md",
    "revision",
    "write_agents_md",
]


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
) -> str:
    """The committed Markdown, with `{instance}` filled in, plus the stamp."""
    posted = today or date.today()
    current_revision = commit if commit is not None else revision(root)
    body = intro_path.read_text(encoding="utf-8").rstrip().replace("{instance}", instance)
    return f"{body}\n\n---\nPosted: {posted.isoformat()}\nRevision: `{current_revision}`\n"


def post_intro(client, *, instance: str, intro_path: Path, root: Path) -> str:
    """Append this instance's current introduction to the shared board.

    Returns the posted text, so a caller can log or test what it announced.
    """
    text = intro_text(intro_path, root, instance)
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
