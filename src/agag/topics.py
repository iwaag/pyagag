"""The shape every agent's chat-topic handler has in common.

Two agents grew the same handler independently — agautolab's mission topics
and agforge's create topics. What is shared is not the work but the
discipline around it:

  ack first, so the sweep stops re-matching a topic being served
  build a numbered generation workspace under `.local/topics/<ch>/<topic>/`
  dump the conversation as `chatlog.md`
  run the agent(s)
  **always** post something back, naming the step if it failed
  hand the turn to whoever spoke last, by naming them in the reply
  re-check for human posts that arrived during the run, and serve again

`serve_topic` owns that skeleton; the caller supplies one `handler` that does
the agent-specific part. What stays with the caller is anything that is one
agent's own vocabulary — command files, sub-work keys, work selection,
project clones.

Two things here carry the turn-taking rule, and both are code rather than
instruction. `reply_to` separates the conversation being *served* from the
one being *spoken into*: a run brought back by a mention works on its own
task and answers in the topic that called it. And `handoff_mention` prepends
`@**<name>**` of the last other speaker to every reply, so the next turn is
handed over mechanically — no guide has to ask an agent to remember to
address the person it is answering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .zulip import ZulipClient, ZulipError, _safe_topic_component, log as default_log, topic_write

HISTORY_MESSAGES = 1000

__all__ = [
    "GuideError",
    "handoff_mention",
    "write_threads",
    "TopicContext",
    "TopicResult",
    "chatlog_path",
    "format_chatlog",
    "generation_dir",
    "guide",
    "next_generation",
    "next_record_path",
    "prompt_with_guide",
    "serve_topic",
    "threads_dir",
    "topic_workspace",
]


class GuideError(RuntimeError):
    """A guide file the agent needs is missing or empty."""


# --- workspaces ------------------------------------------------------------


def topic_workspace(root: Path, channel: str, topic: str) -> Path:
    """`<root>/<channel>/<topic>/` — one directory per conversation.

    Both display names are validated as single path components, so a channel
    or topic named `../x` cannot escape `root`.
    """
    return (
        root
        / _safe_topic_component(channel, "channel")
        / _safe_topic_component(topic, "topic")
    )


def next_generation(topic_dir: Path) -> int:
    """The topic's next generation number: highest existing one, plus one.

    Read off the directory rather than a counter file, so a hand-made or
    hand-removed generation cannot desynchronize it. Generations are never
    deleted: cutting a new one is precisely what stops a previous
    generation's leftover files from being re-executed.
    """
    highest = 0
    if topic_dir.is_dir():
        for child in topic_dir.iterdir():
            if child.is_dir() and child.name.isdigit():
                highest = max(highest, int(child.name))
    return highest + 1


def generation_dir(root: Path, channel: str, topic: str, number: int, role: str) -> Path:
    directory = topic_workspace(root, channel, topic) / str(number) / role
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def next_record_path(directory: Path) -> Path:
    """`run-NNNN.json` — the common numbering for `ag.agent-run.v1` records."""
    directory.mkdir(parents=True, exist_ok=True)
    number = 1
    while (directory / f"run-{number:04d}.json").exists():
        number += 1
    return directory / f"run-{number:04d}.json"


# --- the conversation as a file -------------------------------------------


def format_chatlog(messages: list[dict], self_id: int, *, drop=None) -> str:
    """`[name] body` per line, own lines marked `name (you)`.

    `drop(content)` filters out this bot's own transport noise — acks and the
    like. It is only consulted for our own messages: a human quoting an ack
    is conversation.
    """
    lines = []
    for message in messages:
        content = str(message.get("content", "")).strip()
        own = message.get("sender_id") == self_id
        if own and drop is not None and drop(content):
            continue
        speaker = message.get("sender_full_name") or f"user{message.get('sender_id')}"
        if own:
            speaker = f"{speaker} (you)"
        lines.append(f"[{speaker}] {content}")
    return "\n".join(lines) + ("\n" if lines else "")


def chatlog_path(directory: Path) -> Path:
    return directory / "chatlog.md"


def threads_dir(directory: Path) -> Path:
    return directory / "threads"


def write_threads(
    client: ZulipClient,
    directory: Path,
    conversations,
    self_id: int,
    *,
    history_messages: int = HISTORY_MESSAGES,
    drop=None,
    log=default_log,
) -> list[Path]:
    """`threads/<channel>/<topic>.md` for each conversation this run is party to.

    The same renderer as `chatlog.md`, because they are the same thing: a
    conversation, as a file, in the workspace. `chatlog.md` is the one being
    served; these are the others the agent has spoken in and may be answered
    in. A thread that cannot be read is skipped and logged — a missing file is
    a run with less context, an exception is a run that does not happen.
    """
    written: list[Path] = []
    for channel, topic in conversations:
        try:
            messages = client.topic_history(channel, topic, num_before=history_messages)
        except (ZulipError, ValueError) as error:
            log(f"could not render thread {channel!r}/{topic!r}: {error!r}")
            continue
        try:
            path = (
                threads_dir(directory)
                / _safe_topic_component(channel, "channel")
                / f"{_safe_topic_component(topic, 'topic')}.md"
            )
        except ValueError as error:
            log(f"skipping thread with an unusable name: {error}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(format_chatlog(messages, self_id, drop=drop), encoding="utf-8")
        written.append(path)
    return written


def handoff_mention(
    client: ZulipClient, channel: str, topic: str, self_id: int, *, num_before: int = 20
) -> str:
    """`@**<name>**` for the last speaker in this topic who is not this bot.

    Whose turn it is next is not etiquette to be remembered — it is the
    mechanism by which the next run happens at all, because a participant of
    a conversation is served only when it is named. So the skeleton names
    them, and no guide has to ask.

    Empty when nobody else has spoken, which is not an error: a topic this
    bot opened alone has no turn to hand back yet.
    """
    try:
        history = client.topic_history(channel, topic, num_before=num_before)
    except Exception:  # noqa: BLE001 - a lost mention must not cost the reply
        return ""
    for message in reversed(history):
        if message.get("sender_id") == self_id:
            continue
        name = str(message.get("sender_full_name") or "").strip()
        if name:
            return f"@**{name}**"
    return ""


# --- prompts ---------------------------------------------------------------


def guide(root: Path, *parts: str) -> str:
    """Read one guide file under `root`.

    The instruction text belongs to the agents, not to the transport. A
    missing or empty guide is fatal on purpose: a run started without it
    would be a prompt with no instruction in it.
    """
    path = root.joinpath(*parts)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise GuideError(f"cannot read guide {path}: {error}") from error
    if not text:
        raise GuideError(f"guide is empty: {path}")
    return text


def prompt_with_guide(lines, guide_text: str) -> str:
    """Placement lines, then the guide. The whole prompt composition rule."""
    return "\n".join(lines) + f"\n\n{guide_text}"


def chatlog_placement(bot_name: str) -> str:
    return (
        "The chatlog is placed in the working directory. "
        f"You are {bot_name!r} in the chatlog."
    )


# --- the serving skeleton --------------------------------------------------


@dataclass
class TopicContext:
    """One serving of one topic, handed to the handler."""

    client: ZulipClient
    channel: str
    topic: str
    self_id: int
    bot_name: str
    history: list[dict] = field(default_factory=list)
    processed_up_to: int = 0
    #: Named by the handler as it advances; a failure is reported as
    #: `failed during <step>`, so this is what tells a human where it broke.
    step: str = "reading the topic"
    #: Where this serving speaks. The same conversation it is serving, unless
    #: the run was brought back by a mention somewhere else — then the work
    #: is this topic's and the answer belongs where the question was asked.
    reply_channel: str = ""
    reply_topic: str = ""

    def __post_init__(self) -> None:
        self.reply_channel = self.reply_channel or self.channel
        self.reply_topic = self.reply_topic or self.topic

    @property
    def replies_here(self) -> bool:
        """Whether this serving answers in the topic it is serving."""
        return (self.reply_channel, self.reply_topic) == (self.channel, self.topic)

    def post(self, text: str) -> None:
        """Post into the reply target now, ahead of the final reply."""
        topic_write(
            self.reply_topic, text, channel=self.reply_channel, client=self.client
        )

    def post_home(self, text: str) -> None:
        """Post into the conversation being served, wherever the reply goes."""
        topic_write(self.topic, text, channel=self.channel, client=self.client)

    def humans_spoke(self) -> bool:
        return any(m.get("sender_id") != self.self_id for m in self.history)


@dataclass
class TopicResult:
    """What the handler produced: what to post, and whether to resolve."""

    sections: list[str] = field(default_factory=list)
    resolve_after: bool = False


def serve_topic(
    client: ZulipClient,
    channel: str,
    topic: str,
    handler,
    *,
    ack_text: str,
    empty_reply: str | None = None,
    reply_to: tuple[str, str] | None = None,
    history_messages: int = HISTORY_MESSAGES,
    log=default_log,
) -> None:
    """Serve one awaiting topic, and always answer it.

    `handler(context) -> TopicResult` does the agent-specific work. Every
    exit path after the ack posts something: an ack followed by silence would
    leave this bot as the topic's last poster, which hides the topic from the
    sweep until a human posts again.

    A human posting *during* a run is not lost either. The final reply makes
    this bot the last poster, so before leaving, this re-checks for messages
    newer than the chatlog it processed and serves the topic again.

    `empty_reply`, when given, is posted instead of running the handler on a
    topic holding nothing but this bot's own messages. That is not a nicety:
    `sweep_topics` skips a topic only when its *last* poster is this bot, so
    a topic with no messages at all matches every sweep forever, and the
    reply is what silences it.

    `reply_to` is where the ack, the reply and the resolve go when that is not
    the conversation being served — a run brought back by a mention elsewhere.
    The work stays this topic's; the answer goes where the question was asked.
    The post-run re-check is skipped in that case on purpose: this bot did not
    become the served topic's last poster, so anything that arrived there is
    found by the ordinary owner sweep instead of by looping here.

    Every reply is prefixed with a mention of the last other speaker in the
    topic being replied into. That is the turn-taking rule as code: whoever is
    named is served next, and a reply that names nobody ends the exchange.
    """
    self_user = client.whoami()
    self_id = int(self_user["user_id"])
    bot_name = str(self_user.get("full_name") or client.email)
    reply_channel, reply_topic = reply_to or (channel, topic)

    while True:
        topic_write(reply_topic, ack_text, channel=reply_channel, client=client)

        context = TopicContext(
            client, channel, topic, self_id, bot_name,
            reply_channel=reply_channel, reply_topic=reply_topic,
        )
        result = TopicResult()
        completed = False
        try:
            context.step = "chatlog"
            context.history = client.topic_history(
                channel, topic, num_before=history_messages
            )
            context.processed_up_to = max(
                (int(m.get("id", 0)) for m in context.history), default=0
            )
            if empty_reply is not None and not context.humans_spoke():
                log(f"nothing to answer in {channel!r}/{topic!r}: no messages")
                context.post(empty_reply)
                return
            result = handler(context)
            completed = True
        except Exception as error:  # noqa: BLE001 - the topic is the error channel
            log(f"topic workflow failed during {context.step}: {error!r}")
            result = TopicResult([f"failed during {context.step}: {error}"])

        body = "\n\n".join(section for section in result.sections if section)
        if body:
            mention = handoff_mention(client, reply_channel, reply_topic, self_id)
            topic_write(
                reply_topic,
                f"{mention}\n\n{body}" if mention else body,
                channel=reply_channel,
                client=client,
            )

        if result.resolve_after:
            # After the final reply, so the whole conversation moves under
            # the ✔ name; a resolved topic stops matching the sweep.
            try:
                tail = client.topic_history(reply_channel, reply_topic, num_before=1)
                if tail:
                    client.resolve_topic(int(tail[-1]["id"]), reply_topic)
            except Exception as error:  # noqa: BLE001
                log(f"could not resolve {reply_channel!r}/{reply_topic!r}: {error!r}")
            return
        if not completed:
            return  # do not loop on a failing topic; a human post re-arms it
        if not context.replies_here:
            return  # the owner sweep, not this loop, picks up what arrived

        try:
            tail = client.topic_history(channel, topic, num_before=history_messages)
        except ZulipError as error:
            log(f"post-run re-check failed for {channel!r}/{topic!r}: {error!r}")
            return
        if not any(
            m.get("sender_id") != self_id and int(m.get("id", 0)) > context.processed_up_to
            for m in tail
        ):
            return
        log(f"reprocessing {channel!r}/{topic!r}: human posts arrived during the run")
