"""A small Plane client, shared by the agents that mirror chat into Plane.

Scope is deliberately narrow: credentials, the HTTP shape, projects, states,
issues keyed on Plane's own `(external_source, external_id)` pair, and the
Markdown ↔ issue conversion around them. Anything that is one agent's own
policy stays with that agent — which work to execute next, sub-work
generation keys, what a label means, whether a project is eligible for
anything.

`external_source` is a required argument on every keyed call. It is the
namespace that keeps two agents' external ids from colliding in one Plane
workspace, so it is never defaulted.

Known Plane CE v1.4.1 behaviors this encodes:

- an unknown `(external_source, external_id)` pair answers **404**, not an
  empty list
- `?parent=` on the issue list is ignored; children must be filtered
  client-side
- `description_stripped` is sometimes None even when the html has content,
  so `html_to_text` inverts `description_html` instead of trusting it
- issue listing needs cursor pagination
"""

from __future__ import annotations

import html
import json
import os
import re
import shlex
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

TITLE_LIMIT = 255
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(?P<title>.+?)\s*#*\s*$")

__all__ = [
    "PlaneConfig",
    "PlaneError",
    "add_comment",
    "compose_document",
    "create_project",
    "description_html",
    "ensure_issue",
    "find_issue_by_external",
    "find_project",
    "html_to_text",
    "issue_label",
    "labels_by_name",
    "list_issues",
    "list_projects",
    "load_plane_config",
    "normalized_name",
    "plane_identifier",
    "read_env",
    "split_document",
    "starting_state_id",
    "state_groups",
    "state_id_for_group",
    "update_issue",
]


class PlaneError(RuntimeError):
    """One Plane operation could not complete."""


@dataclass(frozen=True)
class PlaneConfig:
    url: str
    api_key: str
    workspace: str


# --- credentials -----------------------------------------------------------


def read_env(path: Path) -> dict[str, str]:
    """Read `KEY=value` lines without sourcing shell code."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PlaneError(f"cannot read configuration {path}: {error}") from error
    values: dict[str, str] = {}
    for line in lines:
        tokens = shlex.split(line, comments=True)
        if len(tokens) == 1 and "=" in tokens[0]:
            key, value = tokens[0].split("=", 1)
            values[key] = value
    return values


def load_plane_config(path: Path) -> PlaneConfig:
    values = read_env(path)
    required = ("PLANE_URL", "PLANE_API_KEY", "PLANE_WORKSPACE_SLUG")
    if missing := [key for key in required if not values.get(key)]:
        raise PlaneError(f"{path} is missing {', '.join(missing)}")
    return PlaneConfig(
        os.environ.get("PLANE_URL", values["PLANE_URL"]).rstrip("/"),
        os.environ.get("PLANE_API_KEY", values["PLANE_API_KEY"]),
        os.environ.get("PLANE_WORKSPACE_SLUG", values["PLANE_WORKSPACE_SLUG"]),
    )


# --- HTTP ------------------------------------------------------------------


def _request_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: dict | None = None,
    timeout: float = 30,
) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"detail": raw.decode("utf-8", "replace")[:200]}
        return error.code, payload
    except (OSError, TimeoutError) as error:
        raise PlaneError(f"{method} {url} failed: {error}") from error


def _rows(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return [row for row in payload["results"] if isinstance(row, dict)]
    raise PlaneError("API response did not contain a result list")


def _headers(config: PlaneConfig) -> dict[str, str]:
    return {"X-API-Key": config.api_key, "Content-Type": "application/json"}


def _workspace_base(config: PlaneConfig) -> str:
    return f"{config.url}/api/v1/workspaces/{urllib.parse.quote(config.workspace, safe='')}"


def _project_base(config: PlaneConfig, project_id: str) -> str:
    return f"{_workspace_base(config)}/projects/{urllib.parse.quote(project_id, safe='')}"


# --- documents -------------------------------------------------------------


def normalized_name(value: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", value.lower()))


def split_document(text: str) -> tuple[str, str]:
    """Split one Markdown file into an issue title and description.

    Title is the first heading line; without one, the first non-empty line.
    Everything else, in file order, is the description.
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
        raise PlaneError("the file is empty")
    description = "\n".join(lines[:index] + lines[index + 1 :]).strip()
    return title[:TITLE_LIMIT], description


def description_html(description: str) -> str:
    return f"<p>{html.escape(description).replace(chr(10), '<br>')}</p>"


