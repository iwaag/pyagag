"""`agproject`: open, inspect and continue a project or a study.

A **project** or a **study** is one `pj-<slug>` channel and what it points
at: the channel itself (filed in a folder of its own, with the people and
agents who work there subscribed), one document — the goal (`goal`) or the
research plan (`researchplan-<slug>`) — and a workspace that autolab
prepares on request in `workplan-setup-<slug>`. This module makes and reads
those artifacts. It is mechanical: which study to open, what its plan says,
and whether an existing one will do are the caller's judgement.

    agproject open <slug> --kind project|study --doc <file>
    agproject status <slug>
    agproject plan <study slug> --doc <file> [--stem <stem>]

**`open` creates or continues.** Run against a slug nobody uses, it creates
everything; run again — after a failure half way, after a restart, or on a
study somebody set up earlier — it finds what exists and does only what is
missing: a subscriber who is not there, a document that was never posted, a
setup request that was never made. It never makes a second channel, a
second document or a second setup request. Its output (and `--json`) names
the channel, the document and setup messages by id, and what remains.

**The setup request is machine-readable.** Besides the words autolab's
planner reads, it carries a fenced `ag-setup` block (`ag.project-setup.v1`)
naming the pattern, the slug and the document by message id, so autolab can
lay the workspace out before any model reads the request — a study never
gets an ordinary project's scaffolding first. autolab answers with a
`study layout established: …` line naming the repository and the commit;
`status` reads that line back and says `ready` only then. An accepted
request is **pending** until that answer exists: the caller ends its run and
is served again when autolab's reply names it (the root note this module
writes before the request says whose conversation it is).

Who is subscribed is resolved explicitly, never assumed from the caller:
the realm's human owners, the agent that runs routines and the agent that
prepares workspaces (both read off the `#agents` board by the topics they
answer), the caller, and the board reader the realm's relay reads with
(`AGAG_BOARD_READER`, default the `Opsroom Observer` account).

The channel is created with the **provisioner** credential this process is
given (`AGAG_PROVISIONER_ENV`, else `AGAG_ZULIP_ADMIN_ENV`); everything said
in it is posted as the caller, under `AGENTCHAT_ZULIP_ENV`. Nothing here
starts work: no `workrun-` topic is opened and no routine is run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .intro import harvest_intros, parse_roster
from .selfnote import Conversation, home_from_environment, is_selfnote, parse_rootchat, rootchat_note
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, ZulipError, topic_history_across_resolve

PROJECT_CHANNEL_PREFIX = "pj-"
GOAL_TOPIC = "goal"
PLAN_TOPIC_PREFIX = "researchplan-"
SETUP_TOPIC_PREFIX = "workplan-setup-"
KINDS = ("project", "study")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}$")

SETUP_FENCE = "ag-setup"
SETUP_SCHEMA = "ag.project-setup.v1"
#: The line autolab adds to its answer once a study's workspace exists; the
#: one thing `status` accepts as "the workspace is there".
ESTABLISHED_RE = re.compile(
    r"study layout established: `?main/`? = `?(?P<repository>[^\s`]+)`? at `?(?P<revision>[0-9a-f]{7,40})`?"
)

PROVISIONER_VARIABLE = "AGAG_PROVISIONER_ENV"
ADMIN_VARIABLE = "AGAG_ZULIP_ADMIN_ENV"
BOARD_READER_VARIABLE = "AGAG_BOARD_READER"
DEFAULT_BOARD_READER = "Opsroom Observer"
GITEA_URL_VARIABLE = "AGAG_GITEA_URL"
GITEA_ORG_VARIABLE = "AGAG_GITEA_ORG"
DEFAULT_GITEA_ORG = "autodev"
#: The prefixes that say which agent on the board does what. A routine's
#: runs are `routinerun-` topics; a workspace is prepared in `workplan-`.
RUNNER_PREFIX = "routinerun-"
WORKSPACE_PREFIX = "workplan-"
HISTORY = 200

__all__ = [
    "ESTABLISHED_RE", "GOAL_TOPIC", "KINDS", "PLAN_TOPIC_PREFIX", "PROJECT_CHANNEL_PREFIX", "ProjectError",
    "ProjectState", "SETUP_FENCE", "SETUP_SCHEMA", "SETUP_TOPIC_PREFIX", "board_agents", "board_reader",
    "established_line", "inspect_project", "main", "open_project", "parse_setup", "plan_topic", "project_channel",
    "project_principals", "provisioner", "run", "setup_block", "setup_request", "write_plan",
]


class ProjectError(RuntimeError):
    """The project cannot be opened, read or continued as asked."""


def project_channel(slug: str) -> str:
    return f"{PROJECT_CHANNEL_PREFIX}{slug}"


def plan_topic(stem: str) -> str:
    return f"{PLAN_TOPIC_PREFIX}{stem}"


def doc_topic(slug: str, kind: str) -> str:
    return GOAL_TOPIC if kind == "project" else plan_topic(slug)


def setup_topic(slug: str) -> str:
    return f"{SETUP_TOPIC_PREFIX}{slug}"


def check_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ProjectError(f"{slug!r} is not a project slug (2-39 lowercase letters, digits and dashes)")
    return slug


def channel_link(client, stream_id: int, name: str) -> str:
    base = str(getattr(client, "base_url", "")).rstrip("/")
    return f"{base}/#narrow/channel/{stream_id}-{name}"


# --- who is who --------------------------------------------------------------


def provisioner(path: str | None = None) -> ZulipClient:
    """The realm-admin client channels are created with."""
    chosen = path or os.environ.get(PROVISIONER_VARIABLE) or os.environ.get(ADMIN_VARIABLE)
    if not chosen:
        raise ProjectError(f"no provisioner credential: set {PROVISIONER_VARIABLE} (or {ADMIN_VARIABLE}) "
                           "to the provisioner's env file; this process cannot create channels without it")
    env = Path(chosen)
    if not env.is_file():
        raise ProjectError(f"no provisioner credential at {env}")
    return ZulipClient.from_env(env)


def _self(client: ZulipClient | None) -> ZulipClient:
    if client is not None:
        return client
    from .chat import client_from_environment

    return client_from_environment()


def board_agents(client) -> dict[str, int]:
    """`{"runner": id, "workspace": id}` for the agents on `#agents` whose
    introductions say they answer `routinerun-` and `workplan-` topics.
    Missing ones are simply absent — the caller says so."""
    found: dict[str, int] = {}
    for _, body in harvest_intros(client):
        roster = parse_roster(body)
        if roster is None or roster.bot_id is None:
            continue
        prefixes = tuple(roster.prefixes or ())
        if RUNNER_PREFIX in prefixes:
            found.setdefault("runner", int(roster.bot_id))
        if WORKSPACE_PREFIX in prefixes:
            found.setdefault("workspace", int(roster.bot_id))
    return found


def board_reader(admin, environ=None) -> int | None:
    """The account the realm's relay reads the boards with: a user id or a
    full name in `AGAG_BOARD_READER`, default `Opsroom Observer`."""
    environ = os.environ if environ is None else environ
    wanted = str(environ.get(BOARD_READER_VARIABLE) or DEFAULT_BOARD_READER).strip()
    if not wanted or wanted.lower() == "none":
        return None
    if wanted.isdigit():
        return int(wanted)
    for user in admin.users():
        if user.get("full_name") == wanted and user.get("is_active", True):
            return int(user["user_id"])
    return None


def project_principals(admin, client) -> dict[str, list[int]]:
    """Who belongs in a project channel, by why: the realm's owners, the
    routine runner, the workspace agent, the caller and the board reader."""
    board = board_agents(client)
    roles: dict[str, list[int]] = {"owners": sorted(admin.realm_owners())}
    roles["runner"] = [board["runner"]] if "runner" in board else []
    roles["workspace"] = [board["workspace"]] if "workspace" in board else []
    roles["caller"] = [int(client.whoami()["user_id"])]
    reader = board_reader(admin)
    roles["board reader"] = [reader] if reader is not None else []
    return roles


def _flat(roles: dict[str, list[int]]) -> list[int]:
    return sorted({user for users in roles.values() for user in users})


# --- the setup request ---------------------------------------------------------


def setup_block(fields: dict[str, object]) -> str:
    lines = [f"schema: {SETUP_SCHEMA}"] + [f"{key}: {value}" for key, value in fields.items() if value not in (None, "")]
    return f"```{SETUP_FENCE}\n" + "\n".join(lines) + "\n```"


_BLOCK = re.compile(r"```" + SETUP_FENCE + r"[ \t]*\n(?P<body>.*?)\n```", re.DOTALL)


def parse_setup(content) -> dict[str, str] | None:
    """The `ag-setup` block of a post, as a dict, or None. Only a block that
    names this schema counts: a quoted or foreign block is not a request."""
    match = _BLOCK.search(str(content or ""))
    if not match:
        return None
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    return fields if fields.get("schema") == SETUP_SCHEMA else None


def established_line(repository: str, revision: str, *, extra: str = "") -> str:
    """autolab's answer line: the workspace exists, at this commit."""
    return f"study layout established: `main/` = `{repository}` at `{revision}`" + (f" — {extra}" if extra else "")


