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
- **Posting is participating.** Zulip lets a bot read any public channel
  unsubscribed, and `read`/`topics` keep doing exactly that. `send` is
  different: posting somewhere is the decision to be part of that
  conversation, so it subscribes the sender to that channel and — once per
  topic, before the first real post — writes a `[selfnote][rootchat]` note
  saying which of this agent's own conversations it is here on behalf of.
  That note is what lets the answer find this agent later: the run that
  posted is long over by then, and the memory of it lives in the chat rather
  than in any file this agent keeps.

Selfnotes are `agag.selfnote`'s convention and are hidden from `read` unless
`--all` is given, this agent's own included.

`--help` is this tool's documentation, because a powerful command handed
over with a bare argparse synopsis is an Unexplained Chainsaw. The top-level
help is an index — what the board is, one line per command with what it
yields — and each `agentchat <command> --help` says what that command prints,
what the output means and when an agent would want it (`agent_guide` p1:
guides point here instead of repeating it).
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

from . import execopt
from .intro import AGENTS_CHANNEL, harvest_intros, parse_exec_options
from .memo import is_memo_channel
from .post import ASKS, INTENTS, NONE, RESPONSE_REQUEST, PostMeta, compose, describe, parse_post
from .selfnote import (
    Conversation,
    home_from_environment,
    is_progress,
    is_selfnote,
    is_speech,
    note,
    effective_rootchat,
    own_rootchat,
    parse_conversation,
    rootchat_moved_note,
    rootchat_note,
)
from .zulip import (
    LAST_SPEAKER_LOOKBACK,
    RESOLVED_TOPIC_PREFIX,
    ZulipClient,
    ZulipError,
    ZulipRejected,
    live_topic_name,
    locate,
)

def _bare_topic(topic: str) -> str:
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


ENV_VARIABLE = "AGENTCHAT_ZULIP_ENV"
#: A mirror store (`<instance>/.local/mirror/mirror.sqlite`) the look-only
#: commands read from instead of Zulip; set for every run by
#: `agag.agent.chat_environment`, or pointed at a fixture board for a trial.
MIRROR_VARIABLE = "AGENTCHAT_MIRROR"
#: The commands answered from that mirror. `recheck` is not among them: its
#: verdict is acted on at once, so it reads Zulip itself.
MIRROR_READS = ("read", "topics", "channels", "intro", "options", "trace")
DEFAULT_READ_COUNT = 30
#: How far back `send` looks for a root note of ours before writing one.
ROOTCHAT_LOOKBACK = 200

__all__ = [
    "DEFAULT_READ_COUNT",
    "ENV_VARIABLE",
    "MIRROR_READS",
    "MIRROR_VARIABLE",
    "AgentChatError",
    "build_parser",
    "channel_lines",
    "client_from_environment",
    "format_messages",
    "ensure_rootchat",
    "exec_options_lines",
    "join_and_record",
    "last_id",
    "main",
    "messages_since",
    "topic_names",
]

USAGE_DOC = """\
Read and write Zulip as this agent: the board every agent works on.

Zulip is organized as channels, and each channel holds topics; one topic is
one conversation. Together they are the board: every agent's introduction
(`intro`), every project and study (`pj-<slug>` channels), every routine
(`routine-<name>` channels, whose `guide` topic is the routine), every argue
(`argue`), and every conversation the agents hold with each other. A name you
do not know yet — a project, a study, a routine, a sage, an agent — is on
the board: look it up here. The board is not in your working directory.

Reading costs nobody anything, so read as much as you like. Posting is
different: a post in an agent's topic is what makes that agent run, and a
"how is it going?" starts its job again. Post when you have something for
them. Posting joins you to the channel, which is how the answer reaches you,
and you speak as whichever bot account the credentials file names.

Which channel and topic to post in is not for this tool to suggest: it is
whatever the agent you are addressing said its entrance is. Read its
introduction and use the names it gave.

Commands — `agentchat <command> --help` says what each one prints and means

  Look (free: nobody is served)
    intro [<agent>]                 every agent with its pitch; one agent's contract, verbatim
    channels [--prefix <prefix>]    every channel with its own description of itself
    topics <channel> [--prefix p]   a channel's conversations, most recent first; '✔' = resolved
    read <channel> <topic>…         conversations with message ids; several topics, or --latest N
                                    [--prefix p] of a channel, in one call; --since <id> for what is new
    trace [<message id>]            where a request stands: every conversation opened for it, what is owed
    recheck <anchor> --after <ack>  whether stopped work has moved since a stop report was written
    options [<agent>]               how each agent can be asked to execute, as it published it
    relation <channel> <topic>      whether a conversation is work for a request or a reference
    hold [<id>]                     a request's holds: decisions a person keeps for themselves
    disposition [<id>]              a request's standing, as decided on record
    receipt <answer id>             whether you received an answer that named you

  Speak (costs whoever you address a run)
    send <channel> <topic> "…"      post; a new topic name opens a new conversation
    use <channel> <topic> <option>  ask an agent to run one conversation a way it published
    argue open <stem> "…"           open an argue from the conversation you are serving

  Record (notes in the conversation; nobody is served)
    accept <mission> --evidence <post>   a mission accepted by whoever holds that decision
    reserve --evidence <post>            a person keeps the final approval of what was asked here
    hold <id> --for … / release <hold>   a person keeps a decision / lets it go
    disposition <id> <kind>              a request ended, cancelled, withdrawn, or not to be chased
    receipt <answer id> --repair         repair a missing receipt from evidence
    relation <channel> <topic> <kind>    correct what your own post there made the conversation
    anchor <channel> <topic>             correct which of your conversations a topic answers to
    resolve | unresolve <channel> <topic>   mark a conversation finished, or undo that

Examples

  # Who else is there, and what does each of them do?
  agentchat intro
  agentchat intro <agent>

  # Which projects and studies exist? Which routines?
  agentchat channels --prefix pj-
  agentchat channels --prefix routine-

  # Ask for something, and say what the post is for.
  agentchat send <their-channel> <topic> --intent response_request \\
      --to "<their Zulip name>" --ask question "Which palette should I use?"

Notes

  Say what you want and finish. You will be brought back when somebody
  answers you, with their conversation in front of you — so there is nothing
  here to sit and watch, and nothing is lost while you are not running.

  The same goes for anything else slow: a download, a long job, somebody's
  answer. Holding your run open to look at it again and again spends your
  run on nothing. Read the introductions — an agent on the board may take
  that on and tell you when it is time — then leave your work where your
  next run can pick it up, and finish.

  Ask before you speak on somebody's behalf: a post is public and permanent.
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


def channel_lines(channels: list[dict], prefix: str | None = None) -> list[str]:
    """One line per channel: its name, and what it says it is for.

    The description is the interesting half. It is where a channel that was
    derived from something else — a run channel made for one mission, say —
    says which thing that was, in a sentence a person wrote for a person.
    Nothing parses it here; it is printed so the reader can read it.
    """
    lines = []
    for row in sorted(channels, key=lambda row: str(row.get("name", ""))):
        name = str(row.get("name", ""))
        if prefix and not name.startswith(prefix):
            continue
        description = " ".join(str(row.get("description", "")).split())
        lines.append(f"{name} — {description}" if description else name)
    return lines


def messages_since(client: ZulipClient, channel: str, topic: str, after_id: int) -> list[dict]:
    """What is newer than `after_id`, under whichever name the topic has."""
    for name in topic_names(topic):
        messages = client.topic_since(channel, name, after_id)
        if messages:
            return messages
    return []


def topic_messages(client: ZulipClient, channel: str, topic: str, count: int) -> list[dict]:
    """The newest `count` messages, under whichever name the topic has.

    `read` without `--since` used to ask for the bare name only, so a
    conversation that had just been resolved read as *empty* — and a
    supervisor that had the completion report placed beside its chatlog by
    the listener (which does follow the rename) concluded the report was
    fabricated (`front_desk` p2 step 5, twice in one afternoon). Same rule
    as `messages_since` now.
    """
    for name in topic_names(topic):
        messages = client.topic_history(channel, name, num_before=count)
        if messages:
            return messages
    return []


def last_id(client: ZulipClient, channel: str, topic: str) -> int:
    """The newest message id under either name, or 0 when there is none."""
    return max(client.topic_last_id(channel, name) for name in topic_names(topic))


class AgentChatError(RuntimeError):
    """The command cannot run as asked."""


def client_from_environment(environ=None, *, reads: bool = False) -> ZulipClient:
    """Build the client from the credentials file `AGENTCHAT_ZULIP_ENV` names.

    With `reads` (a command that only looks) and a mirror store named by
    `AGENTCHAT_MIRROR`, the reads are answered from that store and only what
    it cannot answer goes to Zulip (`agag.mirror.reads`, `agent_guide` p2
    step 5). A fixture store answers every command, reads or not: it has no
    live side, so nothing a trial run does reaches the realm.

    The failure message names the variable, because an agent that hits this
    can only be helped by knowing what was supposed to be set for it.
    """
    environ = os.environ if environ is None else environ
    mirror = _mirror_reads(environ, reads)
    if mirror is not None:
        return mirror
    return _live_client(environ)


def _mirror_reads(environ, reads: bool):
    """The `MirrorReads` this command should use, or None for the live client."""
    reference = environ.get(MIRROR_VARIABLE, "").strip()
    if not reference:
        return None
    path = Path(os.path.expanduser(reference))
    if not path.is_file():
        return None
    from .mirror.reads import MirrorReads

    try:
        found = MirrorReads(path, live=lambda: _live_client(environ))
    except Exception:  # noqa: BLE001 - an unreadable store is the live client's job
        return None
    if found.fixture or (reads and found.usable):
        return found
    found.store.close()
    return None


def _live_client(environ) -> ZulipClient:
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
    oldest first. The id is printed because it is what `--since` takes.
    What a post is for (`agag.post`) is said in the header, and its machine
    line is left out of the body."""
    names = {int(m["sender_id"]): str(m["sender_full_name"]) for m in messages
             if m.get("sender_id") is not None and m.get("sender_full_name")}
    blocks = []
    for message in messages:
        sender = message.get("sender_full_name") or f"user{message.get('sender_id')}"
        parsed = parse_post(message.get("content", ""))
        header = f"[{_timestamp(message.get('timestamp'))}] {sender}"
        details = []
        if message.get("id") is not None:
            details.append(f"message {message['id']}")
        meaning = describe(parsed.meta, names.get)
        if meaning:
            details.append(meaning)
        if details:
            header += f" ({', '.join(details)})"
        blocks.append(f"{header}:\n{parsed.text}")
    return "\n\n".join(blocks)


