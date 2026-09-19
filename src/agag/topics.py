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

from collections.abc import Callable

from . import execopt, serving as serving_record
from .delivery import DeliveryError, deliver, redeliver
from .execopt import ExecOptions, Selection
from .memo import is_memo_channel
from .reply import REPLY_GUIDE, record_reply_outcome, resolve_reply
from .selfnote import is_selfnote, is_speech
from .serving import NullJournal, Serving
from .zulip import (
    RESOLVED_TOPIC_PREFIX,
    ZulipClient,
    ZulipError,
    _safe_topic_component,
    log as default_log,
    topic_write,
)

HISTORY_MESSAGES = 1000

#: How much rendered conversation one prompt carries, in characters. Zulip
#: caps a single post at about 10 000, so this is a handful of full-length
#: posts and very many ordinary ones — the common `front-*` serving fits
#: whole. Beyond it the newest end is carried and the rest is named as
#: omitted; the file in the workspace is always complete.
CONVERSATION_BUDGET = 20000

#: The markers the conversation is carried between. Deliberately not
#: Markdown: a fence would be closed by a fence somebody posted, and what
#: this has to survive is text written by other people.
CONVERSATION_BEGIN = "===== BEGIN CONVERSATION ====="
CONVERSATION_END = "===== END CONVERSATION ====="

