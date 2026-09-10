"""One Markdown file as a title and a body, and back again.

A plan, a task description, a report — every one of them is a Markdown file
whose first heading is its title and whose remainder is its content. That
split predates this module: it lived in `agag.plane`, because the only
consumer stored the two halves in an issue's `name` and `description_html`.

A work record kept in **Zulip** stores the same two halves as Markdown, with
no HTML anywhere near them, and an agent that has no Plane credential must
not have to import the Plane client to split a heading off a file. So the
document rules live here, `agag.plane` re-exports them, and both storages
agree on what the title of a file is.
"""

from __future__ import annotations

import re

#: Plane's issue-name ceiling, kept as the shared limit: a title long enough
#: to be cut here is a title nobody reads to the end anyway, and one rule for
#: both storages means a document survives a move between them unchanged.
TITLE_LIMIT = 255

HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")

__all__ = ["HEADING", "TITLE_LIMIT", "DocumentError", "compose", "split"]


class DocumentError(ValueError):
    """The text is not a document — it has no line to take a title from."""


def split(text: str) -> tuple[str, str]:
    """Split one Markdown file into a title and a body.

    Title is the first heading line; without one, the first non-empty line.
    Everything else, in file order, is the body.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if match := HEADING.match(line):
            title = match.group("title")
            break
        if line.strip():
            title = line.strip()
            break
    else:
        raise DocumentError("the file is empty")
    body = "\n".join(lines[:index] + lines[index + 1:]).strip()
    return title[:TITLE_LIMIT], body


def compose(title: str, body: str | None) -> str:
    """Invert `split`: the title as a `#` heading, then the body."""
    text = (body or "").strip()
    return f"# {title}\n\n{text}\n" if text else f"# {title}\n"