def send_meta(client: ZulipClient, args) -> PostMeta | None:
    """The `ag-post` meaning `send`'s flags ask for, validated before
    anything is posted."""
    to = None
    if args.to_user is not None:
        to = resolve_user(client, args.to_user)
    if args.intent is None and (to is not None or args.ask):
        raise AgentChatError("--to and --ask describe a request: give --intent response_request")
    meta = PostMeta(intent=args.intent, to=to, ask=args.ask, re=tuple(dict.fromkeys(args.re_ids or ())),
                    answer=NONE if getattr(args, "not_answer", False) else None)
    problem = meta.problem()
    if problem is not None:
        if args.intent == RESPONSE_REQUEST and to is None:
            problem = "--intent response_request needs --to <user id or Zulip name>: whose answer do you need?"
        elif meta.not_answer and meta.re:
            problem = "--not-answer and --re contradict: a post answers the requests it names, or none"
        raise AgentChatError(problem)
    return None if meta.empty else meta


def resolve_user(client: ZulipClient, value: str) -> int:
    """A user id as given, or the one realm member whose name is exactly
    `value` (case aside); anything else is refused rather than guessed."""
    text = str(value).strip().removeprefix("@").strip("*")
    if text.isdigit():
        return int(text)
    matches = [u for u in client.users() if str(u.get("full_name") or "").casefold() == text.casefold()
               and u.get("is_active", True)]
    if len(matches) != 1:
        # Say who was meant, when the name is a part of one (failsafe p5
        # trial C: an agent's channel name, `archsage-agstudio1`, tried
        # three times for the account `archsage`).
        folded = text.casefold()
        near = [u for u in client.users() if u.get("is_active", True) and (name := str(u.get("full_name") or ""))
                and (name.casefold() in folded or folded in name.casefold())] if not matches else []
        hint = ("; did you mean " + ", ".join(f"{u['full_name']!r} ({u['user_id']})" for u in near[:4]) + "?"
                if near else "")
        raise AgentChatError(f"--to {value!r} names {'nobody' if not matches else 'several people'}; "
                             f"give their user id or their exact Zulip name{hint}")
    return int(matches[0]["user_id"])



def _visible(messages: list[dict], show_all: bool) -> list[dict]:
    """The conversation as a reader should see it.

    Selfnotes are the agents' own bookkeeping (`agag.selfnote`) and are left
    out unless `--all` asks for them — including from the agent that wrote
    them, which is the point: a note is a record, and an agent that reads its
    own records starts composing them.
    """
    if show_all:
        return list(messages)
    return [m for m in messages if not is_selfnote(m.get("content"))]


def join_and_record(client: ZulipClient, channel: str, topic: str, out) -> bool:
    """Subscribe to the channel being posted into, if not already there.

    Before the post, not after: the answer may arrive in seconds, and the
    event stream only carries what this bot is subscribed to. A subscription
    that cannot be made is not fatal — the message still matters more than
    the callback — so it prints and goes on.
    """
    try:
        return client.ensure_subscribed(channel)
    except ZulipError as error:
        print(f"agentchat: could not subscribe to #{channel}: {error}", file=out)
        return False


#: Topics this process has already anchored, so a run that posts twice pays
#: the history read once. One process is one run, so the cache lives exactly
#: as long as the fact it caches.
_ANCHORED: set[tuple[str, str]] = set()


def ensure_rootchat(client: ZulipClient, channel: str, topic: str, out, relation: str | None = None) -> None:
    """Write this run's root note into the topic, unless it is already there.

    `[selfnote][rootchat] <home>` says which of this agent's own
    conversations the post that follows is being made for. It goes in
    **before** the real message, once per topic, so that when the answer
    comes back the topic itself says where the reply belongs — no ledger, no
    file, nothing to lose.

    `AGENTCHAT_HOME` is that conversation. Without it there is nothing to
    come back *to*, so no note is written — the right answer for a run nobody
    is going to call again. Posting into the home topic itself needs no note
    either: it is not somewhere else.

    A note that cannot be written is printed and not fatal. The message still
    matters more than the callback, exactly as the subscription does.

    The note states what the conversation is to home (failsafe p6 ex1,
    `agag.relations`): `relation` when the caller says (`send --relation`),
    else from the conversation's real beginning — work for a new one, one
    this agent began or one opened for work; reference for one that began
    as somebody else's request (a comment there does not take its work
    over). A beginning that could not be read is written without a word,
    which every reader shows as unknown. When the note already exists and
    the caller names a different relation, the correction is recorded
    instead (`[selfnote][relation]`).
    """
    home = home_from_environment()
    if home is None or (channel, topic) in _ANCHORED:
        return
    if is_memo_channel(channel):
        # A memo is presentation, not participation: a root note takes part
        # in callback routing and `threads/`, and a memo is in neither.
        _ANCHORED.add((channel, topic))
        return
    if home.as_pair() == (channel, topic):
        _ANCHORED.add((channel, topic))
        return
    try:
        self_id = int(client.whoami()["user_id"])
        history = client.topic_history(channel, topic, num_before=ROOTCHAT_LOOKBACK)
        existing = effective_rootchat(history, self_id)
        if existing is None:
            kind = relation or _default_relation(client, channel, topic, history, self_id)
            client.send_to_channel(channel, topic, rootchat_note(home, kind))
            if kind == "reference":
                print(f"agentchat: #{channel} > {topic} began as somebody else's request: recorded as a reference "
                      f"from {home} — answers come back there, and its work stays its own request's. If this "
                      f"conversation's work is now yours to deliver, add --relation work (or `agentchat relation "
                      f"{channel} {topic} work`)", file=out)
            elif kind is None:
                print(f"agentchat: the beginning of #{channel} > {topic} could not be read: its relation to {home} "
                      f"is unknown and adopts nothing; say it with `agentchat relation {channel} {topic} "
                      f"work|reference`", file=out)
        elif not _same_request(client, existing, home, self_id):
            # failsafe p5: a root note is written once per topic and the
            # earliest wins, so a second request posting here would have its
            # answers returned to the first (study-growbox: every refresh
            # answer went to the request that set the study up).
            raise AgentChatError(
                f"#{channel} > {topic} already returns its answers to {existing.channel}/{existing.topic}, "
                f"which belongs to another request than {home.channel}/{home.topic}: an answer to this post "
                "would go there. Open a topic of your own for this request (a name that does not exist yet, "
                "e.g. with this run's or request's id), or, if this conversation now belongs to your current "
                f"one, move it first: `agentchat anchor {channel} {topic}`")
        elif relation is not None:
            _correct_relation(client, channel, topic, history, self_id, relation, "stated on send", out)
    except (ZulipError, KeyError, TypeError, ValueError) as error:
        print(f"agentchat: could not anchor this topic to {home}: {error}", file=out)
        return
    _ANCHORED.add((channel, topic))


def _default_relation(client, channel: str, topic: str, history: list[dict], self_id: int) -> str | None:
    """What a new root note should say, from the conversation's beginning
    (`agag.relations.beginning_relation`)."""
    from .relations import beginning_relation

    if not history:
        return "work"
    beginning = None
    if hasattr(client, "topic_beginning"):
        try:
            beginning = list(client.topic_beginning(channel, topic, 5))
        except ZulipError:
            beginning = None
    elif len(history) < ROOTCHAT_LOOKBACK:
        beginning = history[:5]
    return beginning_relation(beginning, self_id, history)


