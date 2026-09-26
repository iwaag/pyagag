"""`agroutine`: register a routine and keep its guide.

A **routine** is a process guide kept in Zulip and the runs made of it
(`refine_routine` p1): one public channel `#routine-<name>`, filed in the
`routine` channel folder, whose fixed `guide` topic holds the guide — **the
newest post there is the whole guide**, a new version is a new full post,
and a ✔ on the topic retires the routine. Runs are `routinerun-<id>` topics
the routine runner (the agent whose introduction answers `routinerun-`)
opens when somebody asks it to run the routine; **registering a routine or
posting a guide starts nothing**.

    agroutine create <name> --guide-file <file>
    agroutine update <name> --guide-file <file>
    agroutine show <name>
    agroutine list

`create` makes the channel with the provisioner credential
(`AGAG_PROVISIONER_ENV`), subscribes the realm's human owners, the routine
runner and the board reader (`AGAG_BOARD_READER`), and posts the guide as
the caller (`AGENTCHAT_ZULIP_ENV`). Run again, it continues: a channel that
exists is completed (missing members, a guide never posted), and a guide
that is already the newest one is not posted twice. `update` posts a new
complete version. Every guide post is **read back**: Zulip silently
truncates a long post, so a guide whose stored text differs from the file
is reported as a failure — shorten it and `update`.

What the guide says is the caller's to write. A study routine's guide
usually tells the runner which project channel to ask for **one** bounded
mission, what the mission is for, the operational context a run needs, and
what the report names.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .project import ProjectError, board_agents, board_reader, provisioner
from .selfnote import is_selfnote
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipError

ROUTINE_FOLDER = "routine"
ROUTINE_CHANNEL_PREFIX = "routine-"
GUIDE_TOPIC = "guide"
RUN_PREFIX = "routinerun-"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,50}$")
TRUNCATED = "[message truncated]"
HISTORY = 400

__all__ = ["RoutineError", "channel_description", "create_routine", "main", "routine_channel", "routine_state",
           "run", "update_guide"]


class RoutineError(RuntimeError):
    """The routine cannot be made, read or updated as asked."""


def routine_channel(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise RoutineError(f"{name!r} is not a routine name (lowercase letters, digits and dashes)")
    return f"{ROUTINE_CHANNEL_PREFIX}{name}"


def channel_description(name: str) -> str:
    """The description every routine channel carries, word for word."""
    return (f"Routine `{name}`. `guide` = the process guide (newest post is the whole guide; posting there starts "
            "nothing). Each execution is its own `routinerun-<id>` topic here.")


def _self(client):
    if client is not None:
        return client
    from .chat import client_from_environment

    return client_from_environment()


def _row(client, channel: str) -> dict | None:
    return next((r for r in client.channels(include_archived=True) if r.get("name") == channel), None)


def _guides(client, channel: str) -> tuple[list[dict], bool]:
    """The guide posts, oldest first, and whether the topic is retired (✔)."""
    live = [m for m in client.topic_history(channel, GUIDE_TOPIC, num_before=HISTORY) if not is_selfnote(m.get("content"))]
    retired = [m for m in client.topic_history(channel, f"{RESOLVED_TOPIC_PREFIX}{GUIDE_TOPIC}", num_before=HISTORY)
               if not is_selfnote(m.get("content"))]
    return (live or retired), bool(retired and not live)


def routine_principals(admin, client) -> dict[str, list[int]]:
    board = board_agents(client)
    roles = {"owners": sorted(admin.realm_owners()), "runner": [board["runner"]] if "runner" in board else []}
    reader = board_reader(admin)
    roles["board reader"] = [reader] if reader is not None else []
    return roles


def routine_state(name: str, client, *, admin=None) -> dict:
    """What exists for the routine: channel, members, guide versions, runs."""
    channel = routine_channel(name)
    row = _row(admin or client, channel)
    if row is None:
        return {"name": name, "channel": channel, "exists": False, "remaining": ["create the channel", "post the guide"]}
    state: dict = {"name": name, "channel": channel, "exists": True, "stream_id": int(row["stream_id"]),
                   "archived": bool(row.get("is_archived")), "folder_id": row.get("folder_id"),
                   "description_ok": str(row.get("description") or "") == channel_description(name)}
    remaining: list[str] = []
    if state["archived"]:
        state["remaining"] = ["the channel is archived"]
        return state
    subscribers = set(int(u) for u in (admin or client).channel_subscribers(state["stream_id"]))
    state["subscribers"] = sorted(subscribers)
    if admin is not None:
        folder = admin.channel_folder_by_name(ROUTINE_FOLDER)
        state["folder_ok"] = bool(folder) and row.get("folder_id") == folder.get("id")
        missing = {role: [u for u in users if u not in subscribers]
                   for role, users in routine_principals(admin, client).items()}
        state["missing"] = {role: users for role, users in missing.items() if users}
        if state["missing"]:
            remaining.append("subscribe " + ", ".join(f"{r} {u}" for r, u in state["missing"].items()))
        if not state["folder_ok"]:
            remaining.append(f"file the channel in the `{ROUTINE_FOLDER}` folder")
    guides, retired = _guides(client, channel)
    state["retired"] = retired
    state["guide_versions"] = len(guides)
    if guides:
        newest = guides[-1]
        state["guide"] = {"id": int(newest["id"]), "by": str(newest.get("sender_full_name") or ""),
                          "chars": len(str(newest.get("content") or "")),
                          "truncated": TRUNCATED in str(newest.get("content") or "")}
    else:
        remaining.append("post the guide")
    runs = [t for t in client.channel_topics(state["stream_id"])
            if t.removeprefix(RESOLVED_TOPIC_PREFIX).startswith(RUN_PREFIX)]
    state["runs"] = len(runs)
    state["remaining"] = remaining
    return state


def _post_guide(client, channel: str, text: str) -> dict:
    """Post one complete guide and read it back by id."""
    message_id = client.send_to_channel(channel, GUIDE_TOPIC, text)
    back = client.message(message_id, strict=True)
    stored = str((back or {}).get("content") or "")
    intact = stored.strip() == text.strip() and TRUNCATED not in stored
    return {"id": int(message_id), "chars": len(text), "stored_chars": len(stored), "intact": intact}


def create_routine(name: str, guide: str, *, client, admin, out=None) -> dict:
    out = sys.stdout if out is None else out
    channel = routine_channel(name)
    if not guide.strip():
        raise RoutineError("the guide is empty")
    before = routine_state(name, client, admin=admin)
    if before.get("archived"):
        raise RoutineError(f"#{channel} is archived; unarchive it by hand or choose another name")
    if before.get("retired"):
        raise RoutineError(f"#{channel} › guide is ✔ (the routine is retired); un-✔ it by hand to revive it")
    roles = routine_principals(admin, client)
    principals = sorted({u for users in roles.values() for u in users})
    if not roles["runner"]:
        print(f"warning: no agent on the board answers {RUN_PREFIX} topics; nobody will run this routine", file=out)
    folder = admin.channel_folder_by_name(ROUTINE_FOLDER)
    if folder is None:
        raise RoutineError(f"the realm has no `{ROUTINE_FOLDER}` channel folder")
    if not before["exists"]:
        admin.create_channel(channel, channel_description(name), principals=principals, folder_id=int(folder["id"]))
        print(f"created #{channel} in the `{ROUTINE_FOLDER}` folder with {principals}", file=out)
    else:
        if before.get("missing"):
            missing = sorted({u for users in before["missing"].values() for u in users})
            admin.subscribe_channels([channel], principals=missing)
            print(f"subscribed {missing}", file=out)
        if not before.get("folder_ok"):
            admin.set_channel_folder(before["stream_id"], int(folder["id"]))
            print(f"filed #{channel} in `{ROUTINE_FOLDER}`", file=out)
        if not before.get("description_ok"):
            admin.update_channel_description(before["stream_id"], channel_description(name))
            print("set the routine channel description", file=out)
    client.ensure_subscribed(channel)
    result: dict = {"channel": channel}
    guides, _ = _guides(client, channel)
    if guides and str(guides[-1].get("content") or "").strip() == guide.strip():
        print(f"the newest guide (#{guides[-1]['id']}) is already this text; nothing posted", file=out)
        result["guide"] = {"id": int(guides[-1]["id"]), "intact": True, "posted": False}
    elif guides:
        raise RoutineError(f"#{channel} already has a guide (newest #{guides[-1]['id']}); "
                           f"a new version is `agroutine update {name} --guide-file …`")
    else:
        result["guide"] = _post_guide(client, channel, guide) | {"posted": True}
        _say_guide(result["guide"], out)
    result["state"] = routine_state(name, client, admin=admin)
    print("nothing has been started: a run is a request to the routine runner", file=out)
    return result


def update_guide(name: str, guide: str, *, client, out=None) -> dict:
    out = sys.stdout if out is None else out
    channel = routine_channel(name)
    if not guide.strip():
        raise RoutineError("the guide is empty")
    state = routine_state(name, client)
    if not state["exists"]:
        raise RoutineError(f"#{channel} does not exist; `agroutine create {name} --guide-file …`")
    if state.get("retired"):
        raise RoutineError(f"#{channel} › guide is ✔ (the routine is retired)")
    guides, _ = _guides(client, channel)
    if guides and str(guides[-1].get("content") or "").strip() == guide.strip():
        print(f"the newest guide (#{guides[-1]['id']}) is already this text; nothing posted", file=out)
        return {"channel": channel, "guide": {"id": int(guides[-1]["id"]), "intact": True, "posted": False}}
    client.ensure_subscribed(channel)
    posted = _post_guide(client, channel, guide) | {"posted": True, "version": len(guides) + 1}
    _say_guide(posted, out)
    return {"channel": channel, "guide": posted}


def _say_guide(guide: dict, out) -> None:
    if guide["intact"]:
        print(f"posted the guide as #{guide['id']} ({guide['chars']} chars) and read it back intact", file=out)
    else:
        print(f"posted the guide as #{guide['id']} but the stored text differs ({guide['stored_chars']} of "
              f"{guide['chars']} chars): it was truncated or altered. Shorten it and `update`.", file=out)


EPILOG = """\
What each command is for:

  create  Register a routine: its channel, its members and its first guide.
          Safe to repeat; it completes what is missing and refuses to post
          a different guide over an existing one (that is `update`).
  update  Post a new complete version of the guide (the newest post is the
          whole guide; earlier posts stay as history). Read back after
          posting.
  show    What exists: channel, members, guide versions, run topics.
  list    Every routine channel in the realm.

