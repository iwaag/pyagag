"""The entrance: a plain topic in the instance's own channel, answered by a run.

Every standardized agent's own channel is its entrance (`agent_standardize`
p4), and since p10 a plain topic there is answered by a front run reading the
board rather than by a canned sentence. forge and autolab grew the same
serving independently; this is that serving, once, for any `AgentSpec`:

  workspace under `.local/topics/<channel>/<topic>/<N>/front/`
  the conversation as `chatlog.md`
  one `roles.front` run with `agentchat` on PATH, transcript kept
  its closing message, posted back by `serve_topic`

What is per-agent is the **guide** — the vocabulary of its own topics. An
agent with `agent/guides/entrance_front/guide.md` uses that; one without
gets `DEFAULT_GUIDE` with its `{plan_prefix}`/`{run_prefix}` filled in, which
is enough to answer what the channel holds and to say where a request goes.

Closing a finished topic out is done **when asked**. That is the contract,
not a shackle: the entrance answers questions and follows instructions, and
tidying on its own would be deciding somebody else's conversation is over.
"""

from __future__ import annotations

from .agent import SWEEP_ACK, AgentSpec, is_ack, run_role
from .topics import (
    TopicResult,
    chatlog_path,
    chatlog_placement,
    format_chatlog,
    generation_dir,
    guide as read_guide,
    next_generation,
    next_record_path,
    prompt_with_guide,
    serve_topic,
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

DEFAULT_GUIDE = """\
You are this instance's entrance. Answer what the chatlog asks, reading what you need from the chat, and start no work here.

- `agentchat topics <your own channel>` lists your conversations{prefix_line}
- A name beginning with `✔` is a conversation somebody marked finished.
- `agentchat read <your own channel> <topic>` for the detail of one of them.
- Read only the topics the question needs, but list the channel every time: your own earlier answers here are history, not the current state.
{request_line}
If you are asked to close out finished work: read those topics to check they really are finished, then `agentchat resolve <your own channel> <topic>` for each. Only when asked.

Your reply is the last thing you say in this run, and it is posted into this topic for you. Never `agentchat send` into this channel — doing that posts your answer twice.
"""

__all__ = [
    "DEFAULT_GUIDE",
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
    """`DEFAULT_GUIDE` with the agent's own topic vocabulary filled in."""
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
    return DEFAULT_GUIDE.format(prefix_line=prefix_line, request_line=request_line)


def entrance_guide(spec: AgentSpec) -> str:
    """The agent's own guide when it has one, else the built-in default."""
    path = spec.guides.joinpath(*GUIDE_PARTS)
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return read_guide(spec.guides, *GUIDE_PARTS)
    return default_guide(spec)


def entrance_prompt(spec: AgentSpec, bot_name: str) -> str:
    """The chatlog placement, this instance's own name, then the guide.

    Naming the channel is not routing knowledge handed out: it is this
    agent's own name for its own entrance, which it would otherwise have to
    guess at from the chatlog.
    """
    return prompt_with_guide(
        [chatlog_placement(bot_name), f"Your own channel is {spec.instance_name()!r}."],
        entrance_guide(spec),
    )


def serve_entrance(spec: AgentSpec, context) -> TopicResult:
    """One question at the entrance, answered by a front run over the board."""
    workspace_root = topic_workspace(spec.topics_root, context.channel, context.topic)
    number = next_generation(workspace_root)
    workspace = generation_dir(
        spec.topics_root, context.channel, context.topic, number, ROLE
    )

    context.step = "chatlog placement"
    chatlog_path(workspace).write_text(
        format_chatlog(context.history, context.self_id, drop=is_ack), encoding="utf-8"
    )

    context.step = ROLE
    output, _, exit_code = run_role(
        spec,
        ROLE,
        entrance_prompt(spec, context.bot_name),
        cwd=workspace,
        timeout=ENTRANCE_TIMEOUT_SECONDS,
        record=next_record_path(spec.records_root / "entrance_front"),
        # What the run actually looked at. Without it, an answer that
        # skipped a topic is indistinguishable from one that found nothing
        # in it — which is how `agent_standardize` p10 lost a whole project
        # on autolab's side and could not say why.
        transcript=workspace / "transcript.jsonl",
        stream=True,
        home=(context.channel, context.topic),
    )
    if exit_code != 0:
        raise EntranceError(f"front run exited {exit_code}: {output.strip()[:500]}")
    return TopicResult([output.strip() or NO_ANSWER])


def handle_entrance(spec: AgentSpec, client: ZulipClient, channel: str, topic: str) -> None:
    """Serve one entrance topic through the shared skeleton."""
    log(f"entrance topic {channel!r}/{topic!r}")
    serve_topic(
        client, channel, topic, lambda context: serve_entrance(spec, context),
        ack_text=SWEEP_ACK,
        empty_reply=EMPTY_REPLY,
    )