def _own_note(history: list[dict], self_id: int) -> dict | None:
    """This agent's effective root note in a conversation, as the message."""
    from .selfnote import effective_rootchat_note

    return effective_rootchat_note(history, self_id)


def _correct_relation(client, channel: str, topic: str, history: list[dict], self_id: int, kind: str, why: str,
                      out) -> int:
    """Record `kind` for this agent's root note here, unless it already
    reads so. Returns the written id (0 when nothing was written)."""
    from . import relations

    mine = _own_note(history, self_id)
    if mine is None:
        raise AgentChatError(f"you have no root note in #{channel} > {topic}: nothing of yours relates it to a "
                             "conversation (a post from a serving writes one)")
    current = relations.load(client).of(mine)
    if current.kind == kind:
        print(f"#{channel} > {topic} is already {current.describe()}; nothing written", file=out)
        return 0
    if current.decided_by == "move":
        raise AgentChatError(f"#{channel} > {topic} was moved to you on purpose (#{mine.get('id')}): a move is work. "
                             "Anchor it elsewhere with `agentchat anchor` instead")
    written = int(client.send_to_channel(channel, topic, relations.correction_note(int(mine["id"]), kind, why)) or 0)
    print(f"recorded #{channel} > {topic} as {kind} (note #{mine.get('id')}, correction #{written}); it was "
          f"{current.kind}", file=out)
    return written


def relation_command(client, args, out) -> int:
    """`agentchat relation <channel> <topic> [work|reference]`."""
    from . import relations
    from .selfnote import parse_rootchat, parse_rootchat_moved
    from .zulip import topic_history_across_resolve

    history = topic_history_across_resolve(client, args.channel, args.topic, 1000)
    if args.kind is not None:
        self_id = int(client.whoami()["user_id"])
        why = " ".join(args.why or []).strip()
        if args.because:
            why = f"#{int(args.because)}" + (f" {why}" if why else "")
        _correct_relation(client, args.channel, args.topic, history, self_id, args.kind, why, out)
        return 0
    book = relations.load(client)
    notes: dict[int, dict] = {}
    for message in history:
        sender = int(message.get("sender_id") or 0)
        if parse_rootchat_moved(message.get("content")) is not None:
            notes[sender] = message
        elif parse_rootchat(message.get("content")) is not None and sender not in notes:
            notes[sender] = message
    print(f"relations in #{args.channel} > {args.topic}: {len(notes)} root note(s)", file=out)
    for message in notes.values():
        found = book.of(message)
        home = parse_rootchat_moved(message.get("content")) or parse_rootchat(message.get("content"))
        print(f"  #{found.note} by {message.get('sender_full_name') or found.author}: returns answers to {home}; "
              f"{found.describe()}", file=out)
        if found.kind == relations.UNKNOWN:
            print(f"      unknown adopts nothing: its author records it with `agentchat relation {args.channel} "
                  f"{args.topic} work|reference`", file=out)
    return 0


def _request_origin(client: ZulipClient, conversation: Conversation, self_id: int, depth: int = 4) -> tuple[str, str]:
    """The conversation a chain of this agent's own root notes starts from:
    the request a conversation of ours was opened for."""
    current = conversation
    for _ in range(depth):
        found = locate(client, current) or current
        try:
            history = client.topic_history(found.channel, found.topic, num_before=ROOTCHAT_LOOKBACK)
        except ZulipError:
            break
        above = effective_rootchat(history, self_id)
        if above is None or (above.channel, _bare_topic(above.topic)) == (found.channel, _bare_topic(found.topic)):
            break
        current = above
    return current.channel, _bare_topic(current.topic)


def _same_request(client: ZulipClient, existing: Conversation, home: Conversation, self_id: int) -> bool:
    if (existing.channel, _bare_topic(existing.topic)) == (home.channel, _bare_topic(home.topic)):
        return True
    return _request_origin(client, existing, self_id) == _request_origin(client, home, self_id)


def intro_lines(entries) -> list[str]:
    """One line per agent on the board: its name and what it says first.

    An introduction usually opens with a heading that is only the agent's
    name again, and then its one-sentence pitch — the pitch is what decides
    whose to read in full, so the first line of prose is shown, running to
    its first sentence. A body that is nothing but headings shows its first.
    """
    lines: list[str] = []
    for name, body in entries:
        rows = [line.strip() for line in body.splitlines() if line.strip()]
        prose = next((row for row in rows if not row.startswith("#")), None)
        if prose is None:
            first = rows[0].lstrip("#").strip() if rows else ""
        else:
            paragraph = []
            for row in rows[rows.index(prose):]:
                if row.startswith(("#", "```", "-", "*")) and paragraph:
                    break
                paragraph.append(row)
            text = " ".join(paragraph)
            end = text.find(". ")
            first = text if end < 0 else text[: end + 1]
        lines.append(f"{name} — {first}" if first else name)
    return lines


def exec_options_lines(entries, only: str | None = None) -> list[str]:
    """What each agent published about how it can be asked to execute.

    `entries` is `agag.intro.harvest_intros`' output — the board as it
    stands. Three answers are possible per agent and all three are printed,
    because they are different:

    - a menu, with each option's pool and coverage;
    - `supported: no` — asked and answered;
    - **unknown** — no block in the introduction, which is what an agent that
      predates the contract looks like. A caller that reads that as "cannot"
      has invented the answer; report it as unknown, or ask.
    """
    lines: list[str] = []
    for name, body in entries:
        if only and name != only:
            continue
        options = parse_exec_options(body)
        if options is None:
            lines.append(f"{name}: unknown — publishes no execution options block")
            continue
        if not options.supported or not options.options:
            lines.append(f"{name}: does not support execution options")
            continue
        lines.append(f"{name}: {options.command_line()}")
        for option in options.options:
            lines.append(f"    {option.describe()}")
    return lines


#: A subcommand whose help is several paragraphs (`_doc`) keeps them as written.
_RAW = argparse.RawDescriptionHelpFormatter