def _origin_word(home: Conversation | None) -> str:
    return "argue" if home is not None and home.channel == "argue" else "conversation"


def _origin_line(home: Conversation | None) -> str:
    return f"Opened from {_origin_word(home)} **#{home.channel} › {home.topic}**." if home else "Opened by hand (no conversation named)."


def setup_request(slug: str, kind: str, doc_id: int, home: Conversation | None, *, about: str = "") -> str:
    """What autolab is asked, in `workplan-setup-<slug>`: prepare, never run."""
    where = f"#{project_channel(slug)} › {doc_topic(slug, kind)}"
    origin = f" It grew out of {_origin_word(home)} #{home.channel} › {home.topic}." if home else ""
    if kind == "study":
        words = (
            "Set it up on the **study** pattern (`autolab doc patterns`). The block below is read before any "
            "planning: the workspace marker (`README_PROJECT.md`) and `main/` — the internal knowledge repository "
            "on the standard route, holding `README.md`, the research plan as `RESEARCHPLAN.md`, `methods/` and "
            "`reports/` with its `INDEX.md` — are laid out for you. Check that layout, make `main/README.md` say what "
            "this study's knowledge is and how its index is kept, and nothing more; no `publish/` yet — a publication "
            "repository is supplied by the developer when one is wanted."
        )
        document = "RESEARCHPLAN.md"
    else:
        words = ("Set it up as a plain project: `main/` on the standard internal repository route, and nothing "
                 "else unless the goal below plainly needs another folder.")
        document = "GOAL.md"
    block = setup_block({
        "pattern": kind,
        "slug": slug,
        "channel": project_channel(slug),
        "document": f"{doc_topic(slug, kind)} #{doc_id}",
        "document_file": f"main/{document}",
        "knowledge": "main" if kind == "study" else "",
        "publish": "none" if kind == "study" else "",
        "about": about.replace("\n", " ").strip(),
        "origin": f"{home.channel}/{home.topic}" + (f" #{home.anchor}" if home and home.anchor else "") if home else "",
    })
    return (
        f"Please prepare the workspace for the {kind} `{slug}` — **setup only, no research and no development, "
        f"and no mission to plan**: reply when the folders and repositories exist.{origin}\n\n"
        f"{words}\n\n"
        f"The document is {where} (message {doc_id}); it goes into `main/{document}` unchanged, and "
        f"`README_PROJECT.md` names this channel, that topic and where the {kind} came from, so the folder and the "
        f"channel point at each other. Commit and push `main/`. Reply with what exists and where — the repository "
        f"and its commit; nothing is started by this post.\n\n{block}"
    )