Exit status 1 with a message on failure, including a guide that did not
survive the read-back. Nothing here runs a routine.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agroutine", description=__doc__, epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="register a routine with its first guide (or continue one)")
    create.add_argument("name", help="the routine's name; the channel becomes routine-<name>")
    create.add_argument("--guide-file", required=True)
    create.add_argument("--provisioner-env", default=None)
    update = sub.add_parser("update", help="post a new complete guide version")
    update.add_argument("name")
    update.add_argument("--guide-file", required=True)
    show = sub.add_parser("show", help="what exists for one routine")
    show.add_argument("name")
    show.add_argument("--provisioner-env", default=None, help="also check members and folder")
    sub.add_parser("list", help="every routine channel")
    for command in (create, update, show):
        command.add_argument("--json", action="store_true")
    return parser


def _guide(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise RoutineError(f"{path} is empty")
    return text


def run(argv: list[str], *, client=None, admin=None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            result = create_routine(args.name, _guide(args.guide_file), client=_self(client),
                                    admin=admin or provisioner(args.provisioner_env), out=out)
            if not result["guide"].get("intact", True):
                return 1
        elif args.command == "update":
            result = update_guide(args.name, _guide(args.guide_file), client=_self(client), out=out)
            if not result["guide"].get("intact", True):
                return 1
        elif args.command == "show":
            chosen = admin or (provisioner(args.provisioner_env) if args.provisioner_env else None)
            result = routine_state(args.name, _self(client), admin=chosen)
            if not args.json:
                print(json.dumps(result, indent=2, sort_keys=True), file=out)
        else:
            me = _self(client)
            for row in sorted(me.channels(), key=lambda r: r.get("name", "")):
                if str(row.get("name", "")).startswith(ROUTINE_CHANNEL_PREFIX):
                    print(f"{row['name'][len(ROUTINE_CHANNEL_PREFIX):]} (#{row['name']}, stream {row['stream_id']})",
                          file=out)
            return 0
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2, sort_keys=True), file=out)
        return 0
    except (RoutineError, ProjectError, ZulipError, OSError) as error:
        print(f"agroutine: {error}", file=err)
        return 1


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