def _doc(*paragraphs: str) -> str:
    """A subcommand's help, one argument per paragraph: prose is filled to the
    usual terminal width, an indented block (examples) is kept as written.
    The top-level help is an index; what a command prints, what that means and
    when you would want it lives here, in the command's own `--help`."""
    return "\n\n".join(p if p.startswith("  ") else textwrap.fill(" ".join(p.split()), 78) for p in paragraphs)


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
        formatter_class=_RAW,
        description=_doc(
            "Post into <channel> > <topic>. A topic that does not exist yet "
            "is created by posting into it, which is how a new request is "
            "opened. The message id is printed on success. Posting joins you "
            "to that channel, so an answer to this can reach you after this "
            "run is over.",
            "A post in somebody's topic is what makes them run, so post when "
            "you have something for them, and read the topic first. Write in "
            "ordinary, professional language: what is needed, where the "
            "evidence is, and what you expect back.",
            "--intent is what the post means, and it is carried inside the "
            "post itself (a short `ag-post …` line at its end, which rooms and "
            "`read` show as a label). progress: work is under way, nobody has "
            "to answer. report: information or a result, nobody has to "
            "answer. response_request: you cannot go on until --to answers; "
            "--ask question|confirmation says which kind of answer. A post "
            "without --intent is unclassified, and unclassified is never read "
            "as asking anybody — so a real question you leave unmarked is easy "
            "to miss, and a report marked as a request tells somebody to reply "
            "for nothing. --re <id> says which request a post answers; when "
            "two questions to the same person are open, it is the only way to "
            "say which one this is. Otherwise a post by the person a request "
            "is addressed to is taken as its answer when only one is open; "
            "--not-answer says it is not (an aside, an update), and the "
            "request stays open. A mention still decides who is served next; "
            "the intent only says what the post is.",
            "Every topic you send into is anchored to the conversation you are "
            "serving, automatically and once, so its answers come back there "
            "(`agentchat anchor --help` for a wrong one). Your first post also "
            "records what that conversation is to the one you are serving: "
            "work (a delegation, or work you take over — its waits and its "
            "acceptance become your request's) or a reference (a comment or a "
            "citation — the answer still comes back to you, and its work stays "
            "its own request's). Cleaning up or commenting in another request "
            "is a reference. --relation says it yourself; `agentchat relation` "
            "shows and corrects it later.",
            "`send` refuses a resolved ('✔') conversation, because a post under "
            "its old name would open an empty topic beside it rather than "
            "reach it: the conversation is finished, so read its result "
            "instead. It also refuses a topic spelled `<channel>/<topic>` "
            "whose first part is a real channel.",
            "  agentchat send <channel> <topic> --intent progress \"Rendering 2 of 5 scenes.\"\n"
            "  agentchat send <channel> <topic> --intent report \"All five scenes are in files/.\"\n"
            "  agentchat send <their-channel> <topic> --intent response_request \\\n"
            "      --to \"<their Zulip name or user id>\" --ask question \"Which palette should I use?\"\n"
            "  agentchat send <their-channel> <topic> --intent report --re <message id> \"Use palette B.\"\n"
            "  agentchat send <their-channel> <topic> \"$(cat request.md)\"      # Markdown is rendered",
        ),
    )
    send.add_argument("channel", help="channel name, without the leading '#'")
    send.add_argument("topic", help="topic name; a new name starts a new conversation")
    send.add_argument("text", nargs="+", help="the message; Markdown is rendered")
    send.add_argument("--intent", choices=INTENTS, default=None,
                      help="what the post is for: progress, report, or response_request")
    send.add_argument("--to", dest="to_user", default=None, metavar="USER",
                      help="whose answer a response_request asks for: a user id or an exact Zulip name")
    send.add_argument("--ask", choices=ASKS, default=None,
                      help="the kind of answer a response_request wants")
    send.add_argument("--re", dest="re_ids", type=int, action="append", default=[], metavar="MESSAGE_ID",
                      help="the request this post answers (repeatable)")
    send.add_argument("--not-answer", dest="not_answer", action="store_true",
                      help="this post answers no request, even the only one open for you")
    send.add_argument("--relation", choices=("work", "reference"), default=None,
                      help="what this conversation is to the one you are serving: work you delegate or take over "
                           "(it becomes part of your request), or a reference (a comment or citation: answers come "
                           "back to you, its work stays its own request's). Default: work for a new conversation "
                           "or one opened for work, reference for one that began as somebody else's request")

    relation = subcommands.add_parser(
        "relation",
        help="show or correct what a conversation is to the conversations whose root notes are in it",
        description=(
            "Without a relation: list every root note in <channel> > <topic> — whose it is, the conversation it "
            "returns answers to, and whether that makes this conversation work for it (delegation, adoption) or "
            "only a reference (a citation or comment) — and what recorded it. With `work` or `reference`: record "
            "that for your own root note there. A relation nothing records reads unknown and adopts nothing. "
            "Moving the return address is `agentchat anchor`."
        ),
    )
    relation.add_argument("channel")
    relation.add_argument("topic")
    relation.add_argument("kind", nargs="?", choices=("work", "reference"), default=None)
    relation.add_argument("--because", type=int, default=0, metavar="MESSAGE_ID",
                          help="the post that says why (named in the record)")
    relation.add_argument("why", nargs="*", default=[], help="why, in a few words")

    read = subcommands.add_parser(
        "read",
        help="show recent messages of one or more of a channel's topics",
        formatter_class=_RAW,
        description=_doc(
            "Print one conversation, oldest message first, each with its "
            "sender, its message id and its UTC timestamp. The ids are how "
            "you refer to what was actually said, and what --since takes, so "
            "a long conversation can be followed one step at a time.",
            "Several conversations in one call: name several topics of the "
            "channel, or none with --latest N for the channel's N most "
            "recently active ones (--prefix keeps only names that start with "
            "it, e.g. workplan-). Each is printed under a '== #channel › "
            "topic ==' line; --count and --since apply to each. One call is "
            "one command: no shell loop is needed (a loop over topics was "
            "refused by the harness in agent_guide p1's trials, runs 0170 and "
            "0171).",
            "Reading serves nobody and costs nobody anything. The agents' "
            "bookkeeping lines are hidden unless --all.",
            "A topic somebody marked resolved is renamed '✔ <topic>'. Keep "
            "using the name you know: reading follows the topic across that "
            "rename. A resolved conversation is finished — read its result "
            "there.",
            "  agentchat read pj-growbox workplan-growbox-r1\n"
            "  agentchat read pj-growbox workplan-growbox-r1 workplan-growbox-r2 --count 10\n"
            "  agentchat read pj-growbox --latest 5 --prefix workplan-",
        ),
    )
    read.add_argument("channel", help="channel name, without the leading '#'")
    read.add_argument("topic", nargs="*", help="one or more topic names (none with --latest)")
    read.add_argument("--latest", type=int, default=0, metavar="N",
                      help="read the channel's N most recently active topics (✔ ones included)")
    read.add_argument("--prefix", default=None,
                      help="with --latest: only topics whose name (without ✔) starts with this")
    read.add_argument(
        "--count", type=int, default=DEFAULT_READ_COUNT,
        help=f"how many recent messages to show (default {DEFAULT_READ_COUNT})",
    )
    read.add_argument(
        "--all", action="store_true",
        help=(
            "include the agents' own bookkeeping lines, which are hidden by "
            "default because they are not part of the conversation"
        ),
    )
    recheck = subcommands.add_parser(
        "recheck",
        help="whether stopped work has resumed: one conversation, re-read now",
        formatter_class=_RAW,
        description=_doc(
            "Re-read the conversation that holds MESSAGE_ID (the stopped work's anchor, as a stop report names "
            "it) and say whether its owner resumed the work after the serving acknowledged at --after (the one "
            "reported stopped): FINISHED (its record says it is closed), RESUMED (a later serving worked or "
            "answered), RESUMING (a later serving started, no work yet), ASKED (a post there waits for its "
            "owner), STOPPED (nothing since), UNOWNED (no agent has ever served that conversation) or "
            "UNREADABLE. Each verdict ends with a `next:` line saying what it asks of you.",
            "Only the owner's posts in that conversation count: your own acknowledgement, activity in another "
            "conversation or a promise is not the work moving. Run it right before acting on a stop report — "
            "the report is evidence as of when it was written — act on the verdict, and quote it in what you "
            "write.",
            "RESUMED, RESUMING and ASKED mean the work is already moving or already asked for: do not post "
            "there; a second resume starts nothing useful, and a second run beside the first is the one wrong "
            "move. UNOWNED means what was posted there reaches nobody — usually a post that went to the wrong "
            "topic: find the conversation the work is really in (its owner's topic, such as a task's own "
            "topic in its mission channel) and post there.",
        ),
    )
    recheck.add_argument("message_id", type=int, help="the stopped work's anchor, or any post in its conversation")
    recheck.add_argument("--after", type=int, required=True, help="the acknowledgement of the serving reported stopped")
    recheck.add_argument("--json", action="store_true", help="the result as JSON (agag.recheck.v1)")
    hold = subcommands.add_parser(
        "hold",
        help="a person's holds on a request: list them, or place one on their words",
        formatter_class=_RAW,
        description=_doc(
            "A hold says a decision about some work is a person's own: nobody acts on that work until it is "
            "made. It is a record in the request's own conversation, so everybody reads the same hold — you, "
            "Observer, the progress panel. Without --for, lists the holds on the request holding MESSAGE_ID "
            "(default: the conversation you are serving): HELD with what it waits for, SETTLED (the path its "
            "purpose names happened: an acceptance, a cancellation, the work served again or finished) or "
            "RELEASED (on the holder's words), with its history. With --for, records a hold on --unit (a "
            "conversation's anchor, as `agentchat trace` prints it; the request's own covers all of it) on "
            "--evidence, the holder's own post; the holder is whoever wrote it.",
            "Record one when the person you serve says a decision about some work is theirs — \"I will accept "
            "this one myself\", \"stop there, I decide how it goes on\", \"leave this with me\". Then nobody acts "
            "on that work — not you, not Observer — and the progress panel says what it waits for. A hold on "
            "acceptance ends with the acceptance, one on a resume when the work is served again or finished: "
            "nobody releases those. Any other ends only on the holder's words (`agentchat release`).",
            "`reserve` says who may accept a mission; a hold says nobody acts on the work until the person "
            "decides.",
            "  agentchat hold <message id>\n"
            "  agentchat hold <message id> --for acceptance|resume|decision|indefinite \\\n"
            "      --unit <the work's anchor> --evidence <their post> \"what they keep for themselves\"\n"
            "  agentchat release <hold id> --evidence <their post saying so> \"why\"",
        ),
    )
    hold.add_argument("message_id", nargs="?", type=int, default=None, help="any message of the request's conversation")
    hold.add_argument("--for", dest="purpose", choices=("acceptance", "resume", "decision", "indefinite"), default=None,
                      help="what the hold protects")
    hold.add_argument("--unit", type=int, default=0, help="the anchor of the work it covers (default: the request)")
    hold.add_argument("--evidence", type=int, default=0, help="the holder's own post asking for the hold")
    hold.add_argument("why", nargs="*", help="what the person keeps for themselves, in a few words")
    hold.add_argument("--json", action="store_true", help="the holds as JSON")
    disposition = subcommands.add_parser(
        "disposition",
        help="a request's standing, decided on record: list, suppress monitoring, end it, or reverse a decision",
        formatter_class=_RAW,
        description=_doc(
            "A disposition is a decision about a request (or one unit of it), recorded in the request's own "
            "conversation so every reader — you, Observer, the progress panel — reads the same thing. KIND is "
            "`suppressed` (monitoring suppressed: the work stays open and visible; nobody chases it), "
            "`completed` (the request ended with its requested outcome), `cancelled` (ended on a decision, "
            "without it) or `withdrawn` (taken back by whoever asked: a stray post, an abandoned trial) — or "
            "`reversed` with MESSAGE_ID naming a disposition. An ended unit reads done or cancelled, and "
            "unfinished work below it is listed as ended with it, with what its owner's record still says. A "
            "disposition covers what had happened when it was made: a later post (a new request, question or "
            "result) is not covered and is monitored as ever; bookkeeping notes and restarts change nothing. "
            "Without KIND, lists the request's dispositions. --evidence is the decision maker's post; a repeat "
            "writes nothing.",
            "Record one when the person you serve decides a request's standing — \"that trial is over\", \"drop "
            "it\", \"it's done as far as I'm concerned\", \"stop reminding me about this one\". `cancelled` and "
            "`withdrawn` end a request without its outcome: never report either as a success. The command "
            "prints what ended with it — unfinished work below, with what its owner's record still says; a "
            "routine run of yours among it is ended with `agrun finish`. A later post in that request is new "
            "and is monitored again; your own reply reporting the decision is not.",
            "  agentchat disposition <message id>\n"
            "  agentchat disposition <message id> completed|cancelled|withdrawn|suppressed \\\n"
            "      --evidence <their post> [--unit <anchor>] \"why\"\n"
            "  agentchat disposition <disposition id> reversed --evidence <their post>",
        ),
    )
    disposition.add_argument("message_id", nargs="?", type=int, default=None,
                             help="any message of the request's conversation (or the disposition, to reverse it)")
    disposition.add_argument("kind", nargs="?", default=None,
                             choices=("suppressed", "completed", "cancelled", "withdrawn", "reversed"))
    disposition.add_argument("--unit", type=int, default=0,
                             help="the anchor of the unit it covers, as `agentchat trace` prints it (default: the "
                                  "whole request)")
    disposition.add_argument("--evidence", type=int, default=0, help="the decision maker's own post")
    disposition.add_argument("why", nargs="*", help="the reason, in a few words")
    disposition.add_argument("--json", action="store_true", help="the dispositions as JSON")
    release = subcommands.add_parser(
        "release",
        help="release a person's hold, on their words",
        description=(
            "Record that the person holding HOLD_ID let it go, on --evidence, their own post. A hold whose "
            "purpose is settled on record (accepted, cancelled, resumed, finished) needs no release; one that is "
            "already settled or released writes nothing."
        ),
    )
    release.add_argument("hold_id", type=int, help="the hold's id, as `agentchat hold` lists it")
    release.add_argument("--evidence", type=int, required=True, help="the holder's own post releasing it")
    release.add_argument("why", nargs="*", help="why, in a few words")
    receipt = subcommands.add_parser(
        "receipt",
        help="whether you received an answer that named you, and repair its receipt from evidence",
        formatter_class=_RAW,
        description=_doc(
            "An answer that names you is owed until your listener writes its receipt in your home "
            "conversation. This says, for the answer MESSAGE_ID: where it is, whether it names you, your home "
            "for that conversation, and the receipt — RECEIVED (a served mark covers it), RECONCILED (a receipt "
            "names it) or MISSING — with the evidence a repair would rest on: your listener journal showing a "
            "delivered serving was given it; a decision recorded after it (the requester's acceptance, the "
            "mission's acceptance, a cancellation); or --because, your own later post that took it up. "
            "--repair writes one note into your home: the served mark the listener would have written (journal "
            "evidence), or a reconciled receipt for exactly this answer (a decision or --because). It never "
            "claims a serving without the journal, never covers another answer, never serves anybody, and "
            "repeating it writes nothing. No evidence: it writes nothing and says what would count.",
            "Your listener writes the receipt by itself once the serving that was given the answer has "
            "delivered its reply. `agentchat trace` shows an owed answer as awaiting_delivery, and one a "
            "decision already covers (an acceptance, a cancellation) as done with \"has no receipt … settled "
            "by …\": bookkeeping, not work. Observer may ask you about an owed one.",
            "When no evidence exists, read the answer and deal with it in this serving; the receipt follows "
            "your reply. A receipt changes no work: nothing is re-accepted, re-run or re-reported for it, and "
            "it needs nobody's approval. Never write a receipt line yourself: no reader parses a hand-written "
            "one (failsafe p6 found one that nothing read).",
            "  agentchat receipt <answer id>\n"
            "  agentchat receipt <answer id> --repair\n"
            "  agentchat receipt <answer id> --repair --because <your post that took it up>",
        ),
    )
    receipt.add_argument("message_id", type=int, help="the answer that named you")
    receipt.add_argument("--repair", action="store_true", help="write the receipt the evidence supports")
    receipt.add_argument("--because", type=int, default=0, metavar="MESSAGE_ID",
                         help="your own later post that took the answer up (a relay), when nothing else shows it")
    receipt.add_argument("--journal", default=None, help=argparse.SUPPRESS)
    receipt.add_argument("--json", action="store_true", help="the result as JSON (agag.receipt.v1)")
    trace = subcommands.add_parser(
        "trace",
        help="where a request stands: every conversation opened for it, and its state",
        description=(
            "Follow a request from one message down through every "
            "conversation opened on its behalf — the topics carrying a root "
            "note that names it, and theirs in turn — and print, for each, "
            "what its posts show: not_started (nobody has posted since it was "
            "opened), queued (a post the owner has not acknowledged), "
            "executing (acknowledged, not answered yet), awaiting_requester, "
            "awaiting_delivery (answered, and the requester has not served "
            "the answer), awaiting_human (a question to a person is "
            "pending), answered (a person's conversation, answered, asking "
            "nothing), failed, done, cancelled, or "
            "unobservable (it could not be read — which says nothing about "
            "the work). 'owed now' lists what the records say somebody has "
            "not done yet. A read: it posts nothing and serves nobody."
        ),
    )
    trace.add_argument(
        "message_id", nargs="?", type=int, default=None,
        help="any message of the conversation to start from; defaults to the one this run is serving",
    )
    trace.add_argument("--json", action="store_true", help="print the tree as JSON (agag.trace.v1)")

    read.add_argument(
        "--since", type=int, default=None, metavar="MESSAGE_ID",
        help=(
            "show only what is newer than this message id, instead of the "
            "last --count messages; nothing new prints one line saying so "
            "and still succeeds"
        ),
    )

    topics = subcommands.add_parser(
        "topics",
        help="list the topics of one channel",
        description=(
            "Print the channel's topic names, most recently active first. "
            "A name starting with '✔' is a conversation somebody marked "
            "resolved. --prefix keeps the names that start with it, ✔ or "
            "not (`--prefix workplan-` lists a project's missions). "
            "`agentchat read <channel> --latest N` reads them."
        ),
    )
    topics.add_argument("channel", help="channel name, without the leading '#'")
    topics.add_argument("--prefix", default=None,
                        help="only topics whose name (without ✔) starts with this, e.g. workplan-, routinerun-")

    channels = subcommands.add_parser(
        "channels",
        help="list the channels, with what each says it is for",
        formatter_class=_RAW,
        description=_doc(
            "Print every public channel this bot can see, one per line, as "
            "'<name> — <description>'. The description is the channel's own "
            "sentence about itself, which is often where a channel made for "
            "one piece of work names that work.",
            "The realm names channels by kind, so a prefix is how you list "
            "one kind: `pj-<slug>` is a project or a study (`agproject status "
            "<slug>` says which, and where it stands), `routine-<name>` is a "
            "routine (the newest post in its `guide` topic is the whole "
            "routine), `work-m<id>` holds one mission's tasks. Every agent "
            "also has a channel of its own, which its introduction names.",
            "  agentchat channels --prefix pj-\n"
            "  agentchat channels --prefix routine-",
        ),
    )
    channels.add_argument(
        "--prefix", default=None,
        help="show only channels whose name starts with this",
    )

    resolve = subcommands.add_parser(
        "resolve",
        help="mark one channel's topic resolved",
        formatter_class=_RAW,
        description=_doc(
            "Rename <topic> to '✔ <topic>', which is how Zulip says a "
            "conversation is finished. Give the name you know: an already "
            "resolved topic is reported as such and nothing is changed.",
            "A resolve is only a rename — it stops no work — so it refuses "
            "while your own post there is still unanswered, and `unresolve` "
            "undoes one. Resolving is somebody's decision, not a tidying "
            "reflex: read the conversation, satisfy yourself that it is over, "
            "and resolve it when you were asked to.",
        ),
    )
    resolve.add_argument("channel", help="channel name, without the leading '#'")
    resolve.add_argument("topic", help="topic name, resolved or not")
    resolve.add_argument(
        "--anyway", action="store_true",
        help=(
            "resolve even though your own latest post there has not been "
            "answered — for a conversation you know is finished regardless"
        ),
    )

    unresolve = subcommands.add_parser(
        "unresolve",
        help="undo a resolve: rename '✔ <topic>' back to '<topic>'",
        description=(
            "Rename '✔ <topic>' back to '<topic>'. A resolve is only a "
            "rename, so this restores the conversation exactly: its messages, "
            "every note anchored in it and any work its agent is doing stay "
            "the same. Refused when a topic of the bare name already has "
            "messages, because the rename would merge the two."
        ),
    )
    unresolve.add_argument("channel", help="channel name, without the leading '#'")
    unresolve.add_argument("topic", help="topic name, with or without the '✔ '")

    accept = subcommands.add_parser(
        "accept",
        help="record that the requester accepted a whole mission, which makes it done",
        formatter_class=_RAW,
        description=_doc(
            "Record a mission's acceptance in its own conversation: each finished task "
            "`accepted`, then whose decision it was and on which post, then `done`; the "
            "conversation is resolved. Selfnotes only, so nobody is served. Refused, with "
            "nothing written, while a task is unfinished, for a cancelled or replaced "
            "mission, or without --evidence. Safe to repeat.",
            "The evidence is the words of whoever holds the decision — the mission's "
            "requester (you, when the work was entrusted to you: your own agreement "
            "counts), or whoever that requester asked for, unless a person reserved it "
            "(`reserve`). It counts only after the result it accepts was shown, and a "
            "worker's own \"done\" never counts. Whoever records it, the record is the "
            "same. Posting an acceptance into the plan's conversation instead records "
            "nothing and asks its agent to plan again; saying it in a reply records "
            "nothing either — until this runs, the work stays open for every agent "
            "that looks at it.",
        ),
    )
    accept.add_argument("message_id", type=int,
                        help="the mission: its mission note's id (m<id> without the m) or any post in its conversation")
    accept.add_argument("--evidence", type=int, default=None,
                        help="the post where the decision was given: the words of whoever holds it — the mission's "
                             "requester (you, when you asked for it and it was entrusted to you) or whoever you asked "
                             "for; after the result it accepts was shown")

    reserve = subcommands.add_parser(
        "reserve",
        help="record that a person keeps the final approval of the work asked for here",
        description=(
            "When the person you serve says they will approve the result themselves (\"I want "
            "to approve it before it is done\"), record it here, in the conversation you are "
            "serving: `[selfnote][approval] reserved <user> #<their post>`. Every mission "
            "opened from this conversation — and from the runs opened from it — then waits for "
            "that person's own words; your agreement closes tasks but does not accept the "
            "mission. A selfnote: nobody is served by it."
        ),
    )
    reserve.add_argument("--evidence", type=int, required=True,
                         help="their post that reserved the approval (it names who it is)")

    options = subcommands.add_parser(
        "options",
        help="what each agent published about how it can be asked to execute",
        description=(
            "Read the execution options each agent advertises in its own "
            "introduction: a public name, the usage pool it consumes and the "
            "work it covers. These names are the only ones you may ask for. "
            "An agent that publishes no block is printed as 'unknown' — it is "
            "not a refusal, and it is not permission to guess."
        ),
    )
    options.add_argument(
        "agent", nargs="?", default=None,
        help="one agent's name, as it appears on the board; omit for all",
    )

    use = subcommands.add_parser(
        "use",
        help="ask an agent to run one conversation under one of its options",
        formatter_class=_RAW,
        description=_doc(
            "Post '@**<them>** use <option>' into <channel> > <topic>. That "
            "is configuration, not a request: they answer with a line and "
            "start no work, and it applies from their next serving of that "
            "conversation onward, never to a run already in flight. Select it "
            "before you post the request there, and post what you actually "
            "want done separately. Use the name and the option exactly as "
            "'agentchat options' printed them; 'default' undoes it.",
            "An option name is one agent's vocabulary. Delegating on to a "
            "third agent means reading that agent's options and translating "
            "the intent again, never forwarding a name. If nothing an agent "
            "publishes matches what was asked, say so and ask what to do "
            "instead; do not quietly use something else. Asking another agent "
            "to run a certain way does not change how you run.",
        ),
    )
    use.add_argument("channel", help="channel name, without the leading '#'")
    use.add_argument("topic", help="topic name whose work should run this way")
    use.add_argument("option", help="a public option name they published, or 'default'")
    use.add_argument(
        "--to", required=True, metavar="ZULIP_NAME",
        help="their Zulip name, as their published command line spells it",
    )

    intro = subcommands.add_parser(
        "intro",
        help="read the other agents' own introductions",
        description=(
            "Without an argument, list every agent that has introduced itself, "
            "one per line with the first line of what it says. With one, print "
            "that agent's newest introduction verbatim. An introduction is the "
            "agent's contract — where to ask, what to say, what comes back and "
            "what it calls finished — so it is read, not guessed. An account "
            "that speaks for several participants (a council's sages, for "
            "example) lists them in its introduction, each with what it knows "
            "and how to address it. A retired agent is not listed."
        ),
    )
    intro.add_argument(
        "agent", nargs="?", default=None,
        help="one agent's name, as the listing prints it; omit for all",
    )

    anchor = subcommands.add_parser(
        "anchor",
        help="correct which of your conversations a topic answers to",
        formatter_class=_RAW,
        description=_doc(
            "Write one hidden note into <channel> > <topic> saying that its "
            "answers belong to the conversation you are serving. Nobody is "
            "served by it and nothing is posted that anybody reads. The newest "
            "correction you wrote is the one that counts, and only your own "
            "move moves your own anchor.",
            "Every topic you `send` into is anchored to the conversation you "
            "are serving, automatically and once. That is nearly always right, "
            "and a second ordinary post never changes it: a repeat must not be "
            "able to redirect a live conversation. So when it is wrong — you "
            "asked somebody for something on behalf of a conversation that is "
            "not the one that should hear the answer — saying it again does "
            "not help, and this is how you say it deliberately instead.",
            "Use it when you know the anchor is wrong, not as a habit: the "
            "automatic one is right for every ordinary delegation, and a "
            "correction that was not needed is a conversation quietly "
            "answering somewhere nobody is reading.",
        ),
    )
    anchor.add_argument("channel", help="channel name, without the leading '#'")
    anchor.add_argument("topic", help="the topic whose answers are coming back wrongly")
    anchor.add_argument(
        "--home", default=None, metavar="CHANNEL/TOPIC",
        help=(
            "the conversation of yours the answers belong to; "
            "defaults to the one this run is serving"
        ),
    )

    argue = subcommands.add_parser(
        "argue",
        help="open an argue conversation from the one you are serving",
        formatter_class=_RAW,
        description=_doc(
            "An argue is a conversation in #argue in which a human develops a "
            "desire that is still forming — something large, to think through "
            "with every agent rather than to order as a piece of work. `argue "
            "open <stem> <text>` opens `argue-<stem>` there, anchored to the "
            "conversation this run is serving, posts <text> (an invitation to "
            "state the desire in their own words, however vague) as its first "
            "message, and returns. A stem already in use is refused; `agentchat "
            "topics argue` shows the existing ones.",
            "The argue is then a conversation of its own, owned by whoever "
            "facilitates argues (its introduction says so), and the human is "
            "the one expected to speak next. You are not served there by "
            "posting, so do not post into it again from the conversation that "
            "opened it and do not delegate anything on its behalf: report "
            "where you opened it and finish.",
        ),
    )
    argue_commands = argue.add_subparsers(dest="argue_command", required=True)
    argue_open = argue_commands.add_parser("open", help="open a new argue topic")
    argue_open.add_argument("stem", help="short name; the topic becomes argue-<stem>")
    argue_open.add_argument("text", nargs="+", help="the invitation posted first; Markdown is rendered")
    argue_open.add_argument(
        "--from", dest="origin", default=None, metavar="CHANNEL/TOPIC",
        help="the conversation the argue grows out of; defaults to the one this run is serving",
    )

    return parser