# --- reading what exists ---------------------------------------------------------


@dataclass
class ProjectState:
    slug: str
    channel: str
    exists: bool = False
    archived: bool = False
    stream_id: int | None = None
    folder_id: int | None = None
    description: str = ""
    kind: str = ""
    subscribers: list[int] = field(default_factory=list)
    missing: dict[str, list[int]] = field(default_factory=dict)
    doc_topic: str = ""
    doc_id: int | None = None
    setup_topic: str = ""
    setup_id: int | None = None
    setup_structured: bool = False
    setup_home: str = ""
    answers: list[dict] = field(default_factory=list)
    repository: str = ""
    revision: str = ""
    gitea: dict = field(default_factory=dict)
    state: str = "absent"
    remaining: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _kind_from(description: str) -> str:
    match = re.search(r"\[AUTO\] project: [^;]+; (?P<kind>project|study)\b", description or "")
    return match.group("kind") if match else ""


def _is_ack(content) -> bool:
    from .agent import is_ack

    return is_ack(str(content or ""))


def _first_speech(history: list[dict]) -> dict | None:
    return next((m for m in history if not is_selfnote(m.get("content"))), None)


def gitea_base(environ=None) -> str:
    """The host's Gitea: `AGAG_GITEA_URL`, else the host the shared context
    catalog is served from (`agrefs`' host setting). Empty when unknown."""
    environ = os.environ if environ is None else environ
    if GITEA_URL_VARIABLE in environ:  # set, even empty, is the answer ("" = do not look)
        return str(environ.get(GITEA_URL_VARIABLE) or "").strip().rstrip("/")
    try:
        from . import refs

        import tomllib

        data = tomllib.loads(refs.host_config_path(environ).read_text(encoding="utf-8"))
        url = str((data.get("catalog") or {}).get("url") or "")
    except Exception:  # noqa: BLE001 - an unknown host is a valid answer
        return ""
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""


