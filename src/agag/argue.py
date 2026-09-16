"""Argue: the conversation in which a human develops a desire with the agents.

`argue` p1. An argue is one Zulip topic — `#argue › argue-<stem>` — where a
human states a vague, far-reaching desire and every agent in the realm can
be asked to help develop it. One agent (Front) *owns* the topic: it is served
automatically whenever anybody else speaks, and it facilitates. Every other
agent takes part only when it is **named**, and answers in the same topic.

What this module fixes, so that every participant means the same thing:

- **A mention is an invitation.** `@**<bot>**` in a post is a deliberate
  request for that agent's contribution, and it costs that agent a run.
  Nobody names the previous speaker out of habit here: the owner's replies
  carry no automatic hand-off mention, and a participant's reply names
  nobody. Naming is a decision.
- **One account may speak as several logical participants.** A selector
  right after the mention — `@**archsage** sage:arxiv` — addresses one role
  of that account. The reply carries a **speaker header** naming the role
  it came from, and an invitation with a selector is answered only by a
  reply with that header. Parsed here, never by a model.
- **An invitation is outstanding until a reply answers it**, judged from
  the conversation: an invitation at message *i* to speaker *s* is answered
  when the account posted speech after *i* under header *s*. Several
  invitations in one post, several posts before anybody answers, a listener
  restart — all read off the same rule, so nothing is erased and nothing
  finished is routinely replayed. The listener's coarse "how far have I
  answered here" mark (`[selfnote][served]`) is written **into the argue
  topic itself** once a serving has answered everything it found.
- **The human's desire is a message id and an author**, recorded as
  `[selfnote][desire] <message id> by <user id>` by the owner's listener
  after checking that the message exists in the conversation and that its
  sender is a human. The owner may help formulate the desire, and its draft
  is not the submission; a human posting the statement, or a human posting
  "yes, that is it" under the draft, is. Nothing here authenticates anybody
  beyond what Zulip already says about a sender.
- **The argue's identity is a message id**: `[selfnote][argue] from
  <channel>/<topic>` (or `from -` for one opened by hand), written when the
  topic is opened, naming the ordinary conversation it grew out of. A
  resolved (`✔ `) argue topic is one whose discussion has ended; the
  studies and projects it created are not resolved with it.

Everything a participant needs — the conversation, the common guide, its
own short role context — is composed by `participant_prompt`, and
`participate` is the whole serving for an agent brought here by a mention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .selfnote import Conversation, is_selfnote, is_speech, note, parse_conversation, parse_note
from .topics import (
    chatlog_path,
    chatlog_placement,
    conversation_context,
    format_chatlog,
    generation_dir,
    next_generation,
    next_record_path,
    topic_workspace,
)
from .zulip import RESOLVED_TOPIC_PREFIX, ZulipClient, log as default_log

#: Where argue topics live, and how they are named.
ARGUE_CHANNEL = "argue"
ARGUE_TOPIC_PREFIX = "argue-"
#: `[selfnote][argue] from <channel>/<topic>` — its own id is the argue's identity.
ARGUE_TAG = "argue"
#: `[selfnote][desire] <message id> by <user id>` — the human's submission.
DESIRE_TAG = "desire"
#: What `from` says when an argue was opened by hand rather than from a conversation.
NO_ORIGIN = "-"
#: The fenced block the owner's run may end its reply with.
BLOCK_LANGUAGE = "ag-argue"
#: A participant's whole reply when its run produced nothing.
NO_ANSWER = "I have nothing to add here."
#: How long a participant's run may take.
PARTICIPANT_TIMEOUT_SECONDS = 600
#: The role a participating agent runs, and the guide directory beside it.
ROLE = "argue"

_MENTION = re.compile(r"@\*\*(?P<name>[^*\n]+?)\*\*(?:[ \t]+(?P<selector>[a-z][\w-]*:[\w.-]+))?")
_SPEAKER = re.compile(r"^\*\*\[(?P<selector>[a-z][\w-]*:[\w.-]+)\]\*\*[ \t]*\n")
_FENCE = re.compile(r"^(`{3,}|~{3,}).*?^\1[ \t]*$", re.MULTILINE | re.DOTALL)
_BLOCK = re.compile(r"^```" + BLOCK_LANGUAGE + r"[ \t]*\n(?P<body>.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_SELECTOR = re.compile(r"^[a-z][\w-]*:[\w.-]+$")

__all__ = [
    "ARGUE_CHANNEL",
    "ARGUE_TAG",
    "ARGUE_TOPIC_PREFIX",
    "BLOCK_LANGUAGE",
    "DESIRE_TAG",
    "NO_ANSWER",
    "NO_ORIGIN",
    "PARTICIPANT_TIMEOUT_SECONDS",
    "ROLE",
    "Anchor",
    "Desire",
    "Invitation",
    "anchor",
    "argue_note",
    "desire_note",
    "desire_placement",
    "invitations",
    "is_argue_topic",
    "mentions_of",
    "open_argue",
    "outstanding",
    "parse_argue",
    "parse_desire",
    "parse_selector",
    "participant_guide",
    "participant_prompt",
    "participate",
    "recorded_desire",
    "speaker_of",
    "split_block",
    "validate_desire",
    "with_speaker",
    "without_fences",
]


def is_argue_topic(channel: str, topic: str) -> bool:
    """Whether a conversation is an argue: in the argue channel, under the
    argue prefix (the `✔ ` of a resolved one stripped first)."""
    bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
    return channel == ARGUE_CHANNEL and bare.startswith(ARGUE_TOPIC_PREFIX)


# --- mentions and speakers ---------------------------------------------------


def without_fences(content: str) -> str:
    """The text with fenced code blocks removed — a mention inside a fence
    is not a mention, which is how one is quoted without firing it."""
    return _FENCE.sub("", str(content or ""))


def parse_selector(value: str | None) -> str | None:
    """A well-formed `<kind>:<name>` selector, or None."""
    text = (value or "").strip()
    return text if text and _SELECTOR.match(text) else None


def mentions_of(content: str, bot_name: str) -> list[str | None]:
    """The selector of every mention of `bot_name` in `content`, in order —
    None where the account itself is addressed. Silent mentions (`@_**`)
    are not mentions."""
    if not bot_name:
        return []
    found: list[str | None] = []
    for match in _MENTION.finditer(without_fences(content)):
        if match.group("name") == bot_name:
            found.append(match.group("selector"))
    return found


def with_speaker(selector: str | None, body: str) -> str:
    """A reply as a logical speaker posts it: the header, then the body.
    No header for the account itself."""
    body = str(body or "").strip()
    return f"**[{selector}]**\n{body}" if selector else body


def speaker_of(content: str) -> str | None:
    """The speaker header a post carries, or None for the account itself."""
    match = _SPEAKER.match(str(content or "").lstrip())
    return match.group("selector") if match else None


# --- invitations ---------------------------------------------------------------


@dataclass(frozen=True)
class Invitation:
    """One request for a contribution: the post that named this account,
    who wrote it, and which logical speaker it addressed (None: the
    account itself)."""

    message_id: int
    sender_id: int
    sender_name: str
    selector: str | None

    @property
    def speaker(self) -> str:
        return self.selector or "(default)"


def invitations(messages: Iterable[dict], self_id: int, bot_name: str) -> list[Invitation]:
    """Every invitation to this account in a conversation, oldest first."""
    found: list[Invitation] = []
    for message in messages:
        if message.get("sender_id") == self_id or not is_speech(message):
            continue
        for selector in mentions_of(str(message.get("content", "")), bot_name):
            found.append(Invitation(
                int(message["id"]), int(message.get("sender_id") or 0),
                str(message.get("sender_full_name") or ""), selector,
            ))
    return found


def outstanding(messages: Iterable[dict], self_id: int, bot_name: str) -> list[Invitation]:
    """The invitations this account has not answered yet, oldest first.

    An invitation is answered by a later post of this account carrying the
    same speaker header — none for the account itself. Read off the
    conversation alone, so a restart, a burst of posts, or two invitations
    in one message all get the same answer.
    """
    rows = list(messages)
    replies = [
        (int(m["id"]), speaker_of(str(m.get("content", ""))))
        for m in rows if m.get("sender_id") == self_id and is_speech(m)
    ]
    return [
        invitation for invitation in invitations(rows, self_id, bot_name)
        if not any(ident > invitation.message_id and speaker == invitation.selector for ident, speaker in replies)
    ]


# --- the argue's own notes -------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """The argue's identity: the note's own id, and where it was opened from."""

    message_id: int
    origin: Conversation | None

    @property
    def label(self) -> str:
        return f"argue {self.message_id}"


def argue_note(origin: Conversation | None) -> str:
    return note(ARGUE_TAG, f"from {origin if origin is not None else NO_ORIGIN}")


def parse_argue(content) -> tuple[bool, Conversation | None]:
    """`(is an argue note, origin)`; the origin is None for a hand-opened one."""
    value = parse_note(content, ARGUE_TAG)
    if value is None or not value.startswith("from "):
        return False, None
    rest = value[len("from "):].strip()
    if rest == NO_ORIGIN:
        return True, None
    origin = parse_conversation(rest)
    return (True, origin) if origin is not None else (False, None)


def anchor(messages: Iterable[dict]) -> Anchor | None:
    """The earliest argue note in a conversation: an argue is opened once."""
    for message in messages:
        ok, origin = parse_argue(message.get("content"))
        if ok and message.get("id") is not None:
            return Anchor(int(message["id"]), origin)
    return None


@dataclass(frozen=True)
class Desire:
    message_id: int
    user_id: int


def desire_note(message_id: int, user_id: int) -> str:
    return note(DESIRE_TAG, f"{int(message_id)} by {int(user_id)}")


def parse_desire(content) -> Desire | None:
    value = parse_note(content, DESIRE_TAG)
    if value is None:
        return None
    parts = value.split()
    if len(parts) != 3 or parts[1] != "by":
        return None
    try:
        return Desire(int(parts[0]), int(parts[2]))
    except ValueError:
        return None


def recorded_desire(messages: Iterable[dict]) -> Desire | None:
    """The desire on record: the earliest valid desire note. Recorded once."""
    for message in messages:
        desire = parse_desire(message.get("content"))
        if desire is not None:
            return desire
    return None


def validate_desire(
    history: Iterable[dict], message_id: int, *, self_id: int, is_human: Callable[[int], bool],
) -> tuple[Desire | None, str | None]:
    """Whether `message_id` may be recorded as the human's desire.

    It must be a real post in this conversation, not a selfnote, and by a
    human — never by this bot or any bot. The reasons are returned in words
    because the owner's listener records them where the run can read them
    next time, rather than silently dropping a designation.
    """
    rows = {int(m["id"]): m for m in history if m.get("id") is not None}
    message = rows.get(int(message_id))
    if message is None:
        return None, f"message {message_id} is not in this conversation"
    if is_selfnote(message.get("content")):
        return None, f"message {message_id} is a note, not a statement"
    sender = message.get("sender_id")
    if sender is None or int(sender) == self_id:
        return None, f"message {message_id} is your own post; the desire must be the human's"
    if not is_human(int(sender)):
        return None, f"message {message_id} was posted by a bot, not a human"
    return Desire(int(message_id), int(sender)), None


# --- the owner's fenced block ------------------------------------------------------


def split_block(output: str) -> tuple[str, dict[str, str] | None, str | None]:
    """`(reply without the block, the block's fields, error)`.

    The block is `key: value` lines in a ```ag-argue fence. A malformed line
    is an error rather than a silent skip, and the fence is removed from
    the visible reply either way.
    """
    match = _BLOCK.search(output or "")
    if match is None:
        return (output or "").strip(), None, None
    text = (output[: match.start()] + output[match.end():]).strip()
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            return text, None, f"unreadable line in the {BLOCK_LANGUAGE} block: {line.strip()!r}"
        fields[key.strip().lower()] = value.strip()
    return text, fields, None


# --- opening one ---------------------------------------------------------------------


def open_argue(
    client: ZulipClient, stem: str, text: str, *, origin: Conversation | None,
) -> tuple[str, int, int]:
    """Open `#argue › argue-<stem>` with the anchor note and the first post.

    Returns `(topic, anchor id, post id)`. The channel is created by the
    subscription when the realm lacks it; a stem already in use is refused,
    because an argue is one conversation and a second under the same name
    would merge two."""
    stem = stem.strip()
    if not stem:
        raise ValueError("an argue needs a stem")
    topic = stem if stem.startswith(ARGUE_TOPIC_PREFIX) else f"{ARGUE_TOPIC_PREFIX}{stem}"
    client.ensure_subscribed(ARGUE_CHANNEL)
    if client.topic_last_id(ARGUE_CHANNEL, topic) or client.topic_last_id(
        ARGUE_CHANNEL, f"{RESOLVED_TOPIC_PREFIX}{topic}"
    ):
        raise ValueError(f"#{ARGUE_CHANNEL} > {topic} already exists; choose another stem")
    anchor_id = client.send_to_channel(ARGUE_CHANNEL, topic, argue_note(origin))
    post_id = client.send_to_channel(ARGUE_CHANNEL, topic, text.strip())
    return topic, anchor_id, post_id


# --- what a participant is told ----------------------------------------------------


def participant_guide() -> str:
    """The guide every participant reads, whoever it is."""
    return (
        "This conversation is an *argue*: a human is developing a desire — often vague "
        "and far-reaching at first — together with every agent in this system. Its "
        "owner facilitates; the other agents, you included, take part when they are "
        "named. You have been named, which is a deliberate request for your "
        "contribution and nothing else.\n\n"
        "Read the whole conversation first. Then answer what you were asked, in "
        "service of the human's desire: from what you know, what you can observe and "
        "what you can do — your own capabilities, your own evidence. Say plainly what "
        "you do not know or cannot answer; a gap named is more useful than a guess. "
        "Where you can, name the concrete next steps you see and who or what would be "
        "needed for them, without starting any work yourself.\n\n"
        "Your reply is posted into the argue topic as it is. Address the people in "
        "the conversation; do not name any agent with an `@**…**` mention unless you "
        "actually need that agent's contribution, because a mention here is a request "
        "that costs a run. The reply is the closing message of this run — do not post "
        "it yourself."
    )


def desire_placement(desire: Desire | None, history: Iterable[dict] | None = None) -> str:
    """One line saying whether the human's desire is on record, and which post it is."""
    if desire is None:
        return "The human's desire has not been recorded yet."
    text = ""
    for message in history or ():
        if int(message.get("id", 0)) == desire.message_id:
            text = str(message.get("content", "")).strip()
            break
    quoted = f': "{text[:400]}"' if text else ""
    return f"The human's desire on record is message {desire.message_id}{quoted}"


