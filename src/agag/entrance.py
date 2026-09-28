"""The entrance: a plain topic in the instance's own channel, answered by a run.

Every standardized agent's own channel is its entrance (`agent_standardize`
p4), and since p10 a plain topic there is answered by a front run reading the
board rather than by a canned sentence. forge and autolab grew the same
serving independently; this is that serving, once, for any `AgentSpec`:

  workspace under `.local/topics/<channel>/<topic>/<N>/front/`
  the conversation as `chatlog.md`
  one `roles.front` run with `agentchat` on PATH, transcript kept
  its closing message, posted back by `serve_topic`

What is per-agent is the **vocabulary** — where its own work is and what its
topics are called. Since `agent_guide` p2 the fixed half (answer from the
chat, ✔ is finished, list afresh every time, close out only when asked, never
`send` into this channel) is one shipped text, `agag/guides/entrance.md`, and
an agent's `agent/guides/entrance_front/guide.md` holds only its vocabulary;
one without gets `entrance_default.md` with its `{plan_prefix}`/`{run_prefix}`
filled in, which is enough to answer what the channel holds and to say where a
request goes.

Closing a finished topic out is done **when asked**. That is the contract,
not a shackle: the entrance answers questions and follows instructions, and
tidying on its own would be deciding somebody else's conversation is over.
"""

from __future__ import annotations

from .agent import SWEEP_ACK, AgentSpec, exec_options_for, is_ack, run_role
from .reply import repair_with
from .topics import (
    TopicResult,
    chatlog_path,
    chatlog_placement,
    conversation_context,
    format_chatlog,
    generation_dir,
    guide as read_guide,
    next_generation,
    next_record_path,
    prompt_with_guide,
    serve_topic,
    shared_text,
    topic_workspace,
)
from .zulip import ZulipClient, log

# The entrance reads chat and writes text. It generates nothing, but a survey
# of a channel's topics is many small reads, so it gets more than a plan
# front's 360.
ENTRANCE_TIMEOUT_SECONDS = 600
ROLE = "front"
GUIDE_PARTS = ("entrance_front", "guide.md")

EMPTY_REPLY = "There is nothing in this topic to answer yet."
NO_ANSWER = "(the run ended without a closing message)"

#: The entrance's fixed half and the vocabulary an agent without a guide of
#: its own gets (`agag/guides/entrance.md`, `entrance_default.md`).
ENTRANCE_SECTION = "entrance"
DEFAULT_VOCABULARY = "entrance_default"

__all__ = [
    "DEFAULT_VOCABULARY",
    "ENTRANCE_SECTION",
    "EMPTY_REPLY",
    "ENTRANCE_TIMEOUT_SECONDS",
    "EntranceError",
    "NO_ANSWER",
    "default_guide",
    "entrance_guide",
    "entrance_prompt",
    "handle_entrance",
    "serve_entrance",
]


class EntranceError(RuntimeError):
    """One entrance serving could not complete."""


def default_guide(spec: AgentSpec) -> str:
    """The default vocabulary with the agent's own topic prefixes filled in."""
    plan, run = spec.plan_prefix, spec.run_prefix
    if plan and run:
        prefix_line = f": `{plan}…` is a plan, `{run}…` is its run."
        request_line = (
            f"- A new request is a new `{plan}…` topic in this channel, "
            "not something started here.\n"
        )
    elif plan:
        prefix_line = f": `{plan}…` is a request."
        request_line = (
            f"- A new request is a new `{plan}…` topic in this channel, "
            "not something started here.\n"
        )
    else:
        prefix_line = "."
        request_line = ""
    return shared_text(DEFAULT_VOCABULARY).format(prefix_line=prefix_line, request_line=request_line).strip()


def entrance_guide(spec: AgentSpec) -> str:
    """The fixed half, then the agent's own vocabulary when it has one, else
    the default one."""
    path = spec.guides.joinpath(*GUIDE_PARTS)
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        vocabulary = read_guide(spec.guides, *GUIDE_PARTS)
    else:
        vocabulary = default_guide(spec)
    return f"{shared_text(ENTRANCE_SECTION)}\n\n{vocabulary}"


def entrance_prompt(spec: AgentSpec, bot_name: str, conversation: str = "") -> str:
    """The conversation, the chatlog placement, this instance's own name, then
    the guide.

    Naming the channel is not routing knowledge handed out: it is this
    agent's own name for its own entrance, which it would otherwise have to
    guess at from the chatlog.

    `conversation` is the rendered chatlog of this serving, carried in the
    prompt by `conversation_context` since `routine_tests` p2 ex1. The
    entrance had the same file-only shape Front's did — the question was a
    file the run had to decide to open — so it is repaired at the same time
    and in the same way. The file stays and is complete.
    """
    lines = [chatlog_placement(bot_name), f"Your own channel is {spec.instance_name()!r}."]
    if conversation:
        lines += ["", conversation]
    return prompt_with_guide(lines, entrance_guide(spec), reply=True)


def serve_entrance(spec: AgentSpec, context) -> TopicResult:
    """One question at the entrance, answered by a front run over the board."""
    workspace_root = topic_workspace(spec.topics_root, context.channel, context.topic)
    number = next_generation(workspace_root)
    workspace = generation_dir(
        spec.topics_root, context.channel, context.topic, number, ROLE
    )

    context.step = "chatlog placement"
    # One rendering: the file and the prompt's copy are the same bytes.
    chatlog = format_chatlog(context.history, context.self_id, drop=is_ack)
    chatlog_path(workspace).write_text(chatlog, encoding="utf-8")

    context.step = ROLE
    record = next_record_path(spec.records_root / "entrance_front")
    output, _, exit_code = run_role(
        spec,
        ROLE,
        entrance_prompt(spec, context.bot_name, conversation_context(chatlog)),
        cwd=workspace,
        timeout=ENTRANCE_TIMEOUT_SECONDS,
        record=record,
        # What the run actually looked at. Without it, an answer that
        # skipped a topic is indistinguishable from one that found nothing
        # in it — which is how `agent_standardize` p10 lost a whole project
        # on autolab's side and could not say why.
        transcript=workspace / "transcript.jsonl",
        stream=True,
        home=(context.channel, context.topic),
        # The entrance is work like any other, so it runs under whatever this
        # conversation was told to run under. An option that covered the
        # answer but not the working roles would be a menu that lies.
        selection=context.selection,
    )
    if exit_code != 0:
        raise EntranceError(f"front run exited {exit_code}: {output.strip()[:500]}")
    journal = getattr(context, "journal", None)
    if journal is not None:
        journal.record(str(record))

    def repair(prompt: str) -> str:
        again, _, code = run_role(spec, ROLE, prompt, cwd=workspace, timeout=ENTRANCE_TIMEOUT_SECONDS,
                                  record=next_record_path(spec.records_root / "entrance_front"),
                                  home=(context.channel, context.topic), selection=context.selection)
        return again if code == 0 else ""

    # The reply contract (`agag.reply`): only the marked reply is posted; an
    # unmarked output is repaired once, then reported as no reply.
    return TopicResult(output=output, repair=repair_with(repair, output))


def handle_entrance(spec: AgentSpec, client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one entrance topic through the shared skeleton."""
    log(f"entrance topic {channel!r}/{topic!r}")
    serve_topic(
        client, channel, topic, lambda context: serve_entrance(spec, context),
        ack_text=SWEEP_ACK,
        empty_reply=EMPTY_REPLY,
        exec_options=exec_options_for(spec, client),
    )
