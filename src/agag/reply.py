"""The reply mark: which part of a run's output is said in the conversation.

`explicit_reply` p1 step 2. Until now an agent's reply was *whatever its run
printed last*, and the only boundary between the agent thinking and the
agent speaking was a sentence in each guide ("no notes to yourself"). The
observation that ended that is in the advice: Front's #7222 opens with two
paragraphs of Front telling itself what it is about to do, then the reply;
both went to Zulip as one post, the human read the thought as addressed to
them, and the presentation role re-voiced Front twice per reply.

The contract is one fenced block:

    ```ag-reply
    what is said, verbatim
    ```

- **Inside the mark is what is said.** Several marks in one output are
  posted as one message, in order, so a run can write its reply in pieces.
- **Everything outside is the agent's own** — notes, plans, a draft it
  discarded — and stays in the run record and the transcript, unposted and
  unforbidden.
- **A reply may contain ordinary code fences.** The splitter is fence-aware
  in the CommonMark sense: a mark opened with four backticks closes only at
  four or more, and inside a mark a fence *with an info string* opens a
  nested code block that a bare fence of its own length closes.
- **Machine blocks** (`ag-argue`, `ag-routinerun`, any block a handler
  parses) are read from the whole output by their own splitters and never
  posted; the handler strips them before the output reaches `split_reply`,
  so their place relative to the mark does not matter.
- **No mark, an empty mark, or an unclosed mark is a failed reply**, not
  silence and not "post it all". The advice proposed posting an unmarked
  output whole as a compatibility fallback; this phase retires that, since
  the fallback would keep the doubled post alive for exactly the runs that
  most need the boundary. The failure is repaired once — the run is asked,
  with its own previous output in front of it, for the reply alone
  (`repair_prompt`) — and if that produces no usable mark either, the
  conversation is told in one visible line that this run produced no reply.
  Repair never re-applies a machine block: the handler applied those before
  handing the output over, and the repair prompt says so.

`REPLY_GUIDE` describes the mark to the model as a tool, once, appended by
`agag.topics.prompt_with_guide(..., reply=True)` to every conversational
role's prompt, so no role guide has to carry a prohibition.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .post import PostMeta, merge, parse_attributes

#: The fence's info string.
REPLY_LANGUAGE = "ag-reply"

_FENCE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^`\n]*?)[ \t]*$")
_ATTRIBUTE = re.compile(r"^[a-z_]+=\S+$")

__all__ = [
    "REPLY_GUIDE",
    "REPLY_LANGUAGE",
    "ReplySplit",
    "failure_line",
    "record_reply_outcome",
    "repair_prompt",
    "repair_with",
    "resolve_reply",
    "split_reply",
]


@dataclass(frozen=True)
class ReplySplit:
    """`reply` is the text to post (the marks, joined in order); `rest` is
    everything else; `blocks` how many marks were found; `error` says why
    the reply is unusable (None when it is)."""

    reply: str
    rest: str
    blocks: int
    error: str | None = None
    #: What the reply is for (`agag.post`), from the attributes written on
    #: its fence(s): None when no block declared one.
    meta: PostMeta | None = None
    #: Why a declared intent could not be used; the reply is still posted,
    #: unclassified — a misspelt attribute never costs the answer.
    meta_error: str | None = None

    @property
    def marked(self) -> bool:
        return self.blocks > 0

    @property
    def ok(self) -> bool:
        return self.error is None


def split_reply(output: str) -> ReplySplit:
    """Split a run's output into what is said and what is not."""
    replies: list[str] = []
    metas: list[PostMeta] = []
    meta_errors: list[str] = []
    rest: list[str] = []
    body: list[str] = []
    in_reply = False
    outer = 0
    outer_char = "`"
    nested: tuple[str, int] | None = None
    for line in str(output or "").splitlines():
        match = _FENCE.match(line)
        fence = match.group("fence") if match else ""
        info = match.group("info").strip() if match else ""
        if not in_reply:
            words = info.split(None, 1)
            # The mark is `ag-reply` followed by nothing but `key=value`
            # attributes (`agag.post`); any other word is another info string.
            if match and words and words[0].lower() == REPLY_LANGUAGE and (
                    len(words) == 1 or all(_ATTRIBUTE.match(word) for word in words[1].split())):
                in_reply, outer, outer_char, nested, body = True, len(fence), fence[0], None, []
                if len(words) > 1:
                    meta, problem = parse_attributes(words[1], require_to=False)
                    if problem is None:
                        metas.append(meta)
                    else:
                        meta_errors.append(f"`{info}`: {problem}")
            else:
                rest.append(line)
            continue
        if nested is None and not match and line.strip().lower() == f"</{REPLY_LANGUAGE}>":
            # An HTML-style close — a slip seen live (robust_workflow p1, N1),
            # unambiguous outside a code fence.
            replies.append("\n".join(body).strip("\n"))
            in_reply = False
            continue
        if match:
            if nested is None:
                if not info and fence[0] == outer_char and len(fence) >= outer:
                    replies.append("\n".join(body).strip("\n"))
                    in_reply = False
                    continue
                nested = (fence[0], len(fence))
            elif not info and fence[0] == nested[0] and len(fence) >= nested[1]:
                nested = None
        body.append(line)
    error = None
    if in_reply:
        # One bare fence as the block's last line, and no other fence in it:
        # the close was written shorter than the opener (````ag-reply …
        # ```), the slip the repair run made in robust_workflow p1 N1. With
        # no other fence in the block it cannot be a code block's opening.
        fences = [i for i, text in enumerate(body) if _FENCE.match(text)]
        last = max((i for i, text in enumerate(body) if text.strip()), default=-1)
        if len(fences) == 1 and fences[0] == last:
            bare = _FENCE.match(body[last])
            if not bare.group("info").strip() and bare.group("fence")[0] == outer_char:
                replies.append("\n".join(body[:last]).strip("\n"))
                in_reply = False
    if in_reply:
        # Unclosed: the text after the opener is kept for the record, but
        # a mark the run did not finish is not a reply it meant to send.
        replies.append("\n".join(body).strip("\n"))
        error = f"the last {REPLY_LANGUAGE} block is not closed"
    reply = "\n\n".join(r for r in replies if r.strip()).strip()
    # Where a mark was cut out, the blank lines around it are folded, so
    # the rest reads as the run wrote it rather than with holes in it.
    rest_text = re.sub(r"\n{3,}", "\n\n", "\n".join(rest)).strip("\n")
    if error is None:
        if not replies:
            error = f"the output contains no {REPLY_LANGUAGE} block"
        elif not reply:
            error = f"the {REPLY_LANGUAGE} block is empty"
    meta = None if meta_errors else merge(metas)
    return ReplySplit(reply, rest_text, len(replies), error, meta, "; ".join(meta_errors) or None)


