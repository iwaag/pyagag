"""Execution options: the public name a requester asks an agent to run under.

`ag.exec-options.v1` (`docs/exec-options-v1.md`) is the whole contract; this
module is the parsing, the resolution and the record fields it needs, written
once so every agent means the same thing by them.

The problem it solves is coupling. "Run it using agy" names a *way of
executing*, and until this contract the only name for one was the recipient's
own `agents.toml` profile — so a requester had to read the recipient's
configuration, and the loosely-coupled conversational delegation the system is
built on became a shared implementation detail. So there are two names:

- an **option** is public. An agent advertises it in its `#agents`
  introduction with the usage pool it consumes and the work it covers, and
  that block is generated from the running instance so it cannot drift.
- a **profile** is private. The agent maps an option to one (or several) of
  its own, and nobody outside ever names one.

Three things a reader of this module should know before using it:

- **The topic is the store.** A selection is the newest command *post* in the
  conversation (or an inherited snapshot note), so a listener restart
  re-derives it, and "why did this run on agy" is answered by reading the
  topic. There is no selection file anywhere.
- **A selection is frozen at a serving's start.** `resolve` takes an `up_to`
  message id; a command posted while a run is in flight has a larger id and
  therefore applies to the next serving. Nothing reaches into a running
  process.
- **Unknown is an answer.** `parse_options` returns `None` for a post with no
  block, and a consumer must keep that as *unknown* rather than "supports
  nothing" — the same rule, for the same reason, as `agag.intro.parse_roster`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .selfnote import Conversation, is_speech, note, parse_conversation, parse_note

SCHEMA = "ag.exec-options.v1"
#: The fence language of the block in an introduction. Fenced, so it is not
#: prose a human skims past and — like the roster block — nothing inside it
#: can fire a mention.
FENCE = "agag-exec"
#: The option meaning "no explicit selection": fall through to the agent's own
#: configured defaults. Always advertised by a supporting agent, and the word
#: the reset command uses, because a reset has no other honest meaning.
DEFAULT_OPTION = "default"
#: The command verb. `@**<bot>** use <option>`.
VERB = "use"
#: A field with nothing to say, written rather than omitted.
NONE = "-"
#: The selfnote tag carrying an inherited selection into a child conversation.
EXEC_TAG = "exec"

HEADING = "## Execution options"
PREAMBLE = (
    "How to ask me for a particular way of executing. These are public names,\n"
    "not my internal configuration: post the command line below in the topic\n"
    "whose work you want run that way, on its own, and it applies from my next\n"
    "serving of that topic onward. `default` resets it."
)

__all__ = [
    "DEFAULT_OPTION",
    "EXEC_TAG",
    "FENCE",
    "NONE",
    "SCHEMA",
    "VERB",
    "ExecOptions",
    "Option",
    "Selection",
    "command_line",
    "configuration_only",
    "confirmation",
    "exec_note",
    "options_block",
    "parse_command",
    "parse_exec_note",
    "parse_options",
    "pending_speech",
    "refusal",
    "resolve",
    "run_meta",
    "split_commands",
    "with_default",
]


# --- what an agent publishes ----------------------------------------------


@dataclass(frozen=True)
class Option:
    """One published execution option.

    `pool` is the usage pool it consumes — the shared account window a
    condition like "until usage exceeds 70 %" is judged against — and it is
    the field that makes a threshold answerable at all: an option and an
    observation that are not of the same pool do not compare.

    `covers` is what the option applies to in the agent's own words, because
    an agent whose auxiliary roles stay on another pool must be able to say
    so rather than imply "everything".
    """

    name: str
    pool: str = NONE
    covers: str = ""
    summary: str = ""

    def line(self) -> str:
        parts = [self.name, f"pool: {self.pool or NONE}", f"covers: {self.covers or NONE}"]
        if self.summary:
            parts.append(self.summary)
        return "option: " + " | ".join(parts)

    def describe(self) -> str:
        """One human phrase, for a confirmation or a refusal."""
        bits = [bit for bit in (self.summary, f"pool `{self.pool}`" if self.pool and self.pool != NONE else "",
                                f"covers {self.covers}" if self.covers else "") if bit]
        return f"`{self.name}`" + (f" ({'; '.join(bits)})" if bits else "")


@dataclass(frozen=True)
class ExecOptions:
    """What one instance publishes, and what it will accept.

    `supported=False` is a real, published answer — "I was asked and I do not
    do this" — and differs from a post with no block at all, which is
    *unknown*.
    """

    bot: str
    options: tuple[Option, ...] = ()
    supported: bool = True

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(option.name for option in self.options)

    def get(self, name: str) -> Option | None:
        wanted = (name or "").strip().lower()
        for option in self.options:
            if option.name.lower() == wanted:
                return option
        return None

    def command_line(self) -> str:
        return command_line(self.bot)

    def block(self) -> str:
        """The fenced block, as it goes into the introduction."""
        lines = [
            f"schema: {SCHEMA}",
            f"supported: {'yes' if self.supported else 'no'}",
            f"command: {self.command_line()}",
        ]
        lines.extend(option.line() for option in self.options)
        body = "\n".join(lines)
        return f"{HEADING}\n\n{PREAMBLE}\n\n```{FENCE}\n{body}\n```"


def with_default(bot: str, options: Iterable[Option], *, pool: str = NONE,
                 summary: str = "my configured defaults") -> ExecOptions:
    """`options` with `default` guaranteed first — the shape agents publish.

    A supporting agent always advertises `default`, because the reset command
    names it and a menu that cannot say "go back" is not a menu.
    """
    listed = [option for option in options if option.name != DEFAULT_OPTION]
    return ExecOptions(
        bot,
        (Option(DEFAULT_OPTION, pool, "everything", summary), *listed),
    )


def command_line(bot: str, option: str = "<option>") -> str:
    return f"@**{bot}** {VERB} {option}"


def options_block(options: ExecOptions) -> str:
    return options.block()


_BLOCK = re.compile(
    rf"^[ \t]*```[ \t]*{FENCE}[ \t]*$\n(.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def parse_options(text: str) -> ExecOptions | None:
    """The `ExecOptions` an introduction declares, or None for *unknown*.

    None must stay None in the caller. A post carrying no block is an
    instance whose execution options are unknown — possibly because it
    predates this contract — and an observer that reads that as "supports
    nothing" has invented the answer, which is exactly the failure the roster
    block was added to abolish.

    The **last** block wins, so a quoted example in the prose above (this
    contract quotes its own command) cannot outrank the real one.
    """
    matches = _BLOCK.findall(text or "")
    if not matches:
        return None
    bot = ""
    supported = True
    options: list[Option] = []
    for raw in matches[-1].splitlines():
        key, sep, value = raw.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "supported":
            supported = value.lower() in {"yes", "true", "1"}
        elif key == "command":
            found = re.search(r"@\*\*(.+?)\*\*", value)
            if found:
                bot = found.group(1).strip()
        elif key == "option":
            option = _parse_option_line(value)
            if option is not None:
                options.append(option)
    return ExecOptions(bot, tuple(options), supported)


def _parse_option_line(value: str) -> Option | None:
    parts = [part.strip() for part in value.split("|")]
    name = parts[0] if parts else ""
    if not name:
        return None
    pool, covers, summary = NONE, "", ""
    tail: list[str] = []
    for part in parts[1:]:
        low = part.lower()
        if low.startswith("pool:"):
            pool = part.split(":", 1)[1].strip() or NONE
        elif low.startswith("covers:"):
            covers = part.split(":", 1)[1].strip()
            covers = "" if covers == NONE else covers
        elif part:
            tail.append(part)
    summary = " | ".join(tail)
    return Option(name, pool, covers, summary)


# --- the topic command -----------------------------------------------------

#: One command line and nothing else. The mention is Zulip's raw markdown
#: (`apply_markdown=false` everywhere in `agag.zulip`), so this is what a
#: reader sees, not rendered HTML.
_COMMAND = re.compile(
    rf"^@\*\*(?P<bot>[^*]+)\*\*[ \t]*(?::)?[ \t]*{VERB}[ \t]+(?P<option>[A-Za-z0-9._-]+)[ \t]*\.?$"
)


def split_commands(content, bot: str) -> list[str] | None:
    """The options a *configuration-only* message asks for, or None.

    None means "this is not a command message": either it addresses somebody
    else, or it is ordinary speech. The rule is deliberately strict — every
    non-blank line must be a command line addressed to `bot` — because the
    alternative is that mentioning the command while discussing it fires it.
    A fenced example therefore cannot be a command, without the parser
    needing to know what a fence is: a fence line is not a command line.

    Several lines are allowed and the last one wins, so a correction posted
    under a typo in the same message behaves like a correction posted after
    it.
    """
    text = str(content or "").strip()
    if not text or not bot:
        return None
    found: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        match = _COMMAND.match(line)
        if match is None or match.group("bot").strip() != bot:
            return None
        found.append(match.group("option").strip())
    return found or None


def parse_command(content, bot: str) -> str | None:
    """The single option a command message selects, or None if it is not one."""
    options = split_commands(content, bot)
    return options[-1] if options else None


# --- inheritance -----------------------------------------------------------


def exec_note(option: str | None, source: Conversation, message_id: int | None = None) -> str:
    """The snapshot a parent writes into a child conversation it opens.

    A selfnote, so it is hidden from every chatlog and — the part that
    matters — it is never counted as somebody speaking, so writing it never
    buys the child's owner a run (`agag.selfnote`).

    A snapshot, not a reference: the child keeps what it was opened with even
    when the parent changes later, because its work is already under way.
    Provenance travels with it so the child can say where its selection came
    from.
    """
    where = f"{source}#{int(message_id)}" if message_id is not None else str(source)
    return note(EXEC_TAG, f"{option or DEFAULT_OPTION} from {where}")


def parse_exec_note(content) -> tuple[str, Conversation | None, int | None] | None:
    """`(option, source conversation, source message id)` of a snapshot note."""
    value = parse_note(content, EXEC_TAG)
    if value is None:
        return None
    option, _, where = value.partition(" from ")
    option = option.strip()
    if not option:
        return None
    where = where.strip()
    message_id: int | None = None
    if "#" in where:
        where, _, tail = where.rpartition("#")
        if tail.isdigit():
            message_id = int(tail)
    return option, parse_conversation(where), message_id


# --- resolution ------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """The execution option in force for one serving.

    `option` is None when nothing was selected, which is not the same as
    `default` having been asked for — but it resolves to the same run, so
    `profile_override` is None in both cases and only the record tells them
    apart.
    """

    option: str | None = None
    source: str = "default"
    message_id: int | None = None
    #: Where an inherited selection came from, for provenance in a report.
    inherited_from: Conversation | None = None

    @property
    def explicit(self) -> bool:
        return self.option is not None


def resolve(
    messages, bot: str, *, up_to: int | None = None, known: Iterable[str] | None = None
) -> Selection:
    """The selection in force, from the topic's own history.

    Precedence is one rule, not a ladder: the **newest execution directive**
    in this conversation wins, whether it is a command post or an inherited
    snapshot note, and with nothing there the agent's configured defaults
    apply. That makes "a child overrides its inherited selection by posting a
    command" fall out of message ordering rather than out of a special case.

    `up_to` is the freeze: only directives at or below that message id are
    considered, so a command that arrived while a run was in flight applies
    to the next serving. Callers pass the serving's `processed_up_to`.

    `known` is the published menu. A directive naming something not on it is
    **skipped**, which is the contract's "the previously effective selection
    continues": a refused name must not become the topic's setting, or a
    typo would silently stop the topic running on what it was running on.
    """
    allowed = None if known is None else {str(name).lower() for name in known} | {DEFAULT_OPTION}
    best: tuple[int, Selection] | None = None
    for message in messages or ():
        identifier = int(message.get("id", 0) or 0)
        if up_to is not None and identifier > up_to:
            continue
        content = message.get("content")
        selection: Selection | None = None
        option = parse_command(content, bot)
        if option is not None and allowed is not None and option.lower() not in allowed:
            continue
        if option is not None:
            selection = Selection(
                None if option.lower() == DEFAULT_OPTION else option,
                "topic",
                identifier or None,
            )
        else:
            parsed = parse_exec_note(content)
            if parsed is not None:
                name, source, source_id = parsed
                if allowed is not None and name.lower() not in allowed:
                    continue
                selection = Selection(
                    None if name.lower() == DEFAULT_OPTION else name,
                    "inherited",
                    source_id if source_id is not None else (identifier or None),
                    source,
                )
        if selection is not None and (best is None or identifier >= best[0]):
            best = (identifier, selection)
    return best[1] if best is not None else Selection()


def pending_speech(messages, self_id: int) -> list[dict]:
    """The speech newer than this bot's own last post — what awaits an answer.

    Selfnotes and Zulip's system notices are not speech and never appear
    here (`agag.selfnote.is_speech`), which is what lets the caller ask "is
    everything waiting for me a configuration command?" without re-deriving
    that rule.
    """
    ordered = list(messages or ())
    start = 0
    for index, message in enumerate(ordered):
        if message.get("sender_id") == self_id:
            start = index + 1
    return [message for message in ordered[start:] if is_speech(message)]


def configuration_only(messages, self_id: int, bot: str) -> list[str] | None:
    """The options asked for when *everything* awaiting an answer is a command.

    None when there is real work waiting too: the selection is still applied,
    but the topic is served as usual. That distinction is the contract's
    "configuration-only posts update the setting without starting agent
    work", and it is decided from the messages rather than from a flag, so a
    command that arrives during a run is classified the same way on the
    re-check as it would have been on a fresh sweep.
    """
    pending = pending_speech(messages, self_id)
    if not pending:
        return None
    asked: list[str] = []
    for message in pending:
        options = split_commands(message.get("content"), bot)
        if options is None:
            return None
        asked.extend(options)
    return asked or None


# --- what a run records ----------------------------------------------------


def run_meta(selection: Selection | None) -> dict:
    """The `ag.agent-run.v1` fields for a selection.

    Beside the harness record's own `profile`/`harness`/`model`, never
    instead of them: the option is what was *asked for* and those are what
    actually ran. A run with no selection still records `exec_source:
    default`, because "nobody asked" and "this predates the contract" are
    different answers.
    """
    selection = selection or Selection()
    meta: dict = {"exec_source": selection.source}
    if selection.option is not None:
        meta["exec_option"] = selection.option
    if selection.message_id is not None:
        meta["exec_message_id"] = selection.message_id
    if selection.inherited_from is not None:
        meta["exec_inherited_from"] = str(selection.inherited_from)
    return meta


# --- the answers a listener posts -----------------------------------------


def confirmation(option: str | None, options: ExecOptions) -> str:
    """The one deterministic line a configuration-only post is answered with.

    A line rather than a reaction, and this is the one place the ComfyUI
    notifier's precedent inverts: the notifier reacts because a *post* by a
    non-owner in a topic serves that topic's owner and buys a run. Here the
    poster *is* answering the owner, so a reaction would leave them as the
    last speaker and the topic would match every sweep forever. The owner's
    own line is what settles the topic, and it costs no model.
    """
    if option is None:
        return (
            "Execution option reset: this topic follows my configured "
            "defaults from my next serving onward."
        )
    known = options.get(option)
    described = known.describe() if known else f"`{option}`"
    return (
        f"Execution option set to {described} for this topic, "
        "from my next serving onward."
    )


def refusal(option: str, options: ExecOptions, *, current: Selection | None = None) -> str:
    """A visible refusal, naming what *is* published. Never a downgrade.

    The previously effective selection is stated, because the poster's next
    question is always "so what is it running on now?" and an unanswered
    refusal invites a second guess at a profile name.
    """
    published = ", ".join(f"`{name}`" for name in options.names) or "none"
    current = current or Selection()
    standing = (
        f"`{current.option}`" if current.option else "my configured defaults"
    )
    return (
        f"I do not publish an execution option named `{option}`. "
        f"Mine are: {published}. This topic still runs on {standing}."
    )