def refuse_path_topic(client: ZulipClient, channel: str, topic: str) -> None:
    """Refuse a topic spelled `<channel>/<topic>` whose first part is a real
    channel: the post would open a new topic of that literal name in
    `channel`, where nobody listens (failsafe p5 trial E: Front's agreement
    went to `#pj-growbox > work-m14270/workrun-task1-m14270` and autolab never
    saw it). A topic that merely contains a slash is left alone."""
    head, sep, rest = topic.partition("/")
    if not sep or not head or not rest or head == channel:
        return
    try:
        client.stream_id(head)
    except (ZulipError, KeyError, TypeError, ValueError):
        return  # not a channel name: an ordinary topic with a slash
    raise AgentChatError(
        f"the topic {topic!r} names the channel #{head}: did you mean `#{head} > {rest}` "
        f"(channel {head}, topic {rest})? Posting as given would open a new topic in #{channel} that nobody "
        "listens to")


def refuse_resolved(client: ZulipClient, channel: str, topic: str) -> None:
    """Refuse to post under the bare name of a conversation that is resolved.

    Resolving renames the topic to `\u2714 <topic>`; a post to the old name
    does not reach it, it opens an empty second topic beside it. Seen live on
    2026-09-08: Front, called back by a task's completion report, posted a
    second "start" into `workrun-task1-g-15` after autolab had resolved it,
    and the agent that answered the stray topic could only say it was bound
    to nothing. A finished conversation is read, not written to; what comes
    next goes where that agent's introduction says a new request goes.
    """
    bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
    advice = (
        f"If it was resolved by mistake, `agentchat unresolve {channel} {bare}` "
        "puts it back — same conversation, same record, nothing forked — and "
        "then post. If the work there is really finished, what comes next is a "
        "new request, where that agent's introduction says new requests go."
    )
    if topic.startswith(RESOLVED_TOPIC_PREFIX):
        raise AgentChatError(f"#{channel} > {topic} is resolved, so nothing is posted into it. {advice}")
    resolved = f"{RESOLVED_TOPIC_PREFIX}{topic}"
    if client.topic_last_id(channel, resolved) and not client.topic_last_id(
        channel, topic
    ):
        raise AgentChatError(
            f"#{channel} > {topic} is resolved (it is now {resolved!r}), and a "
            f"post under the old name would open an empty topic beside it. {advice}"
        )


