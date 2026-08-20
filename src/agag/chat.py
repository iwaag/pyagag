"""`agentchat` — the shared way an agent reads and writes Zulip at run time.

Every agent's *listener* already speaks Zulip through `agag.zulip`, but that
is the harness talking, not the agent. This is the agent-facing half: one
executable an agentic run can call from its workspace to look at a channel
and say something in it, so a run that decides to ask another agent has a
hand to do it with.

Two properties are the whole design:

- **Identity is the env file.** `AGENTCHAT_ZULIP_ENV` names a bot credentials
  file and whoever's file that is, is who speaks. There is no `--as` flag and
  no shared service account: the caller's identity is the caller's
  environment, decided by whoever launched the run.
- **No subscriptions.** Zulip lets a bot post into and read any public
  channel without subscribing, so this never touches subscriptions. What an
  agent is *subscribed* to decides what gets swept into its own inbox; that
  is a routing decision belonging to the listener, and reading or answering
  elsewhere must not silently change it.

`--help` is this tool's documentation — it is written as a usage document
with examples, because a powerful command handed over with a bare argparse
synopsis is an Unexplained Chainsaw.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .zulip import ZulipClient, ZulipError

ENV_VARIABLE = "AGENTCHAT_ZULIP_ENV"
DEFAULT_READ_COUNT = 30

__all__ = [
    "DEFAULT_READ_COUNT",
    "ENV_VARIABLE",
    "AgentChatError",
    "build_parser",
    "client_from_environment",
    "format_messages",
    "main",
]

USAGE_DOC = """\
Read and write Zulip as this agent, so you can talk to other agents.

Zulip is organized as channels, and each channel holds topics; one topic is
one conversation. Another agent's entrance is a channel of its own, and a
topic name prefix is usually how you tell it what kind of request this is —
whatever that agent's introduction told you.

You speak as whichever bot account the credentials file names, and reading or
posting in a channel does not subscribe you to it: your own inbox is
unchanged by anything you do here.

Examples

  # What conversations exist in another agent's channel?
  agentchat topics agforge-agstudio1

  # Read the most recent messages of one conversation.
  agentchat read agforge-agstudio1 create-title-image-1

  # Start a conversation (a topic that does not exist yet is created by
  # posting into it) or add to one.
  agentchat send agforge-agstudio1 create-title-image-1 \\
      "Please make a 16:9 title image: a lone lighthouse at dusk."

  # Multi-line text is fine; Zulip renders Markdown.
  agentchat send agents intro-front "$(cat intro.md)"

Notes

  Ask before you speak on somebody's behalf: a post is public and permanent.
  Answers do not come back here — read the topic again to see the reply.
"""


class AgentChatError(RuntimeError):
    """The command cannot run as asked."""


def client_from_environment(environ=None) -> ZulipClient:
    """Build the client from the credentials file `AGENTCHAT_ZULIP_ENV` names.

    The failure message names the variable, because an agent that hits this
    can only be helped by knowing what was supposed to be set for it.
    """
    environ = os.environ if environ is None else environ
    reference = environ.get(ENV_VARIABLE, "").strip()
    if not reference:
        raise AgentChatError(
            f"{ENV_VARIABLE} is not set: it must name the Zulip credentials "
            "file this agent speaks with"
        )
    path = Path(os.path.expanduser(reference))
    if not path.is_file():
        raise AgentChatError(f"{ENV_VARIABLE} points at {path}, which is not a file")
    return ZulipClient.from_env(path)


def _timestamp(value) -> str:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return "unknown-time"


def format_messages(messages: list[dict]) -> str:
    """One `[time] sender:` header per message, body below it, oldest first."""
    blocks = []
    for message in messages:
        sender = message.get("sender_full_name") or f"user{message.get('sender_id')}"
        content = str(message.get("content", "")).strip()
        blocks.append(f"[{_timestamp(message.get('timestamp'))}] {sender}:\n{content}")
    return "\n\n".join(blocks)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentchat",
        description=USAGE_DOC,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    send = subcommands.add_parser(
        "send",
        help="post a message into one channel's topic",
        description=(
            "Post into <channel> > <topic>. A topic that does not exist yet "
            "is created by posting into it, which is how a new request is "
            "opened. The message id is printed on success."
        ),
    )
    send.add_argument("channel", help="channel name, without the leading '#'")
    send.add_argument("topic", help="topic name; a new name starts a new conversation")
    send.add_argument("text", nargs="+", help="the message; Markdown is rendered")

    read = subcommands.add_parser(
        "read",
        help="show recent messages of one channel's topic",
        description=(
            "Print one conversation, oldest message first, each with its "
            "sender and UTC timestamp."
        ),
    )
    read.add_argument("channel", help="channel name, without the leading '#'")
    read.add_argument("topic", help="topic name")
    read.add_argument(
        "--count", type=int, default=DEFAULT_READ_COUNT,
        help=f"how many recent messages to show (default {DEFAULT_READ_COUNT})",
    )

    topics = subcommands.add_parser(
        "topics",
        help="list the topics of one channel",
        description=(
            "Print the channel's topic names, most recently active first. "
            "A name starting with '✔' is a conversation somebody marked "
            "resolved."
        ),
    )
    topics.add_argument("channel", help="channel name, without the leading '#'")

    return parser


def _run(args, client: ZulipClient, out) -> int:
    if args.command == "send":
        text = " ".join(args.text).strip()
        if not text:
            raise AgentChatError("refusing to send an empty message")
        message_id = client.send_to_channel(args.channel, args.topic, text)
        print(
            f"sent message {message_id} to #{args.channel} > {args.topic}",
            file=out,
        )
        return 0
    if args.command == "read":
        if args.count < 1:
            raise AgentChatError("--count must be at least 1")
        messages = client.topic_history(args.channel, args.topic, num_before=args.count)
        if not messages:
            print(f"no messages in #{args.channel} > {args.topic}", file=out)
            return 0
        print(format_messages(messages), file=out)
        return 0
    if args.command == "topics":
        names = client.channel_topics(client.stream_id(args.channel))
        if not names:
            print(f"no topics in #{args.channel}", file=out)
            return 0
        print("\n".join(names), file=out)
        return 0
    raise AgentChatError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args, client_from_environment(), out)
    except (AgentChatError, ZulipError) as error:
        print(f"agentchat: {error}", file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