def gitea_head(slug: str, *, environ=None, timeout: float = 15) -> dict:
    """`{"repository": url, "revision": sha}` of the study's internal `main`
    repository on the host's Gitea, or `{"error": …}`. Read-only, no token:
    the standard route's repositories are readable."""
    environ = os.environ if environ is None else environ
    base = gitea_base(environ)
    if not base:
        return {"error": "the host's Gitea is unknown (set AGAG_GITEA_URL)"}
    org = str(environ.get(GITEA_ORG_VARIABLE) or DEFAULT_GITEA_ORG)
    api = f"{base}/api/v1/repos/{urllib.parse.quote(org)}/{urllib.parse.quote(slug)}"
    repository = f"{base}/{org}/{slug}.git"
    try:
        with urllib.request.urlopen(api, timeout=timeout) as response:
            info = json.loads(response.read() or b"{}")
        branch = str(info.get("default_branch") or "main")
        with urllib.request.urlopen(f"{api}/branches/{urllib.parse.quote(branch)}", timeout=timeout) as response:
            head = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"repository": repository, "exists": False}
        return {"repository": repository, "error": f"HTTP {error.code}"}
    except (OSError, ValueError) as error:
        return {"repository": repository, "error": str(error)}
    sha = str(((head.get("commit") or {}).get("id")) or "")
    return {"repository": repository, "exists": True, "branch": branch, "revision": sha[:12], "empty": not sha}