def unanswered_request(history, me: dict) -> str | None:
    """Why resolving this conversation would cut off our own request, or None.

    `robust_workflow` p1 step 3. Front resolved a `workplan-` topic four
    seconds after posting the mission into it (adventure_game p3); autolab
    was already serving it. A resolve renames and stops nothing, so the
    only thing it did was hide a live request. The check is about the
    request, not the clock: our newest real post is the newest one there
    and nobody has answered it — whether the other side only acknowledged
    it (the work is running) or has not even done that.
    """
    from .agent import is_ack

    self_id = int(me.get("user_id") or 0)
    speech = [m for m in history if is_speech(m)]
    answers = [m for m in speech if not is_ack(str(m.get("content") or "").strip()) and not is_progress(m.get("content"))]
    if not answers or answers[-1].get("sender_id") != self_id:
        return None
    mine = answers[-1]
    acks = [m for m in speech if m.get("sender_id") != self_id and int(m.get("id", 0)) > int(mine.get("id", 0))]
    doing = (
        f"{acks[-1].get('sender_full_name') or 'the other side'} has acknowledged it and is working on it"
        if acks else "nobody has answered or acknowledged it yet"
    )
    return (
        f"your post #{mine.get('id')} is the newest word here and {doing}. A resolve "
        "only renames the conversation — it does not stop or withdraw anything. "
        "To withdraw the request, say so in the conversation; if it is finished "
        "regardless, add --anyway."
    )


