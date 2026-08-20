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

An agent wires this up with one call:

    from agag.intro import post_intro

    post_intro(client, instance="agforge-agstudio1",
               intro_path=ROOT / "params" / "intro.md", root=ROOT)
"""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

AGENTS_CHANNEL = "agents"
INTRO_TOPIC_PREFIX = "intro-"

__all__ = [
    "AGENTS_CHANNEL",
    "INTRO_TOPIC_PREFIX",
    "intro_text",
    "intro_topic",
    "post_intro",
    "revision",
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
    today: date | None = None,
    commit: str | None = None,
) -> str:
    """The committed Markdown plus this post's freshness stamp."""
    posted = today or date.today()
    current_revision = commit if commit is not None else revision(root)
    body = intro_path.read_text(encoding="utf-8").rstrip()
    return f"{body}\n\n---\nPosted: {posted.isoformat()}\nRevision: `{current_revision}`\n"


def post_intro(client, *, instance: str, intro_path: Path, root: Path) -> str:
    """Append this instance's current introduction to the shared board.

    Returns the posted text, so a caller can log or test what it announced.
    """
    text = intro_text(intro_path, root)
    client.send_to_channel(AGENTS_CHANNEL, intro_topic(instance), text)
    return text