def participant_prompt(
    bot_name: str, conversation: str, role_context: str, *, speaker: str | None = None,
    desire: Desire | None = None, history: Iterable[dict] | None = None,
) -> str:
    """Placement, the conversation, the common guide, then the role's own context."""
    who = chatlog_placement(bot_name)
    if speaker:
        who += f" You are taking part as the logical participant {speaker!r}; that is the name others use for you here."
    lines = [who, desire_placement(desire, history), "", conversation, "", participant_guide()]
    context = (role_context or "").strip()
    if context:
        lines += ["", "About you, in this conversation:", "", context]
    return "\n".join(lines)


# --- the serving ---------------------------------------------------------------------


def participate(
    client: ZulipClient,
    channel: str,
    topic: str,
    *,
    spec,
    role_context: str,
    role: str = ROLE,
    speaker: str | None = None,
    selectors: Iterable[str | None] | None = None,
    run: Callable[..., str] | None = None,
    history_messages: int = 1000,
    drop: Callable[[str], bool] | None = None,
    timeout: float = PARTICIPANT_TIMEOUT_SECONDS,
    log=default_log,
) -> list[int]:
    """Answer every outstanding invitation to this account in an argue topic.

    For each invitation: a fresh generation workspace under the agent's
    `.local/topics/`, the conversation as `chatlog.md`, one run of `role`
    with `participant_prompt`, and one post carrying the invitation's
    speaker header. No acknowledgement is posted — this is not this bot's
    topic, and an ack would be a post in the owner's conversation. The
    invitation is reacted to instead, which wakes nobody.

    `selectors` are the logical speakers this account answers for; an
    invitation addressed to any other selector is answered with one line
    saying so, without a run. `run(prompt, cwd, invitation)` replaces the
    default `run_role` for an agent whose logical speakers need their own
    working directory or tools (archsage's sages). The served mark is
    written after everything found has been answered, so a crash midway
    leaves the rest owed rather than silently spent.

    Returns the ids of the posts made.
    """
    from .agent import run_role
    from .zulip import mark_served

    self_user = client.whoami()
    self_id = int(self_user["user_id"])
    bot_name = str(self_user.get("full_name") or client.email)
    history = client.topic_history(channel, topic, num_before=history_messages)
    pending = outstanding(history, self_id, bot_name)
    if not pending:
        log(f"no outstanding invitation for {bot_name!r} in {channel!r}/{topic!r}")
        return []
    known = None if selectors is None else {s for s in selectors}
    conversation_rows = [m for m in history if not (drop is not None and drop(str(m.get("content", ""))))]
    rendered = format_chatlog(conversation_rows, self_id)
    desire = recorded_desire(history)
    posted: list[int] = []
    for invitation in pending:
        try:
            client.add_reaction(invitation.message_id, "eyes")
        except Exception as error:  # noqa: BLE001 - a reaction is a courtesy
            log(f"could not react to {invitation.message_id}: {error!r}")
        if known is not None and invitation.selector not in known:
            names = ", ".join(sorted(s for s in known if s)) or "none"
            text = with_speaker(invitation.selector, (
                f"There is no participant {invitation.selector!r} on this account. "
                f"The ones I answer for are: {names}."
            ))
            posted.append(client.send_to_channel(channel, topic, text))
            log(f"refused unknown selector {invitation.selector!r} in {channel!r}/{topic!r}")
            continue
        label = invitation.selector or speaker
        number = next_generation(topic_workspace(spec.topics_root, channel, topic))
        workspace = generation_dir(spec.topics_root, channel, topic, number, role)
        chatlog_path(workspace).write_text(rendered, encoding="utf-8")
        prompt = participant_prompt(
            bot_name, conversation_context(rendered), role_context,
            speaker=label, desire=desire, history=history,
        )
        meta = {"argue": (anchor(history).message_id if anchor(history) else None),
                "invitation": invitation.message_id, "speaker": label or bot_name}
        try:
            if run is not None:
                output = run(prompt, workspace, invitation)
            else:
                output, _, exit_code = run_role(
                    spec, role, prompt, cwd=workspace, timeout=timeout,
                    record=next_record_path(spec.records_root / role),
                    transcript=workspace / "transcript.jsonl", stream=True,
                    extra_meta={k: v for k, v in meta.items() if v is not None},
                )
                if exit_code != 0:
                    raise RuntimeError(f"{role} run exited {exit_code}: {output.strip()[:500]}")
            body = output.strip() or NO_ANSWER
        except Exception as error:  # noqa: BLE001 - the topic is the error channel
            log(f"participation failed in {channel!r}/{topic!r}: {error!r}")
            body = f"failed while answering: {error}"
        posted.append(client.send_to_channel(channel, topic, with_speaker(invitation.selector, body)))
        log(f"answered invitation {invitation.message_id} as {label or bot_name!r} in {channel!r}/{topic!r}")
    here = Conversation(channel, topic)
    try:
        mark_served(client, here, here, max(i.message_id for i in pending))
    except Exception as error:  # noqa: BLE001 - the replies are the record; the mark is a bound
        log(f"could not write the served mark in {channel!r}/{topic!r}: {error!r}")
    return posted


def role_context_path(root: Path, role: str = ROLE) -> Path:
    """`<root>/agent/guides/<role>/role.md` — the agent's own short context."""
    return root / "agent" / "guides" / role / "role.md"
