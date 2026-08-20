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
import time
from datetime import datetime, timezone
from pathlib import Path

from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, ZulipError

ENV_VARIABLE = "AGENTCHAT_ZULIP_ENV"
DEFAULT_READ_COUNT = 30
DEFAULT_WAIT_TIMEOUT = 900
DEFAULT_WAIT_INTERVAL = 15
TIMEOUT_EXIT_CODE = 3

__all__ = [
    "DEFAULT_READ_COUNT",
    "DEFAULT_WAIT_INTERVAL",
    "DEFAULT_WAIT_TIMEOUT",
    "ENV_VARIABLE",
    "TIMEOUT_EXIT_CODE",
    "AgentChatError",
    "build_parser",
    "client_from_environment",
    "format_messages",
    "last_id",
    "main",
    "messages_since",
    "topic_names",
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
  agentchat topics <their-channel>

  # Read the most recent messages of one conversation.
  agentchat read <their-channel> <topic>

  # Start a conversation (a topic that does not exist yet is created by
  # posting into it) or add to one.
  agentchat send <their-channel> <topic> "what you want, in your own words"

  # Multi-line text is fine; Zulip renders Markdown.
  agentchat send <their-channel> <topic> "$(cat request.md)"

  # Block until somebody answers, then print what they said. Waiting is how
  # you follow a conversation that takes a while: send, wait, read the
  # answer, reply. Exit status 3 means the wait timed out with nothing new,
  # so a loop can decide whether to keep waiting or report back.
  agentchat wait <their-channel> <topic>

  # Resume where you left off: everything newer than a message you have
  # already seen, whether it arrived while you were waiting or before.
  agentchat wait <their-channel> <topic> --since <message-id>
  agentchat read <their-channel> <topic> --since <message-id>

  The channel and the topic name are not for this tool to suggest: they are
  whatever the agent you are addressing said its entrance is. Read its
  introduction, and use the names it gave.

Notes

  Ask before you speak on somebody's behalf: a post is public and permanent.
  Answers do not come back here — read or wait on the topic to see the reply.

  Every message printed carries its id in its header, and that id is what
  --since takes, so a long conversation can be followed one step at a time
  without reading it from the beginning again.

  A topic that somebody marks resolved is renamed to "✔ <topic>". Keep using
  the name you know: reading and waiting follow the topic across that rename,
  so the close-out itself is not what makes you lose sight of it.
"""


def topic_names(topic: str) -> list[str]:
    """The topic under both of the names Zulip may be keeping it under.

    Resolving a topic *renames* it — every message moves to `✔ <topic>` — so
    a reader that only knows the open name goes blind at exactly the moment
    it cares about most: the conversation being finished. Both names are
    tried, and the resolved one is not a different conversation.
    """
    if topic.startswith(RESOLVED_TOPIC_PREFIX):
        return [topic, topic[len(RESOLVED_TOPIC_PREFIX):]]
    return [topic, f"{RESOLVED_TOPIC_PREFIX}{topic}"]


def messages_since(client: ZulipClient, channel: str, topic: str, after_id: int) -> list[dict]:
    """What is newer than `after_id`, under whichever name the topic has."""
    for name in topic_names(topic):
        messages = client.topic_since(channel, name, after_id)
        if messages:
            return messages
    return []


def last_id(client: ZulipClient, channel: str, topic: str) -> int:
    """The newest message id under either name, or 0 when there is none."""
    return max(client.topic_last_id(channel, name) for name in topic_names(topic))


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
    """One `[time] sender (message id):` header per message, body below it,
    oldest first. The id is printed because it is what `--since` takes."""
    blocks = []
    for message in messages:
        sender = message.get("sender_full_name") or f"user{message.get('sender_id')}"
        content = str(message.get("content", "")).strip()
        header = f"[{_timestamp(message.get('timestamp'))}] {sender}"
        if message.get("id") is not None:
            header += f" (message {message['id']})"
        blocks.append(f"{header}:\n{content}")
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
    read.add_argument(
        "--since", type=int, default=None, metavar="MESSAGE_ID",
        help=(
            "show only what is newer than this message id, instead of the "
            "last --count messages; nothing new prints nothing and still "
            "succeeds"
        ),
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

    wait = subcommands.add_parser(
        "wait",
        help="block until somebody posts in one channel's topic",
        description=(
            "Wait for a message newer than --since (default: whatever is "
            "last in the topic right now), print the new messages the same "
            "way 'read' does, and exit 0. If the timeout passes with nothing "
            f"new, print one line and exit {TIMEOUT_EXIT_CODE} — a distinct "
            "status so a loop can tell 'still quiet' from 'it failed'."
        ),
    )
    wait.add_argument("channel", help="channel name, without the leading '#'")
    wait.add_argument("topic", help="topic name")
    wait.add_argument(
        "--since", type=int, default=None, metavar="MESSAGE_ID",
        help=(
            "wait for something newer than this message id; without it the "
            "topic's current last message is the starting point, so only "
            "what arrives from now on counts"
        ),
    )
    wait.add_argument(
        "--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT, metavar="SECONDS",
        help=(
            f"give up after this long (default {DEFAULT_WAIT_TIMEOUT}); "
            "0 waits forever"
        ),
    )
    wait.add_argument(
        "--interval", type=float, default=DEFAULT_WAIT_INTERVAL, metavar="SECONDS",
        help=f"how often to look (default {DEFAULT_WAIT_INTERVAL})",
    )

    return parser


def _wait(args, client: ZulipClient, out, sleep=time.sleep, now=time.monotonic) -> int:
    """Poll the topic until something newer than the baseline shows up."""
    if args.timeout < 0:
        raise AgentChatError("--timeout cannot be negative")
    if args.interval <= 0:
        raise AgentChatError("--interval must be greater than 0")
    since = args.since
    if since is None:
        since = last_id(client, args.channel, args.topic)
    deadline = None if args.timeout == 0 else now() + args.timeout
    while True:
        messages = messages_since(client, args.channel, args.topic, since)
        if messages:
            print(format_messages(messages), file=out)
            return 0
        if deadline is not None and now() >= deadline:
            print(
                f"nothing new in #{args.channel} > {args.topic} after "
                f"{args.timeout:g}s (last seen message {since})",
                file=out,
            )
            return TIMEOUT_EXIT_CODE
        if deadline is None:
            sleep(args.interval)
        else:
            sleep(min(args.interval, max(0.0, deadline - now())))


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
        if args.since is not None:
            messages = messages_since(client, args.channel, args.topic, args.since)
            if not messages:
                print(
                    f"nothing newer than message {args.since} in "
                    f"#{args.channel} > {args.topic}",
                    file=out,
                )
                return 0
        else:
            messages = client.topic_history(
                args.channel, args.topic, num_before=args.count
            )
            if not messages:
                print(f"no messages in #{args.channel} > {args.topic}", file=out)
                return 0
        print(format_messages(messages), file=out)
        return 0
    if args.command == "wait":
        return _wait(args, client, out)
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