def inspect_project(slug: str, client, *, admin=None, kind: str | None = None, check_gitea: bool = True,
                    environ=None) -> ProjectState:
    """Everything that exists for `pj-<slug>`, read from the realm (and the
    host's Gitea), and what is still missing. Writes nothing."""
    check_slug(slug)
    name = project_channel(slug)
    state = ProjectState(slug=slug, channel=name)
    reader = admin or client
    row = next((r for r in reader.channels(include_archived=True) if r.get("name") == name), None)
    if row is None:
        state.remaining = ["create the channel", "post the document", "request the workspace setup"]
        return state
    state.exists = True
    state.archived = bool(row.get("is_archived"))
    state.stream_id = int(row["stream_id"])
    state.folder_id = row.get("folder_id")
    state.description = str(row.get("description") or "")
    state.kind = _kind_from(state.description) or kind or "study"
    if state.archived:
        state.state = "archived"
        state.remaining = ["the channel is archived: choose another slug or unarchive it by hand"]
        return state
    state.subscribers = sorted(int(u) for u in reader.channel_subscribers(state.stream_id))
    if admin is not None:
        wanted = project_principals(admin, client)
        state.missing = {role: [u for u in users if u not in state.subscribers]
                         for role, users in wanted.items() if any(u not in state.subscribers for u in users)}
    state.doc_topic = doc_topic(slug, state.kind)
    doc = _first_speech(topic_history_across_resolve(client, name, state.doc_topic, HISTORY))
    if doc is None and state.kind == "study":
        # An older study may keep its plan under another stem.
        for topic in client.channel_topics(state.stream_id):
            bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
            if bare.startswith(PLAN_TOPIC_PREFIX):
                doc = _first_speech(client.topic_history(name, topic, num_before=HISTORY))
                if doc is not None:
                    state.doc_topic = bare
                    break
    state.doc_id = int(doc["id"]) if doc else None
    state.setup_topic = setup_topic(slug)
    history = topic_history_across_resolve(client, name, state.setup_topic, HISTORY)
    request = _first_speech(history)
    if request is not None:
        state.setup_id = int(request["id"])
        state.setup_structured = parse_setup(request.get("content")) is not None
        requester = int(request.get("sender_id") or 0)
        for message in history:
            if message.get("sender_id") == requester:
                home = parse_rootchat(message.get("content"))
                if home is not None:
                    state.setup_home = str(home)
                    break
        for message in history:
            if int(message.get("id", 0)) <= state.setup_id or is_selfnote(message.get("content")):
                continue
            if message.get("sender_id") == requester or _is_ack(message.get("content")):
                continue  # an acknowledgement says a serving started, not what it found
            text = str(message.get("content") or "")
            found = ESTABLISHED_RE.search(text)
            state.answers.append({"id": int(message["id"]), "sender": str(message.get("sender_full_name") or ""),
                                  "established": bool(found)})
            if found:
                state.repository, state.revision = found.group("repository"), found.group("revision")
    if check_gitea and state.kind == "study":
        state.gitea = gitea_head(slug, environ=environ)
    _judge(state)
    return state


def _judge(state: ProjectState) -> None:
    remaining: list[str] = []
    if state.missing:
        remaining.append("subscribe " + ", ".join(f"{role} {users}" for role, users in state.missing.items()))
    if state.doc_id is None:
        remaining.append(f"post the document in {state.doc_topic}")
    if state.setup_id is None and state.gitea.get("exists") and not state.gitea.get("empty"):
        # Set up by hand before this tool: the repository is the witness,
        # and asking autolab to set it up again would only be noise.
        state.state = "ready"
        state.repository = str(state.gitea.get("repository") or "")
        state.revision = str(state.gitea.get("revision") or "")
    elif state.setup_id is None:
        remaining.append(f"request the workspace setup in {state.setup_topic}")
        state.state = "document-posted" if state.doc_id else "channel-only"
    elif state.repository:
        state.state = "ready"
    elif state.answers:
        # An answer without the established line: an older setup, or a
        # question back. For an older (prose) setup the repository on Gitea
        # is the other witness; a structured one is ready only by the line —
        # the repository is made before the planner has even read the request.
        if not state.setup_structured and state.gitea.get("exists") and not state.gitea.get("empty"):
            state.state = "ready"
            state.repository = state.repository or str(state.gitea.get("repository") or "")
            state.revision = state.revision or str(state.gitea.get("revision") or "")
        else:
            state.state = "answered"
            remaining.append(f"read autolab's answer in {state.setup_topic} (#{state.answers[-1]['id']}): "
                             "the workspace is not confirmed")
    else:
        state.state = "setup-pending"
        remaining.append(f"wait for autolab's answer in {state.setup_topic}; it names the requester when it comes")
    state.remaining = remaining


def _print_state(state: ProjectState, out) -> None:
    if not state.exists:
        print(f"#{state.channel} does not exist", file=out)
        return
    print(f"#{state.channel} (stream {state.stream_id}, folder {state.folder_id}) — {state.kind}, state: {state.state}",
          file=out)
    print(f"  document: {state.doc_topic} " + (f"#{state.doc_id}" if state.doc_id else "(none)"), file=out)
    if state.setup_id:
        print(f"  setup: {state.setup_topic} #{state.setup_id}"
              f"{' (ag-setup block)' if state.setup_structured else ' (prose only, no ag-setup block)'}"
              f"{f', return to {state.setup_home}' if state.setup_home else ''}", file=out)
        for answer in state.answers:
            print(f"    answer #{answer['id']} by {answer['sender']}{' — established' if answer['established'] else ''}",
                  file=out)
    else:
        print(f"  setup: {state.setup_topic} (not requested)", file=out)
    if state.repository:
        print(f"  main: {state.repository} at {state.revision}", file=out)
    if state.gitea:
        print(f"  gitea: {json.dumps(state.gitea, sort_keys=True)}", file=out)
    print("  remaining: " + ("; ".join(state.remaining) if state.remaining else "nothing"), file=out)