REPLY_GUIDE = f"""\
# How your reply is posted

Nothing you write is posted by itself. The text you want said in the conversation goes inside a fenced block marked `{REPLY_LANGUAGE}`:

```{REPLY_LANGUAGE}
What you say, exactly as it should appear.
```

Everything outside such blocks — notes to yourself, reasoning, a draft you discard — is yours: it stays in the run record and is not posted, and nothing forbids it. Several `{REPLY_LANGUAGE}` blocks are posted as one message, in order, so you may write the reply in pieces as you work. A reply may itself contain code fences; open the reply block with four backticks (````{REPLY_LANGUAGE}) when it does. Any machine block a guide asks for (`ag-argue`, `ag-routinerun`, …) is read wherever it is in your output and is never posted, so its place relative to the reply does not matter.

An output with no `{REPLY_LANGUAGE}` block, or an empty one, is a failed reply: you are asked once more for the reply alone, and if that fails too the conversation is told that this run produced no reply.

## Say what the reply is for

Write on the opening fence what your reply is, so a reader sees at a glance whether you are waiting for them:

- `intent=report` — information or a result; nobody has to answer. Most final replies are this.
- `intent=progress` — work is under way and you are only saying how far it got.
- `intent=response_request` — you cannot go on until somebody answers. It is addressed to the person you are replying to; add `to=<user id>` only to ask somebody else. `ask=question` or `ask=confirmation` says which kind of answer you want.

```{REPLY_LANGUAGE} intent=response_request ask=confirmation
I would open a workplan in pj-demo for the three steps above. Shall I go ahead?
```

```{REPLY_LANGUAGE} intent=report
The plan is posted in pj-demo › workplan-login; autolab has started task 1.
```

Ask only when you really wait for the answer: a question left in a report is not seen as one, and a report marked as a request tells somebody to reply for nothing. A reply without `intent=` is posted unclassified — never read as waiting. The label is written into the post for you; do not type it yourself. `agentchat send --help` says how to mark a post you send elsewhere the same way."""