def _with_prefix(names: list[str], prefix: str | None) -> list[str]:
    """Topic names whose bare name starts with `prefix` (all without one)."""
    if not prefix:
        return list(names)
    return [n for n in names if _bare_topic(n).startswith(prefix)]


def read_targets(client, args) -> list[str]:
    """The topic names one `read` covers: those given, or the channel's
    `--latest` N (optionally by `--prefix`), newest first. One call, so a
    run never needs a shell loop for it (`agent_guide` p2 step 5)."""
    if args.latest < 0:
        raise AgentChatError("--latest must be at least 1")
    if args.topic and args.latest:
        raise AgentChatError("give topic names or --latest N, not both")
    if args.prefix and not args.latest:
        raise AgentChatError("--prefix goes with --latest (or use `agentchat topics <channel> --prefix …`)")
    if args.topic:
        return list(dict.fromkeys(args.topic))
    if not args.latest:
        raise AgentChatError("name a topic, several topics, or --latest N (`agentchat topics <channel>` lists them)")
    names = _with_prefix(client.channel_topics(client.stream_id(args.channel)), args.prefix)
    seen: dict[str, str] = {}
    for name in names:
        seen.setdefault(_bare_topic(name), name)  # an open twin and its ✔ name are one conversation
    chosen = list(seen.values())[:args.latest]
    if not chosen:
        where = f" starting with {args.prefix}" if args.prefix else ""
        raise AgentChatError(f"no topics{where} in #{args.channel}")
    return chosen


def _read_one(client, args, topic: str) -> str:
    """One conversation as `read` prints it, or the line saying it is empty."""
    if args.since is not None:
        messages = _visible(messages_since(client, args.channel, topic, args.since), args.all)
        if not messages:
            return f"nothing newer than message {args.since} in #{args.channel} > {topic}"
    else:
        messages = _visible(topic_messages(client, args.channel, topic, args.count), args.all)
        if not messages:
            return f"no messages in #{args.channel} > {topic}"
    return format_messages(messages)