# --- making what is missing ------------------------------------------------------


def open_project(slug: str, kind: str, document: str, *, home: Conversation | None, client, admin, about: str = "",
                 out=None, environ=None) -> ProjectState:
    """Create or continue `pj-<slug>`: the channel and its members, the
    document, the setup request — each only if it is not there yet."""
    out = sys.stdout if out is None else out
    if kind not in KINDS:
        raise ProjectError(f"kind must be one of {', '.join(KINDS)}")
    check_slug(slug)
    name = project_channel(slug)
    before = inspect_project(slug, client, admin=admin, kind=kind, environ=environ)
    if before.archived:
        raise ProjectError(f"#{name} is archived; choose another slug or unarchive it by hand")
    if before.exists and before.kind and before.kind != kind:
        raise ProjectError(f"#{name} is a {before.kind}, not a {kind}")
    did: list[str] = []
    roles = project_principals(admin, client)
    if not before.exists:
        origin = f"{_origin_word(home)} {home}" if home else "a request by hand"
        description = f"[AUTO] project: {slug}; {kind}; opened from {origin}; its document is the `{doc_topic(slug, kind)}` topic"
        folder = admin.channel_folder_by_name(name)
        folder_id = int(folder["id"]) if folder else admin.create_channel_folder(name, f"{slug} project channel and its work channels")
        admin.create_channel(name, description, principals=_flat(roles), folder_id=folder_id)
        did.append(f"created #{name} in folder {folder_id} with {_flat(roles)}")
    elif before.missing:
        missing = sorted({u for users in before.missing.values() for u in users})
        admin.subscribe_channels([name], principals=missing)
        did.append(f"subscribed {missing} to #{name}")
    for role in ("runner", "workspace"):
        if not roles[role]:
            print(f"warning: no agent on the board answers {RUNNER_PREFIX if role == 'runner' else WORKSPACE_PREFIX} "
                  f"topics; the {role} is not subscribed", file=out)
    topic = before.doc_topic or doc_topic(slug, kind)
    doc_id = before.doc_id
    if doc_id is None:
        doc_id = client.send_to_channel(name, topic, f"{document}\n\n---\n{_origin_line(home)}")
        did.append(f"posted the document as #{doc_id} in {topic}")
    elif document.strip():
        print(f"the document already exists (#{doc_id} in {topic}) and is kept; a further plan is "
              f"`agproject plan {slug} --doc …`", file=out)
    if before.setup_id is None and before.state == "ready":
        print(f"the workspace already exists ({before.repository} at {before.revision}); no setup request", file=out)
    elif before.setup_id is None:
        if home is not None:
            client.send_to_channel(name, setup_topic(slug), rootchat_note(home))
        setup_id = client.send_to_channel(name, setup_topic(slug), setup_request(slug, kind, doc_id, home, about=about))
        did.append(f"asked for the workspace as #{setup_id} in {setup_topic(slug)}")
    after = inspect_project(slug, client, admin=admin, kind=kind, environ=environ)
    for line in did or ["nothing was missing; nothing was written"]:
        print(line, file=out)
    _print_state(after, out)
    if after.state == "setup-pending":
        print("the setup is pending: end this run; autolab's answer names the requester and brings its "
              f"conversation ({after.setup_home or 'none recorded'}) back", file=out)
    print("nothing has been started: no workrun- topic was opened and no routine was run", file=out)
    return after