def repair_prompt(previous_output: str, reason: str) -> str:
    """The one repair: the run's own output back to it, and a request for
    the reply alone. Machine-block effects are not to be repeated — the
    handler applied them before this point — and the prompt says so."""
    body = (previous_output or "").strip()
    shown = body if body else "(the output was empty)"
    return (
        f"Your previous output for this serving could not be posted: {reason}.\n\n"
        f"Write the reply now, inside one fenced `{REPLY_LANGUAGE}` block (with the `intent=` it should carry "
        f"on its opening fence), and nothing else. "
        f"Do not run any tool or take any action: everything the previous output did has already "
        f"happened, and any machine block it contained has already been applied — do not include one again. "
        f"If the previous output already contains the words you meant to say, put those words in the block.\n\n"
        f"===== YOUR PREVIOUS OUTPUT =====\n{shown}\n===== END OF YOUR PREVIOUS OUTPUT =====\n\n"
        f"{REPLY_GUIDE}"
    )


def repair_with(run: Callable[[str], str], previous_output: str) -> Callable[[str], str]:
    """A `TopicResult.repair` callable from a `run(prompt) -> output`:
    the consumer supplies how its role is run, this supplies the prompt."""
    return lambda reason: run(repair_prompt(previous_output, reason))


def failure_line(reason: str) -> str:
    """The visible system failure when no reply could be made."""
    return f"(this run produced no reply: {reason}; its output is kept in the run record)"


def resolve_reply(output: str, repair: Callable[[str], str] | None = None, *, log=None) -> tuple[str, ReplySplit, bool]:
    """`(text to post, the split it came from, repaired)`.

    The split of `output`; when it is unusable and a `repair` is given, one
    repair run and its split; when that is unusable too (or there is no
    repair), the visible failure line as the text, with the split saying
    why. The caller records the split's outcome beside the run identity.
    """
    split = split_reply(output)
    repaired = False
    if not split.ok and repair is not None:
        if log is not None:
            log(f"reply unusable ({split.error}); asking the run once more for the reply alone")
        try:
            again = repair(split.error or "no reply")
        except Exception as error:  # noqa: BLE001 - a failed repair is the failure, out loud
            again = ""
            if log is not None:
                log(f"the repair run failed: {error!r}")
        split = split_reply(again)
        repaired = True
    if split.ok:
        return split.reply, split, repaired
    return failure_line(split.error or "no reply"), split, repaired


def record_reply_outcome(path: str | Path, **fields) -> bool:
    """Add the reply and delivery outcome to an `ag.agent-run.v1` record on
    disk (`reply_marked`, `reply_blocks`, `reply_failure`, `reply_repaired`,
    `delivered_id`, `serving_id`). False when the file is not there or not
    JSON — the record is evidence, and evidence is never fabricated."""
    try:
        target = Path(path)
        record = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    record["reply"] = {key: value for key, value in fields.items() if value is not None}
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True