def html_to_text(value: str | None) -> str:
    """Recover plain text from `description_html`."""
    text = re.sub(r"<br\s*/?>", "\n", value or "")
    text = re.sub(r"</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def compose_document(name: str, description_html_value: str | None) -> str:
    """Invert `split_document`: title as `# heading`, then the description."""
    body = html_to_text(description_html_value)
    return f"# {name}\n\n{body}\n" if body else f"# {name}\n"


# --- projects --------------------------------------------------------------


def list_projects(config: PlaneConfig) -> list[dict]:
    status, payload = _request_json(
        "GET", f"{_workspace_base(config)}/projects/?per_page=100", headers=_headers(config)
    )
    if status != 200:
        raise PlaneError(f"Plane project list returned HTTP {status}: {payload!r}")
    return _rows(payload)


def find_project(config: PlaneConfig, name: str) -> dict | None:
    wanted = normalized_name(name)
    return next(
        (
            row
            for row in list_projects(config)
            if normalized_name(str(row.get("name", ""))) == wanted and row.get("id")
        ),
        None,
    )


def plane_identifier(name: str) -> str:
    parts = [part for part in re.split(r"[^a-z0-9]+", name.lower()) if part]
    return "".join(part if part.isdigit() else part[0] for part in parts).upper()[:12]


def create_project(config: PlaneConfig, name: str, description: str = "") -> dict:
    """Create one project, retrying past identifier collisions.

    `description` is the caller's: what a marker in it means is the calling
    agent's policy, never this client's.
    """
    used = {str(row.get("identifier", "")).upper() for row in list_projects(config)}
    base_identifier = plane_identifier(name) or "P"
    for suffix in range(1, 101):
        tail = "" if suffix == 1 else str(suffix)
        identifier = f"{base_identifier[:12 - len(tail)]}{tail}"
        if identifier in used:
            continue
        status, payload = _request_json(
            "POST",
            f"{_workspace_base(config)}/projects/",
            headers=_headers(config),
            body={"name": name, "identifier": identifier, "description": description},
            timeout=60,
        )
        if status in {200, 201} and isinstance(payload, dict) and payload.get("id"):
            return payload
        if status in {409, 422}:
            if existing := find_project(config, name):
                return existing
            used.update(str(row.get("identifier", "")).upper() for row in list_projects(config))
            continue
        raise PlaneError(f"Plane project create returned HTTP {status}: {payload!r}")
    raise PlaneError("Plane identifier collision retries exhausted")


# --- states ----------------------------------------------------------------


def _states(config: PlaneConfig, project_id: str) -> list[dict]:
    status, payload = _request_json(
        "GET", f"{_project_base(config, project_id)}/states/",
        headers=_headers(config), timeout=60,
    )
    if status != 200:
        raise PlaneError(f"Plane state list returned HTTP {status}: {payload!r}")
    return _rows(payload)


def starting_state_id(config: PlaneConfig, project_id: str) -> str:
    """The project's actionable initial state, from its live vocabulary:
    ready → todo → the first `unstarted` state → backlog."""
    rows = _states(config, project_id)
    by_name = {str(row.get("name", "")).lower(): row for row in rows}
    state = by_name.get("ready") or by_name.get("todo")
    if state is None:
        state = next((row for row in rows if row.get("group") == "unstarted"), None)
    if state is None:
        state = by_name.get("backlog")
    if not state or not state.get("id"):
        raise PlaneError("Plane project has no usable starting state")
    return str(state["id"])


def state_groups(config: PlaneConfig, project_id: str) -> dict[str, str]:
    """State id -> group (`backlog`/`unstarted`/`started`/`completed`/`cancelled`)."""
    return {
        str(row["id"]): str(row.get("group", ""))
        for row in _states(config, project_id)
        if row.get("id")
    }


def state_id_for_group(config: PlaneConfig, project_id: str, group: str) -> str:
    for state_id, state_group in state_groups(config, project_id).items():
        if state_group == group:
            return state_id
    raise PlaneError(f"Plane project has no state in group {group!r}")


# --- labels ----------------------------------------------------------------


def labels_by_name(config: PlaneConfig, project_id: str) -> dict[str, str]:
    """Lowercased label name -> label id, for one project."""
    status, payload = _request_json(
        "GET", f"{_project_base(config, project_id)}/labels/?per_page=100",
        headers=_headers(config), timeout=60,
    )
    if status != 200:
        raise PlaneError(f"Plane label list returned HTTP {status}: {payload!r}")
    return {
        str(row.get("name", "")).lower(): str(row["id"]) for row in _rows(payload) if row.get("id")
    }


def create_label(config: PlaneConfig, project_id: str, name: str) -> str | None:
    """Create one label and return its id, or None if the name was taken."""
    status, payload = _request_json(
        "POST", f"{_project_base(config, project_id)}/labels/",
        headers=_headers(config), body={"name": name}, timeout=60,
    )
    if status in {200, 201} and isinstance(payload, dict) and payload.get("id"):
        return str(payload["id"])
    if status not in {400, 409, 422}:
        raise PlaneError(f"Plane label create returned HTTP {status}: {payload!r}")
    return None


# --- issues ----------------------------------------------------------------


def find_issue_by_external(
    config: PlaneConfig, project_id: str, external_source: str, external_id: str
) -> dict | None:
    """One issue by its `(external_source, external_id)` pair, or None.

    Plane answers with the issue object itself, and **404** when the pair is
    unknown. This is the duplicate guard; no local marker file is involved.
    """
    query = urllib.parse.urlencode(
        {"external_id": external_id, "external_source": external_source}
    )
    status, payload = _request_json(
        "GET", f"{_project_base(config, project_id)}/issues/?{query}",
        headers=_headers(config),
    )
    if status == 404:
        return None
    if status != 200:
        raise PlaneError(f"Plane issue lookup returned HTTP {status}: {payload!r}")
    if isinstance(payload, dict) and payload.get("id"):
        return payload
    rows = _rows(payload) if isinstance(payload, (list, dict)) else []
    return rows[0] if rows else None


def list_issues(config: PlaneConfig, project_id: str) -> list[dict]:
    """Every issue in the project, following Plane's cursor pagination."""
    rows: list[dict] = []
    cursor: str | None = None
    while True:
        query = "per_page=100" + (
            f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else ""
        )
        status, payload = _request_json(
            "GET", f"{_project_base(config, project_id)}/issues/?{query}",
            headers=_headers(config),
        )
        if status != 200:
            raise PlaneError(f"Plane issue list returned HTTP {status}: {payload!r}")
        rows.extend(_rows(payload))
        if not (isinstance(payload, dict) and payload.get("next_page_results")):
            return rows
        cursor = str(payload.get("next_cursor", ""))
        if not cursor:
            return rows


def ensure_issue(
    config: PlaneConfig,
    project_id: str,
    *,
    name: str,
    description: str,
    state: str,
    external_source: str,
    external_id: str,
    parent: str | None = None,
    labels: list[str] | None = None,
) -> tuple[dict, bool]:
    """Return `(issue, created)` for one external key, creating at most one.

    `labels` is opt-in. An agent that attaches none gets an issue no
    label-driven automation will pick up, which for some agents is the point.
    """
    if not name.strip():
        raise PlaneError("issue title must not be empty")
    if existing := find_issue_by_external(config, project_id, external_source, external_id):
        return existing, False
    body = {
        "name": name.strip(),
        "description_html": description_html(description),
        "state": state,
        "external_source": external_source,
        "external_id": external_id,
    }
    if parent:
        body["parent"] = parent
    if labels:
        body["labels"] = labels
    status, payload = _request_json(
        "POST", f"{_project_base(config, project_id)}/issues/",
        headers=_headers(config), body=body, timeout=60,
    )
    if status in {200, 201} and isinstance(payload, dict):
        return payload, True
    if status == 409:
        # Plane reports the winner of a race in the error body; a re-read is
        # still needed to get its human-readable sequence id.
        if existing := find_issue_by_external(
            config, project_id, external_source, external_id
        ):
            return existing, False
        if isinstance(payload, dict) and payload.get("id"):
            return {"id": payload["id"]}, False
    raise PlaneError(f"Plane issue create returned HTTP {status}: {payload!r}")


def update_issue(config: PlaneConfig, project_id: str, issue_id: str, body: dict) -> dict:
    status, payload = _request_json(
        "PATCH",
        f"{_project_base(config, project_id)}/issues/{urllib.parse.quote(issue_id, safe='')}/",
        headers=_headers(config), body=body, timeout=60,
    )
    if status != 200 or not isinstance(payload, dict):
        raise PlaneError(f"Plane issue update returned HTTP {status}: {payload!r}")
    return payload


def add_comment(config: PlaneConfig, project_id: str, issue_id: str, text: str) -> dict:
    """Post one plain-text comment on an issue."""
    status, payload = _request_json(
        "POST",
        f"{_project_base(config, project_id)}/issues/"
        f"{urllib.parse.quote(issue_id, safe='')}/comments/",
        headers=_headers(config), body={"comment_html": description_html(text)}, timeout=60,
    )
    if status in {200, 201} and isinstance(payload, dict):
        return payload
    raise PlaneError(f"Plane comment create returned HTTP {status}: {payload!r}")


def issue_label(project: dict, issue: dict) -> str:
    """The human-readable `PA-7` form, falling back to the raw id."""
    identifier = str(project.get("identifier", "")).strip()
    sequence = issue.get("sequence_id")
    if identifier and sequence is not None:
        return f"{identifier}-{sequence}"
    return str(issue.get("id", "?"))