def write_plan(slug: str, stem: str, document: str, *, home: Conversation | None, client, out=None) -> dict:
    """One research plan into an existing study's channel."""
    out = sys.stdout if out is None else out
    name = project_channel(slug)
    row = next((r for r in client.channels() if r.get("name") == name), None)
    if row is None:
        raise ProjectError(f"#{name} does not exist; a new study is `agproject open {slug} --kind study`")
    topic = plan_topic(stem)
    if client.topic_last_id(name, topic) or client.topic_last_id(name, f"{RESOLVED_TOPIC_PREFIX}{topic}"):
        raise ProjectError(f"#{name} › {topic} already exists; choose another stem")
    client.ensure_subscribed(name)
    doc_id = client.send_to_channel(name, topic, f"{document}\n\n---\n{_origin_line(home)}")
    link = channel_link(client, int(row["stream_id"]), name)
    print(f"posted the research plan as message {doc_id} in #{name} › {topic} — {link}", file=out)
    print("nothing has been started: the study's routine is not run by this post", file=out)
    return {"channel": name, "topic": topic, "doc_id": doc_id, "link": link}


# --- the command ----------------------------------------------------------------


EPILOG = """\
What each command is for:

  open    Create a project or study, or continue one whose setup stopped
          half way. Safe to repeat: it reports what already exists and
          writes only what is missing. Needs the provisioner credential
          when the channel does not exist or somebody must be subscribed.
  status  Read what exists — channel, members, document, setup request,
          autolab's answer, the internal repository — and what remains.
          Writes nothing; use it before deciding to reuse a study.
  plan    Post a further research plan into an existing study.

States `status` reports: absent, channel-only, document-posted,
setup-pending (asked, no answer yet: end the run and wait — the answer
names you), answered (autolab replied but no workspace is confirmed: read
its answer), ready (the internal `main` repository exists; its revision is
printed), archived.

Recovery: a failure half way is continued by running the same `open`
again. A document you want to replace is a new `plan`, never an edit.
"""


def _document(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ProjectError(f"{path} is empty")
    return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agproject", description=__doc__, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    open_ = sub.add_parser("open", help="create or continue a project or study")
    open_.add_argument("slug", help="the short name; the channel is pj-<slug> and the internal repository <slug>")
    open_.add_argument("--kind", choices=KINDS, required=True)
    open_.add_argument("--doc", required=True, help="the goal (project) or research plan (study), a Markdown file; "
                                                   "kept as is when the document already exists")
    open_.add_argument("--about", default="", help="one line saying what the study or project is about")
    open_.add_argument("--provisioner-env", default=None, help=f"admin credential (default ${PROVISIONER_VARIABLE})")
    open_.add_argument("--json", action="store_true", help="print the resulting state as JSON at the end")
    status = sub.add_parser("status", help="what exists for pj-<slug> and what remains (read-only)")
    status.add_argument("slug")
    status.add_argument("--provisioner-env", default=None,
                        help="also check the subscribers against who should be there (needs the admin credential)")
    status.add_argument("--json", action="store_true")
    plan = sub.add_parser("plan", help="add a research plan to an existing study's channel")
    plan.add_argument("slug", help="the study's slug (its channel is pj-<slug>)")
    plan.add_argument("--doc", required=True, help="the research plan, a Markdown file")
    plan.add_argument("--stem", default=None, help="topic stem; the topic becomes researchplan-<stem> (default: the slug)")
    return parser


def run(argv: list[str], *, client=None, admin=None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    try:
        home = home_from_environment()
        if args.command == "open":
            state = open_project(args.slug, args.kind, _document(args.doc), home=home, client=_self(client),
                                 admin=admin or provisioner(args.provisioner_env), about=args.about, out=out)
            if args.json:
                print(json.dumps(state.as_dict(), indent=2, sort_keys=True), file=out)
        elif args.command == "status":
            chosen = admin
            if chosen is None and (args.provisioner_env or os.environ.get(PROVISIONER_VARIABLE)):
                chosen = provisioner(args.provisioner_env)
            state = inspect_project(args.slug, _self(client), admin=chosen)
            _print_state(state, out)
            if args.json:
                print(json.dumps(state.as_dict(), indent=2, sort_keys=True), file=out)
        else:
            write_plan(args.slug, args.stem or args.slug, _document(args.doc), home=home, client=_self(client), out=out)
        return 0
    except (ProjectError, ZulipError, OSError) as error:
        print(f"agproject: {error}", file=err)
        return 1


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