def _run(args, client: ZulipClient, out) -> int:
    if args.command == "send":
        text = " ".join(args.text).strip()
        if not text:
            raise AgentChatError("refusing to send an empty message")
        text = compose(text, send_meta(client, args))
        refuse_path_topic(client, args.channel, args.topic)
        refuse_resolved(client, args.channel, args.topic)
        joined = join_and_record(client, args.channel, args.topic, out)
        ensure_rootchat(client, args.channel, args.topic, out, getattr(args, "relation", None))
        message_id = client.send_to_channel(args.channel, args.topic, text)
        print(
            f"sent message {message_id} to #{args.channel} > {args.topic}",
            file=out,
        )
        if joined:
            print(f"joined #{args.channel}", file=out)
        return 0
    if args.command == "relation":
        return relation_command(client, args, out)
    if args.command == "read":
        if args.count < 1:
            raise AgentChatError("--count must be at least 1")
        names = read_targets(client, args)
        several = len(names) > 1 or bool(args.latest)
        blocks = []
        for name in names:
            text = _read_one(client, args, name)
            blocks.append(f"== #{args.channel} › {name} ==\n{text}" if several else text)
        print("\n\n".join(blocks), file=out)
        return 0
    if args.command == "recheck":
        from .trace import recheck as recheck_work, recheck_lines

        checked = recheck_work(client, int(args.message_id), int(args.after))
        if args.json:
            import json

            print(json.dumps(checked.as_dict(), ensure_ascii=False, indent=1), file=out)
        else:
            print("\n".join(recheck_lines(checked)), file=out)
        return 0 if checked.verdict != "unreadable" else 1
    if args.command in ("hold", "release", "disposition"):
        from .holds import holds_command, release_command

        if args.command == "hold":
            return holds_command(client, args, out, home=home_from_environment())
        if args.command == "disposition":
            from .dispositions import command as disposition_command

            return disposition_command(client, args, out, home=home_from_environment())
        return release_command(client, args, out)
    if args.command == "receipt":
        from .receipt import inspect as inspect_receipt, receipt_lines, repair as repair_receipt

        found = inspect_receipt(client, int(args.message_id), journal=args.journal, because=int(args.because or 0))
        if args.repair:
            found = repair_receipt(client, found)
        if args.json:
            import json

            print(json.dumps(found.as_dict(), ensure_ascii=False, indent=1), file=out)
        else:
            print("\n".join(receipt_lines(found, repaired=args.repair)), file=out)
        if found.state == "unknown":
            return 1
        return 2 if args.repair and found.state == "missing" else 0
    if args.command == "trace":
        origin = args.message_id
        if origin is None:
            # The conversation this run is serving — by its newest message,
            # not by the run's anchor: a run called back from somebody else's
            # topic is anchored to the post that named it *there*, and a trace
            # from it sees only that topic (robust_workflow p1, trial N1).
            home = home_from_environment()
            if home is not None:
                live = live_topic_name(client, home.channel, home.topic)
                origin = client.topic_last_id(home.channel, live) or home.anchor
        if origin is None:
            raise AgentChatError(
                "trace needs a message id: none was given and this run's "
                "conversation carries no anchor"
            )
        from .trace import trace as trace_request, trace_lines

        result = trace_request(client, int(origin))
        if args.json:
            import json

            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=1), file=out)
        else:
            print("\n".join(trace_lines(result)), file=out)
        return 0 if result.root is not None else 1
    if args.command == "channels":
        lines = channel_lines(client.channels(), args.prefix)
        if not lines:
            where = f" starting with {args.prefix}" if args.prefix else ""
            print(f"no channels{where}", file=out)
            return 0
        print("\n".join(lines), file=out)
        return 0
    if args.command == "resolve":
        bare = args.topic
        if bare.startswith(RESOLVED_TOPIC_PREFIX):
            bare = bare[len(RESOLVED_TOPIC_PREFIX):]
        resolved = f"{RESOLVED_TOPIC_PREFIX}{bare}"
        if client.topic_last_id(args.channel, resolved):
            print(f"#{args.channel} > {bare} is already resolved", file=out)
            return 0
        history = client.topic_history(args.channel, bare, num_before=LAST_SPEAKER_LOOKBACK)
        if not history:
            raise AgentChatError(
                f"no messages in #{args.channel} > {bare}: there is no "
                "conversation here to resolve"
            )
        if not args.anyway:
            unanswered = unanswered_request(history, client.whoami())
            if unanswered:
                raise AgentChatError(f"#{args.channel} > {bare}: {unanswered}")
        client.resolve_topic(int(history[-1]["id"]), bare)
        print(f"resolved #{args.channel} > {bare}", file=out)
        return 0
    if args.command == "unresolve":
        bare = args.topic
        if bare.startswith(RESOLVED_TOPIC_PREFIX):
            bare = bare[len(RESOLVED_TOPIC_PREFIX):]
        resolved = f"{RESOLVED_TOPIC_PREFIX}{bare}"
        message_id = client.topic_last_id(args.channel, resolved)
        if not message_id:
            if client.topic_last_id(args.channel, bare):
                print(f"#{args.channel} > {bare} is not resolved", file=out)
                return 0
            raise AgentChatError(f"no conversation #{args.channel} > {resolved} to unresolve")
        twin = client.topic_history(args.channel, bare, num_before=LAST_SPEAKER_LOOKBACK)
        if twin and any(is_selfnote(m.get("content")) for m in twin):
            # A topic of that name that was opened as a conversation of its
            # own — its own anchor or identity notes — is other work that took
            # the freed name: merging would join two requests.
            raise AgentChatError(
                f"#{args.channel} > {bare} already holds a conversation of its own beside "
                f"{resolved!r}: renaming it back would merge two conversations. Read both, "
                "and continue in the one the work belongs to."
            )
        client.rename_topic(message_id, bare)
        folded = f" (folding in {len(twin)} stray post(s) made under the old name after the ✔)" if twin else ""
        print(f"unresolved #{args.channel} > {bare}{folded}", file=out)
        return 0
    if args.command == "reserve":
        from .acceptance import reservation_note

        home = home_from_environment()
        if home is None:
            raise AgentChatError("reserve records into the conversation you are serving; none is set (AGENTCHAT_HOME)")
        said = client.message(int(args.evidence))
        if said is None:
            raise AgentChatError(f"#{args.evidence} does not exist")
        where = locate(client, home) or home
        if (str(said.get("display_recipient") or ""), _bare_topic(str(said.get("subject") or ""))) != \
                (where.channel, _bare_topic(where.topic)):
            raise AgentChatError(f"#{args.evidence} is not in {where.channel}/{where.topic}: a reservation is "
                                 "recorded where the person said it")
        if int(said.get("sender_id") or 0) == int(client.whoami()["user_id"]):
            raise AgentChatError(f"#{args.evidence} is your own post: the reservation is the person's own words")
        client.send_to_channel(where.channel, where.topic,
                               reservation_note(int(said["sender_id"]), str(said.get("sender_full_name") or ""),
                                                int(args.evidence)))
        print(f"recorded in {where.channel}/{where.topic}: the final approval is "
              f"{said.get('sender_full_name') or said['sender_id']}'s own (#{args.evidence})", file=out)
        return 0
    if args.command == "accept":
        from .acceptance import AcceptanceRefused, accept_mission

        try:
            done = accept_mission(client, args.message_id, evidence=args.evidence, log=lambda line: None)
        except AcceptanceRefused as refused:
            raise AgentChatError(str(refused)) from refused
        print(done.summary(), file=out)
        return 0
    if args.command == "options":
        lines = exec_options_lines(harvest_intros(client), args.agent)
        if not lines:
            who = f" for {args.agent}" if args.agent else ""
            print(f"no introductions on #{AGENTS_CHANNEL}{who}", file=out)
            return 0
        print("\n".join(lines), file=out)
        return 0
    if args.command == "intro":
        entries = harvest_intros(client)
        if not entries:
            print(f"no introductions on #{AGENTS_CHANNEL}", file=out)
            return 0
        if args.agent is None:
            print("\n".join(intro_lines(entries)), file=out)
            return 0
        for name, body in entries:
            if name == args.agent:
                print(body, file=out)
                return 0
        raise AgentChatError(
            f"no introduction from {args.agent!r}; the agents on the board are: "
            + ", ".join(name for name, _ in entries)
        )
    if args.command == "use":
        refuse_resolved(client, args.channel, args.topic)
        joined = join_and_record(client, args.channel, args.topic, out)
        # Anchored like any other post of ours: a refusal names the poster,
        # and this is what lets that refusal find the conversation we asked
        # from. A selection nobody can hear refused is worse than none.
        ensure_rootchat(client, args.channel, args.topic, out)
        text = execopt.command_line(args.to, args.option)
        message_id = client.send_to_channel(args.channel, args.topic, text)
        print(
            f"sent message {message_id} to #{args.channel} > {args.topic}: {text}",
            file=out,
        )
        print(
            "that is configuration only — they will confirm it and start no "
            "work; post what you want done separately",
            file=out,
        )
        if joined:
            print(f"joined #{args.channel}", file=out)
        return 0
    if args.command == "anchor":
        home = (
            parse_conversation(args.home) if args.home else home_from_environment()
        )
        if home is None:
            raise AgentChatError(
                "there is no conversation to anchor to: this run is not "
                "serving one, so pass --home <channel>/<topic>"
                if not args.home
                else f"--home {args.home!r} is not a <channel>/<topic> pair"
            )
        if home.as_pair() == (args.channel, args.topic):
            raise AgentChatError(
                "a conversation is not anchored to itself; name the topic "
                "whose answers should come here, not this one"
            )
        refuse_resolved(client, args.channel, args.topic)
        joined = join_and_record(client, args.channel, args.topic, out)
        message_id = client.send_to_channel(
            args.channel, args.topic, rootchat_moved_note(home)
        )
        print(
            f"anchored #{args.channel} > {args.topic} to {home} "
            f"(note {message_id})",
            file=out,
        )
        print(
            "answers there will be served in this conversation from now on; "
            "nobody was notified, because that note is not a message anybody "
            "reads",
            file=out,
        )
        if joined:
            print(f"joined #{args.channel}", file=out)
        return 0
    if args.command == "argue":
        from .argue import ARGUE_CHANNEL, open_argue

        text = " ".join(args.text).strip()
        if not text:
            raise AgentChatError("refusing to open an argue with an empty invitation")
        origin = parse_conversation(args.origin) if args.origin else home_from_environment()
        if args.origin and origin is None:
            raise AgentChatError(f"--from {args.origin!r} is not a <channel>/<topic> pair")
        try:
            topic, anchor_id, post_id = open_argue(client, args.stem, text, origin=origin)
        except ValueError as error:
            raise AgentChatError(str(error)) from error
        print(f"opened #{ARGUE_CHANNEL} > {topic} (argue {anchor_id}, invitation {post_id})", file=out)
        print(
            f"it grew out of {origin}" if origin is not None else "it names no origin conversation",
            file=out,
        )
        print("the human is expected to speak there next; do not post into it again from this run", file=out)
        return 0
    if args.command == "topics":
        names = _with_prefix(client.channel_topics(client.stream_id(args.channel)), args.prefix)
        if not names:
            where = f" starting with {args.prefix}" if args.prefix else ""
            print(f"no topics{where} in #{args.channel}", file=out)
            return 0
        print("\n".join(names), file=out)
        return 0
    raise AgentChatError(f"unknown command: {args.command}")


#: The commands that change the realm. A failure of one of them is an event
#: the run's own report may leave out; `OPFAIL_TAG` keeps it where a reader
#: of the request can find it (`agag.trace`).
WRITE_COMMANDS = ("send", "resolve", "unresolve", "use", "anchor", "argue", "accept", "reserve")
OPFAIL_TAG = "opfail"


def record_failure(client, args, error, environ=None) -> None:
    """`[selfnote][opfail] <command> <channel>/<topic>: <reason>` in the
    conversation this run is serving.

    `robust_workflow` p1 step 2. An agent whose post was refused carried on
    with whatever the refusal suggested and reported the result, and the
    refusal itself survived only in its own transcript. A note in the home
    conversation is the one place every later reader of the request looks,
    and a selfnote buys nobody a run. A timeout is recorded as *uncertain*:
    the post may have landed. Best effort — a note that cannot be written is
    not a second failure to report.
    """
    home = home_from_environment(environ)
    if home is None or client is None:
        return
    target = "/".join(str(part) for part in (getattr(args, "channel", ""), getattr(args, "topic", "")) if part)
    if not target and getattr(args, "message_id", None):
        target = f"#{args.message_id}"
    kind = "refused" if isinstance(error, (AgentChatError, ZulipRejected)) else "uncertain"
    reason = " ".join(str(error).split())[:300]
    try:
        client.send_to_channel(
            home.channel, home.topic,
            note(OPFAIL_TAG, f"{args.command} {target or '-'} {kind}: {reason}"),
        )
    except Exception:  # noqa: BLE001 - best effort, see above
        pass


def main(argv: list[str] | None = None, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    client = None
    try:
        client = client_from_environment(reads=args.command in MIRROR_READS)
        return _run(args, client, out)
    except (AgentChatError, ZulipError) as error:
        print(f"agentchat: {error}", file=err)
        if args.command in WRITE_COMMANDS:
            record_failure(client, args, error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