__all__ = [
    "CONVERSATION_BEGIN",
    "CONVERSATION_BUDGET",
    "CONVERSATION_END",
    "GuideError",
    "apply_exec_commands",
    "conversation_context",
    "handoff_mention",
    "write_threads",
    "TopicContext",
    "TopicResult",
    "chatlog_path",
    "format_chatlog",
    "generation_dir",
    "workspace_identity",
    "guide",
    "next_generation",
    "next_record_path",
    "prompt_with_guide",
    "threads_placement",
    "requester_of",
    "resume_prepared",
    "serve_topic",
    "unprocessed_input",
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


def workspace_identity(cwd: Path) -> dict:
    """`{channel, topic, generation}` read back from a generation directory.

    The inverse of `generation_dir`, keyed on the `.local/topics` pair in the
    path so a caller does not need the topics root. The names are the
    *directory* names, i.e. after `_safe_topic_component`; the relay's
    `/inflight` walks the same directories, so they match what it reports. A
    cwd that is not a generation directory (autolab's project clones,
    forge's generator workspace) yields `{}`.
    """
    parts = cwd.resolve().parts
    for index in range(len(parts) - 5):
        if parts[index] == ".local" and parts[index + 1] == "topics":
            channel, topic, number = parts[index + 2:index + 5]
            if number.isdigit():
                return {"channel": channel, "topic": topic, "generation": int(number)}
    return {}


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

    Selfnotes are dropped whoever wrote them, this bot included. They are
    machine-to-machine (`agag.selfnote`), and an agent that reads its own
    notes starts writing them by hand — at which point they are prose, and
    the deterministic record they were is gone.
    """
    lines = []
    for message in messages:
        content = str(message.get("content", "")).strip()
        if is_selfnote(content):
            continue
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

    Read across the `\u2714 ` resolve rename: the message that calls a run
    back is very often the one that finished the conversation, and a thread
    read under the bare name in that window is empty.
    """
    written: list[Path] = []
    for channel, topic in conversations:
        try:
            messages = client.topic_history(channel, topic, num_before=history_messages)
            if not messages and not topic.startswith(RESOLVED_TOPIC_PREFIX):
                messages = client.topic_history(
                    channel, f"{RESOLVED_TOPIC_PREFIX}{topic}", num_before=history_messages
                )
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

    **No longer what `serve_topic` uses** (`explicit_reply` p1 step 3): the
    skeleton names the requester it recorded from the input it processed
    (`requester_of`), never a speaker looked up at send time. Kept for the
    callers that want the send-time answer on purpose.
    """
    try:
        history = client.topic_history(channel, topic, num_before=num_before)
    except Exception:  # noqa: BLE001 - a lost mention must not cost the reply
        return ""
    for message in reversed(history):
        if message.get("sender_id") == self_id or is_selfnote(message.get("content")):
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


def prompt_with_guide(lines, guide_text: str, *, reply: bool = False) -> str:
    """Placement lines, then the guide. The whole prompt composition rule.

    `reply=True` appends the reply mark's description (`agag.reply
    .REPLY_GUIDE`) after the guide — once, for every conversational role,
    so no guide has to say "no notes to yourself". A structured-output role
    (Front's `present`, a generator that answers with files) leaves it off
    and keeps its own output contract.
    """
    prompt = "\n".join(lines) + f"\n\n{guide_text}"
    return f"{prompt}\n\n{REPLY_GUIDE}" if reply else prompt


def chatlog_placement(bot_name: str) -> str:
    return (
        "The chatlog is placed in the working directory. "
        f"You are {bot_name!r} in the chatlog."
    )


# --- the conversation, in the prompt ---------------------------------------


def _conversation_blocks(rendered: str) -> tuple[list[str], list[list[str]]]:
    """A rendered conversation split into its preamble and its posts.

    Both renderers in this realm start every post with a bracketed speaker
    line — `[Developer] …` from `format_chatlog`, `[Developer #6478] …` from
    agfront's `format_evidence` — and a post's body may run over many lines
    after it. Anything before the first such line is the preamble the
    renderer wrote about the conversation itself (which topic, how many
    posts, whether the read was bounded); it is kept whatever else is
    dropped, because it is what says the copy is a window.
    """
    preamble: list[str] = []
    blocks: list[list[str]] = []
    for line in rendered.splitlines():
        if line.startswith("[") and "]" in line:
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
        else:
            preamble.append(line)
    return preamble, blocks


def _truncate_post(block: list[str], budget: int) -> list[str]:
    """One post cut down to `budget` characters, saying so where it was cut.

    The speaker prefix is kept whole — who said it is the half of a message
    that cannot be paraphrased — and the body is cut after it. `format_chatlog`
    puts the body on the speaker's own line and `format_evidence` puts it on
    the lines below, so the split is on the `"] "` that ends the prefix rather
    than on the line break. A post long enough to reach here is a post whose
    beginning is the part worth having; the file holds the rest.
    """
    first = block[0]
    cut = first.find("] ")
    speaker = first[: cut + 2] if cut != -1 else first + "\n"
    body = first[len(speaker):] if cut != -1 else ""
    if len(block) > 1:
        body = body + ("\n" if body or cut == -1 else "") + "\n".join(block[1:])
    room = max(0, budget - len(speaker))
    if len(body) <= room:
        return (speaker + body).splitlines() or [speaker.rstrip("\n")]
    kept = body[:room].rstrip()
    dropped = len(body) - len(kept)
    return (speaker + kept).splitlines() + [
        f"[... this message is cut off here: {dropped} more characters of it "
        f'are in "{{file}}" ...]'
    ]


def conversation_context(
    rendered: str,
    *,
    budget: int = CONVERSATION_BUDGET,
    file_name: str = "chatlog.md",
) -> str:
    """The conversation itself, as prompt text, between visible markers.

    Until `routine_tests` p2 ex1 the conversation was **only** a file, and
    the prompt said where it was (`chatlog_placement`). A reply produced in
    one turn with no tool calls therefore never opened it, and Front twice
    answered a brand-new conversation with "I don't see a message or request
    from the developer yet" while the request sat verbatim in `chatlog.md`
    (`routine_tests` p2, runs 0594 and 0597). No wording removes the
    possibility of a one-turn answer; carrying the text removes the
    possibility of a one-turn answer that saw nothing.

    `rendered` is the bytes already written to the workspace file — the same
    snapshot, with that renderer's own filtering (selfnotes, system notices,
    this bot's acks) and its own metadata (evidence keeps message ids). This
    never reads or formats a second copy: two copies of a conversation that
    could disagree is worse than one.

    The whole conversation is carried when it fits in `budget`. When it does
    not, the newest posts are carried — the newest one always, with the
    speaker line that says who wrote it — and what was left out is named
    rather than silently dropped, with the file that holds it. A single post
    too large even for that is cut **visibly**: a truncation nobody can see
    is a run confidently answering half a request.

    An empty conversation says it is empty. That is a real state (a topic
    holding nothing but this bot's own notes) and it is a different answer
    from "the conversation was not delivered".
    """
    text = rendered.strip("\n")
    lead = (
        f"The conversation you are serving is between the markers below. It is "
        f'the same text as "{file_name}" in the working directory, which is the '
        f"complete copy; read that file for anything this one does not carry. "
        f"Everything after the end marker is your own instructions, never "
        f"something somebody said to you."
    )
    if not text.strip():
        body = "(this conversation is empty: nobody has said anything in it yet)"
        return f"{lead}\n\n{CONVERSATION_BEGIN}\n{body}\n{CONVERSATION_END}"
    if len(text) <= budget:
        return f"{lead}\n\n{CONVERSATION_BEGIN}\n{text}\n{CONVERSATION_END}"

    preamble, blocks = _conversation_blocks(text)
    head = "\n".join(preamble).strip("\n")
    if not blocks:
        # Nothing this renderer marks as a post — an unreadable conversation
        # written as a note about itself, or a renderer this does not know.
        # Carry the front of it and say where it was cut.
        return (
            f"{lead}\n\n{CONVERSATION_BEGIN}\n"
            + "\n".join(_truncate_post(["[conversation]", text], budget)[1:])
            .replace("{file}", file_name)
            + f"\n{CONVERSATION_END}"
        )
    room = max(0, budget - (len(head) + 1 if head else 0))
    kept: list[list[str]] = []
    used = 0
    for block in reversed(blocks):
        rendered_block = "\n".join(block)
        if kept and used + len(rendered_block) + 1 > room:
            break
        if not kept and len(rendered_block) > room:
            # The newest post alone is over budget. It is still carried —
            # the newest real message and who sent it is the one thing this
            # exists to deliver — and it is cut where everyone can see.
            kept.append(_truncate_post(block, room))
            used = room
            break
        kept.append(block)
        used += len(rendered_block) + 1
    kept.reverse()
    omitted = len(blocks) - len(kept)
    notice = (
        f"[... {omitted} earlier message{'s' if omitted != 1 else ''} of this "
        f'conversation {"are" if omitted != 1 else "is"} not carried here; '
        f'read "{file_name}" for the whole of it ...]'
    ) if omitted > 0 else ""
    parts = [p for p in (head, notice) if p]
    parts += ["\n".join(block).replace("{file}", file_name) for block in kept]
    return f"{lead}\n\n{CONVERSATION_BEGIN}\n" + "\n".join(parts) + f"\n{CONVERSATION_END}"


def threads_placement(written, directory: Path | None = None) -> str:
    """One line naming the other conversations placed in this workspace.

    Placement, not instruction: it says where the files are, the way the
    chatlog line does. Empty when this run is party to nothing else, so a
    prompt never carries a sentence about files that are not there.

    `directory` is what the names are relative to, for a run whose working
    directory is the workspace. Leave it out — as a run that works somewhere
    else and reaches its workspace by absolute path does — and each path is
    named exactly as it was given.
    """
    written = list(written)
    if not written:
        return ""

    def name(path: Path) -> str:
        if directory is None:
            return path.as_posix()
        try:
            return path.relative_to(directory).as_posix()
        except ValueError:
            return path.as_posix()

    names = ", ".join(f'"{name(path)}"' for path in written)
    return (
        "Other conversations you have taken part in are placed beside it: "
        f"{names}. One of them is why you are running."
    )


# --- the execution command -------------------------------------------------


def apply_exec_commands(
    client: ZulipClient,
    channel: str,
    topic: str,
    history: list[dict],
    self_id: int,
    options: ExecOptions,
    *,
    log=default_log,
) -> bool:
    """Answer the execution commands awaiting this bot. True = nothing else to do.

    `ag.exec-options.v1` §2. Two things happen here and only here, so every
    agent means the same by them:

    - **A configuration-only post is not work.** When everything waiting for
      this bot is a command, the setting is answered with one deterministic
      line and no model is launched. The line is the owner's own post, which
      is what stops the topic matching the sweep forever; a reaction would
      leave the poster as the last speaker (the ComfyUI notifier reacts for
      the opposite reason — it is not the owner).
    - **A refused name is refused visibly**, and the refusal names the poster
      so they actually learn it, because an agent that asked for `opus` and
      heard nothing will ask again. A confirmation names nobody: it is not
      worth a run at the other end.

    A command sitting beside real work is *not* configuration-only. It is
    applied — `execopt.resolve` reads it off the history like any other
    directive — and the topic is served as usual; only a bad name is answered
    here, before the run, so the refusal is not buried under the reply.
    """
    if not options.supported or not options.bot:
        return False
    pending = execopt.pending_speech(history, self_id)
    asked = execopt.configuration_only(history, self_id, options.bot)
    bad = [
        name
        for message in pending
        for name in (execopt.split_commands(message.get("content"), options.bot) or ())
        if name.lower() != execopt.DEFAULT_OPTION and options.get(name) is None
    ]
    if not pending or (asked is None and not bad):
        return False
    standing = execopt.resolve(history, options.bot, known=options.names)
    poster = ""
    for message in reversed(pending):
        name = str(message.get("sender_full_name") or "").strip()
        if name:
            poster = f"@**{name}**"
            break
    for name in dict.fromkeys(bad):
        log(f"refusing execution option {name!r} in {channel!r}/{topic!r}")
        text = execopt.refusal(name, options, current=standing)
        topic_write(topic, f"{poster} {text}" if poster else text, channel=channel, client=client)
    if asked is None:
        return False  # work is waiting too; serve it under the standing selection
    good = [name for name in asked if name.lower() == execopt.DEFAULT_OPTION or options.get(name)]
    if good:
        chosen = good[-1]
        option = None if chosen.lower() == execopt.DEFAULT_OPTION else chosen
        log(f"execution option for {channel!r}/{topic!r} is now {option or 'default'}")
        topic_write(topic, execopt.confirmation(option, options), channel=channel, client=client)
    return True


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
    #: Conversations to place beside the chatlog **in addition** to the ones
    #: this bot's own root notes name. One case needs it: a callback from a
    #: topic this bot has never posted in, reached through the `replaces`
    #: relation (`agag.zulip.inherited_rootchat`). Its text is why the run is
    #: happening, and `remotes_for_home` cannot know about it, because the
    #: note that would have named it is in the conversation that was retired.
    #: Handlers that build `threads/` merge these in; everything else ignores
    #: the field.
    extra_threads: tuple[tuple[str, str], ...] = ()
    #: The execution option in force for this serving, frozen from the topic
    #: history as it stood when the serving started (`ag.exec-options.v1`).
    #: A command posted while the run is in flight has a larger message id
    #: and lands on the next serving. `Selection()` — nothing selected — is
    #: what a serving of an agent that publishes no options always gets.
    selection: Selection = field(default_factory=Selection)
    #: The serving's durable record (`agag.serving`), for a handler that
    #: wants to file its run record against it (`context.journal.record`).
    journal: object = None
    #: The record of an earlier serving of this conversation that was
    #: interrupted before it replied, when the listener found one: what it
    #: had acknowledged and processed, so the run can reconcile actions it
    #: may have started rather than repeat them blindly.
    previous: Serving | None = None
    #: The reply conversation as it stood before the run, for a serving that
    #: answers somewhere else; None when it could not be read.
    reply_history: list[dict] | None = None

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
        """Whether anybody but this bot has really said something here.

        Selfnotes do not count: a topic holding nothing but this bot's own
        machine-to-machine notes has nothing in it to answer.
        """
        return any(
            m.get("sender_id") != self.self_id and not is_selfnote(m.get("content"))
            for m in self.history
        )



@dataclass
class TopicResult:
    """What the handler produced, in three kinds, and whether to resolve.

    - `output` is the **model's output** — what the run printed, with any
      machine block the handler parses already removed. It is subject to
      the reply contract (`agag.reply`): only its `ag-reply` blocks are
      posted; the rest is the run's own. Missing, empty or unclosed marks
      are repaired once through `repair(reason) -> output` when the
      handler gives one, else answered with a visible failure line.
    - `sections` are **literal text** the handler wants posted as written:
      a deterministic status line, a canonical machine block echoed back
      as the record. Never model output.
    - `notices` are **system-generated notes** appended after the reply:
      the desire recorded, a completion checked, a block that could not be
      read. Kept apart from the reply so the mark cannot swallow them.

    The post is `reply`, then `sections`, then `notices`, joined by blank
    lines; an empty whole means no final post.
    """

    sections: list[str] = field(default_factory=list)
    resolve_after: bool = False
    output: str | None = None
    notices: list[str] = field(default_factory=list)
    repair: Callable[[str], str] | None = None


# --- the completion rule ------------------------------------------------------


def unprocessed_input(history, self_id: int, processed_up_to: int) -> list[dict]:
    """Speech by somebody else newer than the input boundary a serving
    processed. **The one completion rule**: a serving is complete for a
    conversation when this list is empty — asked after the ordinary reply,
    before a resolve, at restart recovery and after a callback, so a post
    that arrived during a run is outstanding on every path."""
    return [
        m for m in history
        if m.get("sender_id") != self_id and is_speech(m) and int(m.get("id", 0)) > processed_up_to
    ]


def requester_of(history, self_id: int, up_to: int | None = None) -> dict | None:
    """The last other speaker **within the processed input**: the person
    this serving's reply answers. `up_to` bounds the search to the input
    boundary the serving read (`TopicContext.processed_up_to`), so a third
    party speaking during the run is not who the reply is handed to.
    `handoff_mention` used to re-read the topic at send time and name
    whoever spoke last *then* — `explicit_reply` p1 step 3 records the
    relationship at intake instead."""
    for message in reversed(list(history)):
        if up_to is not None and int(message.get("id", 0)) > up_to:
            continue
        if message.get("sender_id") == self_id or not is_speech(message):
            continue
        return message
    return None


def mention_of(message: dict | None) -> str:
    name = str((message or {}).get("sender_full_name") or "").strip()
    return f"@**{name}**" if name else ""


def _resolve(client: ZulipClient, channel: str, topic: str, log) -> bool:
    """Resolve after the final reply, so the whole conversation moves under
    the ✔ name. False when it could not be done; the reason is logged."""
    try:
        tail = client.topic_history(channel, topic, num_before=1)
        if tail:
            client.resolve_topic(int(tail[-1]["id"]), topic)
        return True
    except Exception as error:  # noqa: BLE001
        log(f"could not resolve {channel!r}/{topic!r}: {error!r}")
        return False


def resume_prepared(client: ZulipClient, record: Serving, journal, *, log=default_log, **delivery) -> int:
    """Finish a serving that was interrupted between preparing its reply and
    confirming it: deliver the same text (read-back first), then the resolve
    it asked for. The model is not run again and no external action is
    repeated — the reply was the last thing left to do.

    Returns the delivered message id; raises `DeliveryError` when the reply
    still cannot be confirmed, leaving the record prepared."""
    self_id = int(client.whoami()["user_id"])
    log(f"redelivering the prepared reply of serving {record.id} to {record.reply_channel!r}/{record.reply_topic!r}")
    message_id = redeliver(client, record.reply_channel, record.reply_topic, record.reply_text or "",
                           self_id=self_id, after_id=record.reply_after, log=log, **delivery)
    journal.delivered(message_id)
    if record.resolve_after and not record.resolved:
        if _resolve(client, record.reply_channel, record.reply_topic, log):
            journal.resolved()
    return message_id


def serve_topic(
    client: ZulipClient,
    channel: str,
    topic: str,
    handler,
    *,
    ack_text: str,
    empty_reply: str | None = None,
    reply_to: tuple[str, str] | None = None,
    extra_threads: tuple[tuple[str, str], ...] = (),
    handoff: bool = True,
    history_messages: int = HISTORY_MESSAGES,
    exec_options: ExecOptions | None = None,
    journal=None,
    delivery: dict | None = None,
    log=default_log,
) -> Serving | None:
    """Serve one awaiting topic, and always answer it.

    `handler(context) -> TopicResult` does the agent-specific work. Every
    exit path after the ack posts something: an ack followed by silence would
    leave this bot as the topic's last poster, which hides the topic from the
    sweep until a human posts again.

    A human posting *during* a run is not lost either. The final reply makes
    this bot the last poster, so before leaving, this re-checks for messages
    newer than the chatlog it processed and serves the topic again — and it
    does so **before a resolve as well**: a run that asks for the topic to be
    resolved while a human has posted into it answers that post first and
    lets the next run decide about resolving (`explicit_reply` p1 step 1).

    `empty_reply`, when given, is posted instead of running the handler on a
    topic holding nothing but this bot's own messages. That is not a nicety:
    `sweep_topics` skips a topic only when its *last* poster is this bot, so
    a topic with no messages at all matches every sweep forever, and the
    reply is what silences it.

    `reply_to` is where the reply and the resolve go when that is not the
    conversation being served — a run brought back by a mention elsewhere.
    The work stays this topic's; the answer goes where the question was asked.
    Two things change in that case, and both are about not making work for
    somebody else:

    - **No ack.** The ack exists so *this* bot's own sweep skips a topic it is
      already serving. In a topic this bot does not own it buys nothing and
      costs the owner a whole serving — one triggered by "Message received",
      against a conversation that does not yet contain the reply being
      acknowledged. That is not noise; it is a run spent answering nothing.
    - **No post-run re-check.** This bot did not become the served topic's
      last poster, so anything that arrived there is found by the ordinary
      owner sweep instead of by looping here.

    Every reply is prefixed with a mention of the **requester**: the last
    other speaker within the input this serving processed, read from the
    history it was given and never re-read at send time. That is the
    turn-taking rule as code — whoever is named is served next, and a reply
    that names nobody ends the exchange — and a third party who posts while
    the run is in flight does not become the addressee of an answer that was
    not written for them.

    `exec_options`, when given, makes this serving obey `ag.exec-options.v1`:
    the topic's own execution commands are answered before anything else
    (`apply_exec_commands`), and a configuration-only post returns here
    without an ack, without a workspace and without a model. Otherwise the
    selection is **frozen** from the history this serving read — commands
    below `processed_up_to` only — and handed to the handler as
    `context.selection`, so a command posted mid-run reaches the next
    serving rather than this one.

    `extra_threads` names conversations the handler should place beside the
    chatlog on top of the ones it works out itself. The one caller is a
    callback reached through the `replaces` relation: the topic that named
    this bot holds no note of ours, so nothing else can discover that its
    text is what this serving is about.

    `handoff=False` posts the reply without that mention, for the serving
    that is a *record* rather than an answer — one whose requester is being
    given their turn back somewhere else. Naming them in both places starts
    two runs for one piece of work, which is `agent_standardize` p8's second
    open item; the caller decides which post is the one that counts.

    **The serving is journaled** (`agag.serving`): the ack's id, the input
    boundary, the reply text *before* it is sent, and the delivered id after
    it is confirmed. Under a listener the journal is the executor's durable
    record for this entry (`serving.current()`); a caller may pass its own
    as `journal`. The final send goes through `agag.delivery`: an ambiguous
    answer from the server is settled by reading the conversation back, a
    refusal or exhausted attempts raise `DeliveryError` **out of this
    function** with the reply kept prepared in the journal, and the
    listener retries the delivery — not the run — with backoff. `delivery`
    holds keyword overrides for `deliver` (attempts, backoff, sleep).

    Returns the serving record as the journal has it at the end, for a
    caller that wants the outcome (the delivered id, the reply outcome).
    """
    if is_memo_channel(channel) or (reply_to is not None and is_memo_channel(reply_to[0])):
        # Execution-time eligibility (`agag.memo`): whatever route reached
        # this call, a memo is never served and never replied into.
        log(f"{channel!r}/{topic!r} is a memo; not served")
        return None
    journal = journal or serving_record.current() or NullJournal()
    delivery = dict(delivery or {})
    self_user = client.whoami()
    self_id = int(self_user["user_id"])
    bot_name = str(self_user.get("full_name") or client.email)
    reply_channel, reply_topic = reply_to or (channel, topic)
    journal.home(channel, topic)

    replies_here = (reply_channel, reply_topic) == (channel, topic)

    while True:
        # Read before the ack when there are options to obey: a
        # configuration-only post must cost neither an ack nor a run, and the
        # ack is our own message so nothing that matters is lost by seeing
        # the topic a moment earlier.
        early: list[dict] | None = None
        if exec_options is not None:
            try:
                early = client.topic_history(channel, topic, num_before=history_messages)
            except Exception as error:  # noqa: BLE001 - fall back to the ordinary path
                log(f"could not read {channel!r}/{topic!r} for execution commands: {error!r}")
            else:
                if replies_here and apply_exec_commands(
                    client, channel, topic, early, self_id, exec_options, log=log
                ):
                    return journal.serving()

        ack_id = 0
        if replies_here:
            # Through `deliver` like the reply: an ack whose answer was lost
            # is found on read-back rather than posted twice.
            ack_id = int(deliver(client, channel, topic, ack_text, self_id=self_id,
                                 after_id=0, log=log, **delivery) or 0)
            journal.acked(ack_id)

        context = TopicContext(
            client, channel, topic, self_id, bot_name,
            reply_channel=reply_channel, reply_topic=reply_topic,
            extra_threads=tuple(extra_threads),
        )
        context.journal = journal
        context.previous = journal.previous()
        if not replies_here:
            # Who the answer elsewhere is handed to is settled *now*, before
            # the run: the last other speaker in the reply conversation as it
            # stands. A third party speaking there during the run is not the
            # addressee of an answer that was not written for them.
            try:
                context.reply_history = client.topic_history(reply_channel, reply_topic, num_before=20)
            except Exception as error:  # noqa: BLE001 - a lost mention must not cost the reply
                log(f"could not read {reply_channel!r}/{reply_topic!r} for the requester: {error!r}")
                context.reply_history = None
        result = TopicResult()
        completed = False
        try:
            context.step = "chatlog"
            context.history = early if early is not None else client.topic_history(
                channel, topic, num_before=history_messages
            )
            context.processed_up_to = max(
                (int(m.get("id", 0)) for m in context.history), default=0
            )
            if exec_options is not None:
                context.selection = execopt.resolve(
                    context.history, exec_options.bot,
                    up_to=context.processed_up_to, known=exec_options.names,
                )
            if empty_reply is not None and not context.humans_spoke():
                log(f"nothing to answer in {channel!r}/{topic!r}: no messages")
                journal.executed(context.processed_up_to, requester_id=None, requester_name="")
                journal.prepared(reply_channel, reply_topic, empty_reply, resolve_after=False,
                                 after_id=max(ack_id, context.processed_up_to))
                journal.delivered(deliver(client, reply_channel, reply_topic, empty_reply, self_id=self_id,
                                          after_id=max(ack_id, context.processed_up_to), log=log, **delivery))
                return journal.serving()
            result = handler(context)
            completed = True
        except Exception as error:  # noqa: BLE001 - the topic is the error channel
            log(f"topic workflow failed during {context.step}: {error!r}")
            result = TopicResult([f"failed during {context.step}: {error}"])

        # The requester is read from the input this serving processed — the
        # history it was handed — so it is settled before the reply exists,
        # and a lookup that would have failed at send time cannot fail here.
        requester = requester_of(context.history, self_id, context.processed_up_to)
        if not replies_here:
            # Answering somewhere else: the addressee is whoever spoke last
            # there before this serving began, which the handler may have
            # read into `context.reply_history`; otherwise one bounded read.
            requester = _reply_requester(client, context, self_id, log)
        journal.executed(context.processed_up_to,
                         requester_id=(int(requester["sender_id"]) if requester and requester.get("sender_id") is not None else None),
                         requester_name=str((requester or {}).get("sender_full_name") or ""))

        parts: list[str] = []
        if result.output is not None:
            text, split, repaired = resolve_reply(result.output, result.repair, log=log)
            journal.reply_outcome(marked=split.marked, blocks=split.blocks, failure=split.error or "")
            if repaired:
                log(f"reply for {reply_channel!r}/{reply_topic!r} came from the repair run"
                    f"{'' if split.ok else ' and still had no usable mark'}")
            if not split.ok:
                log(f"no usable reply for {reply_channel!r}/{reply_topic!r}: {split.error}; posting the failure")
            parts.append(text)
        parts += [section for section in result.sections if section]
        parts += [notice for notice in result.notices if notice]
        body = "\n\n".join(part for part in parts if part)
        after_id = max(ack_id, context.processed_up_to)
        if body:
            mention = mention_of(requester) if handoff else ""
            text = f"{mention}\n\n{body}" if mention else body
            journal.prepared(reply_channel, reply_topic, text, resolve_after=bool(result.resolve_after),
                             after_id=after_id)
            # `DeliveryError` escapes on purpose: the text is prepared and
            # the listener owns the retry.
            journal.delivered(deliver(client, reply_channel, reply_topic, text, self_id=self_id,
                                      after_id=after_id, log=log, **delivery))
        else:
            journal.prepared(reply_channel, reply_topic, "", resolve_after=bool(result.resolve_after),
                             after_id=after_id)
            journal.delivered(None)
        _annotate_record(journal)

        if result.resolve_after:
            if completed and context.replies_here and _input_arrived(client, context, self_id, history_messages, log):
                # The one completion rule, before a resolve too: a human who
                # posted during this run is answered before the topic closes.
                log(f"resolution of {channel!r}/{topic!r} deferred: input arrived during the run")
                continue
            if _resolve(client, reply_channel, reply_topic, log):
                journal.resolved()
            return journal.serving()
        if not completed:
            return journal.serving()  # do not loop on a failing topic; a human post re-arms it
        if not context.replies_here:
            return journal.serving()  # the owner sweep, not this loop, picks up what arrived

        arrived = _input_arrived(client, context, self_id, history_messages, log)
        if arrived is None:
            journal.recheck_failed("post-run re-check failed")
            return journal.serving()
        if not arrived:
            return journal.serving()
        log(f"reprocessing {channel!r}/{topic!r}: human posts arrived during the run")


def _annotate_record(journal) -> None:
    """The reply and delivery outcome beside the run identity, in the run
    record the handler filed (`context.journal.record(path)`)."""
    record = journal.serving()
    if record is None or not record.run_record:
        return
    record_reply_outcome(
        record.run_record, marked=record.reply_marked, blocks=record.reply_blocks,
        failure=record.reply_failure or None, delivered_id=record.delivered_id,
        posted_to=f"{record.reply_channel}/{record.reply_topic}" if record.reply_channel else None,
        serving_id=record.id or None,
    )


def _input_arrived(client, context, self_id, history_messages, log) -> bool | None:
    """Whether speech by somebody else landed past the processed boundary.
    None when the conversation could not be read: the journal records it and
    the listener's own evidence decides."""
    try:
        tail = client.topic_history(context.channel, context.topic, num_before=history_messages)
    except ZulipError as error:
        log(f"post-run re-check failed for {context.channel!r}/{context.topic!r}: {error!r}")
        return None
    return bool(unprocessed_input(tail, self_id, context.processed_up_to))


def _reply_requester(client, context, self_id, log) -> dict | None:
    """Who a reply posted *elsewhere* is handed to: the last other speaker
    in the reply conversation as it stood when this serving started
    (`context.reply_history`, read before the run). A read that failed then
    hands the turn to nobody, and the journal's requester stays empty so the
    failure is visible rather than guessed at send time."""
    del client, log
    history = context.reply_history
    if history is None:
        return None
    return requester_of(history, self_id)
