"""The reply mark: which part of a run's output is said in the conversation.

`explicit_reply` p1 step 2. Until now an agent's reply was *whatever its run
printed last*, and the only boundary between the agent thinking and the
agent speaking was a sentence in each guide ("no notes to yourself"). The
observation that ended that is in the advice: Front's #7222 opens with two
paragraphs of Front telling itself what it is about to do, then the reply;
both went to Zulip as one post, the human read the thought as addressed to
them, and the presentation role re-voiced Front twice per reply.

The contract is one tagged block, each tag on a line of its own:

    <ag-reply intent=report>
    what is said, verbatim
    </ag-reply>

- **Inside the mark is what is said.** Several marks in one output are
  posted as one message, in order, so a run can write its reply in pieces.
- **Everything outside is the agent's own** — notes, plans, a draft it
  discarded — and stays in the run record and the transcript, unposted and
  unforbidden.
- **A reply may contain any Markdown, code fences included.** Inside the
  mark, fences pair as CommonMark pairs them (a fence opens a code block, a
  bare fence of its kind and at least its length closes it), and a
  `</ag-reply>` line inside a code block is quoted text, not the close.
  (`failsafe` p3: the mark used to be a fence itself, and a reply opened
  with three backticks ended at the first bare fence of a code block it
  contained — p2's #12328, #12338, #12410, #12419. A bare fence after the
  reply's text is either that close or a code block, and no rule over the
  same text can tell which, so the fenced mark is retired: a run that
  writes one is repaired with that reason.)
- **The attributes never cost the reply.** An attribute that cannot be read
  (`to=Omni Agent`, p2's #12509, where the whole opener used to be ignored)
  is dropped and said in `meta_error`; the readable ones stand.
- **Machine blocks** (`ag-argue`, `ag-routinerun`, any block a handler
  parses) are read from the whole output by their own splitters and never
  posted; the handler strips them before the output reaches `split_reply`,
  so their place relative to the mark does not matter.
- **No mark, an empty mark, or an unclosed mark is a failed reply**, not
  silence and not "post it all". The failure is repaired once — the run is
  asked, with its own previous output in front of it and the exact reason,
  for the reply alone (`repair_prompt`) — and if that produces no usable
  mark either, the conversation is told in one visible line that this run
  produced no reply, and the reply stays owed (`agag.topics`).
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
from dataclasses import dataclass, replace
from pathlib import Path

from .post import PostMeta, merge, parse_attributes

#: The mark's tag name.
REPLY_LANGUAGE = "ag-reply"

_FENCE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^`\n]*?)[ \t]*$")
_OPEN = re.compile(rf"^[ \t]{{0,3}}<{REPLY_LANGUAGE}(?P<attributes>[ \t][^>]*)?>[ \t]*$", re.IGNORECASE)
_CLOSE = re.compile(rf"^[ \t]{{0,3}}</{REPLY_LANGUAGE}[ \t]*>[ \t]*$", re.IGNORECASE)
_PAIR = re.compile(r"^[a-z_]+=\S+$")
OPEN_TAG, CLOSE_TAG = f"<{REPLY_LANGUAGE}>", f"</{REPLY_LANGUAGE}>"

__all__ = [
    "REPLY_GUIDE",
    "REPLY_LANGUAGE",
    "ReplySplit",
    "REPLY_ATTEMPTS",
    "failure_line",
    "owed_reply",
    "retry_notice",
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
    #: its opening tag(s): None when no block declared one.
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


def _attributes(text: str) -> tuple[PostMeta | None, str | None]:
    """The opener's attributes: every readable `key=value` stands, and what
    could not be read is dropped and named — a malformed attribute never
    costs the reply (p2 #12509: `to=Omni Agent`)."""
    text = (text or "").strip()
    if not text:
        return None, None
    meta, problem = parse_attributes(text, require_to=False)
    if problem is None:
        return meta, None
    kept, dropped = [], []
    for word in text.split():
        key, _, value = word.partition("=")
        if not _PAIR.match(word) or (key == "to" and not value.isdigit()):
            dropped.append(word)
        else:
            kept.append(word)
    meta, again = parse_attributes(" ".join(kept), require_to=False)
    if again is not None:
        return None, f"`{text}`: {problem}"
    return meta, f"`{text}`: dropped {' '.join(dropped)} ({problem})"


def split_reply(output: str) -> ReplySplit:
    """Split a run's output into what is said and what is not."""
    replies: list[str] = []
    metas: list[PostMeta] = []
    meta_errors: list[str] = []
    rest: list[str] = []
    body: list[str] = []
    in_reply = False
    fence: tuple[str, int] | None = None
    quoted_closes: list[int] = []
    retired = False
    error = None

    def close(upto: int, after: list[str]) -> None:
        nonlocal in_reply
        replies.append("\n".join(body[:upto]).strip("\n"))
        rest.extend(after)
        in_reply = False

    for line in str(output or "").splitlines():
        if not in_reply:
            opened = _OPEN.match(line)
            if opened:
                in_reply, fence, body, quoted_closes = True, None, [], []
                meta, problem = _attributes(opened.group("attributes") or "")
                if meta is not None:
                    metas.append(meta)
                if problem is not None:
                    meta_errors.append(problem)
                continue
            match = _FENCE.match(line)
            if match and match.group("info").strip().lower().split(None, 1)[:1] == [REPLY_LANGUAGE]:
                retired = True
            rest.append(line)
            continue
        if fence is None and _CLOSE.match(line):
            close(len(body), [])
            continue
        if fence is None and _OPEN.match(line):
            error = f"a {OPEN_TAG} block opens before the previous one is closed with a {CLOSE_TAG} line"
            break
        match = _FENCE.match(line)
        if match:
            marker = match.group("fence")
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1] and not match.group("info").strip():
                fence = None
        elif fence is not None and _CLOSE.match(line):
            quoted_closes.append(len(body))
        body.append(line)
    if in_reply and error is None:
        if quoted_closes:
            # A code block the reply opened and never closed swallowed the
            # close: the last `</ag-reply>` line is the close — the one
            # reading in which the reply ends at all.
            last = quoted_closes[-1]
            close(last, body[last + 1:])
        else:
            # Unclosed: the text after the opener is kept for the record, but
            # a mark the run did not finish is not a reply it meant to send.
            replies.append("\n".join(body).strip("\n"))
            error = f"the last {OPEN_TAG} block is not closed with a {CLOSE_TAG} line"
    reply = "\n\n".join(r for r in replies if r.strip()).strip()
    # Where a mark was cut out, the blank lines around it are folded, so
    # the rest reads as the run wrote it rather than with holes in it.
    rest_text = re.sub(r"\n{3,}", "\n\n", "\n".join(rest)).strip("\n")
    if error is None:
        if not replies:
            error = (f"the reply is in a fenced ```{REPLY_LANGUAGE} block, which is no longer read: put it "
                     f"between a {OPEN_TAG} line and a {CLOSE_TAG} line") if retired else \
                f"the output contains no {OPEN_TAG} block"
        elif not reply:
            error = f"the {OPEN_TAG} block is empty"
    meta = merge(metas) if metas else None
    return ReplySplit(reply, rest_text, len(replies), error, meta, "; ".join(meta_errors) or None)


REPLY_GUIDE = f"""\
# How your reply is posted

Nothing you write is posted by itself. The text you want said in the conversation goes between a `{OPEN_TAG}` line and a `{CLOSE_TAG}` line, each on a line of its own:

{OPEN_TAG}
What you say, exactly as it should appear.
{CLOSE_TAG}

Everything outside such blocks — notes to yourself, reasoning, a draft you discard — is yours: it stays in the run record and is not posted, and nothing forbids it. Several `{OPEN_TAG}` blocks are posted as one message, in order, so you may write the reply in pieces as you work. Inside the block write ordinary Markdown: code blocks, test output and quoted files keep their own ``` fences, and the text after them is posted too. Any machine block a guide asks for (`ag-argue`, `ag-routinerun`, …) is a fenced block outside the reply, read wherever it is in your output and never posted.

One post holds tens of thousands of characters, and nothing in it is cut. A reply longer than one post is not posted at all: you are asked once more for a shorter one, told the size it must fit. Quote the part of long output that matters (the tail of a test run with its `Ran … OK` line, the lines that failed, the diff of the files you changed) rather than all of it, and when the whole is worth keeping, write it to a file in your workspace and say where it is.

An output with no `{OPEN_TAG}` block, an empty one, or one never closed with `{CLOSE_TAG}` is a failed reply: you are asked once more for the reply alone, and if that fails too the conversation is told that this run produced no reply.

## Say what the reply is for

Write on the opening tag what your reply is, so a reader sees at a glance whether you are waiting for them:

- `intent=report` — information or a result; nobody has to answer. Most final replies are this.
- `intent=progress` — work is under way and you are only saying how far it got.
- `intent=response_request` — you cannot go on until somebody answers. It is addressed to the person you are replying to; add `to=<user id>` (a number, never a name) only to ask somebody else. `ask=question` or `ask=confirmation` says which kind of answer you want.

<{REPLY_LANGUAGE} intent=response_request ask=confirmation>
I would open a workplan in pj-demo for the three steps above. Shall I go ahead?
{CLOSE_TAG}

<{REPLY_LANGUAGE} intent=report>
The tests pass:

```
Ran 207 tests in 0.2s
OK
```

The change is committed as 41d5503.
{CLOSE_TAG}

In the conversation you are given, a question is labelled with its id (`request #9120`). When your reply answers one, or an earlier question of yours no longer stands, say which with `re=<id>` on the opening tag (`re=9120,9125` for several) — that is what takes it off the list of open questions; nothing else does. A post labelled `not an answer to any request` was written as an aside: the questions it follows are still open — answer what it says, and do not take it as their answer.

Ask only when you really wait for the answer: a question left in a report is not seen as one, and a report marked as a request tells somebody to reply for nothing. A reply without `intent=` is posted unclassified — never read as waiting. The label is written into the post for you; do not type it yourself. `agentchat send --help` says how to mark a post you send elsewhere the same way."""


def repair_prompt(previous_output: str, reason: str) -> str:
    """The one repair: the run's own output back to it, and a request for
    the reply alone. Machine-block effects are not to be repeated — the
    handler applied them before this point — and the prompt says so."""
    body = (previous_output or "").strip()
    shown = body if body else "(the output was empty)"
    return (
        f"Your previous output for this serving could not be posted: {reason}.\n\n"
        f"Write the reply now, between one {OPEN_TAG} line and one {CLOSE_TAG} line (with the `intent=` it "
        f"should carry on the opening tag), and nothing else. "
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


def failure_line(reason: str, *, final: bool = True) -> str:
    """The visible system failure when no reply could be made. The input it
    was given stays unanswered: the first failure says the reply is asked
    for once more; the last says it is left for the monitor to report."""
    then = ("the input stays unanswered and is reported" if final
            else "the reply is still owed and is asked for once more")
    return f"(this run produced no reply: {reason}; its output is kept in the run record; {then})"


#: Servings that may try to produce one reply for the same input: the one
#: that failed, and one re-serving of that input (`agag.listen`). Each runs
#: its own in-run repair first.
REPLY_ATTEMPTS = 2
#: How much of a failed run's own output the journal keeps for the retry.
OWED_OUTPUT_CHARS = 12000


def owed_reply(record) -> dict | None:
    """The reply a delivered serving still owes (`failsafe` p3), or None.

    A serving whose run produced no usable reply even after its repair
    posted a failure line, but the input it was given was not answered:
    its journal record carries `extra.reply_owed` (the reason, the attempt,
    the run's own output) until a later serving of the same input settles
    it. The last attempt (`final`) owes nothing more to the listener; its
    failure is the record the monitor escalates."""
    extra = getattr(record, "extra", None) or {}
    owed = extra.get("reply_owed") if isinstance(extra, dict) else None
    if not isinstance(owed, dict) or owed.get("final") or owed.get("settled_by"):
        return None
    return owed


def retry_notice(owed: dict) -> str:
    """What the re-serving of an unanswered input is told: the previous
    run's own output, and that everything it did has happened."""
    shown = str(owed.get("output") or "").strip() or "(the output was empty)"
    return (
        "# Your previous run on this input produced no usable reply\n\n"
        f"Its reply could not be posted ({owed.get('reason') or 'no reply'}), so the conversation was told that "
        "the reply is still owed and this serving was started for it. Everything that run did has already "
        "happened — posts elsewhere, files, commands, recorded decisions, machine blocks. Do not repeat any of "
        "it; if you are unsure whether something happened, check the threads and the workspace. Its own output "
        "is below for reference. Write the reply from where things stand now.\n\n"
        f"===== PREVIOUS OUTPUT =====\n{shown}\n===== END OF PREVIOUS OUTPUT ====="
    )


def too_long(reply: str, limit: int) -> str:
    """Why a reply of this size cannot be posted, said so the run can fix it."""
    return (f"the reply is {len(reply)} characters and one post here holds at most {limit} of them (after the "
            "listener's own lines); nothing is cut, so it was not posted. Write a shorter reply: quote what matters "
            "and keep the whole text in a file in your workspace, naming its path")


def _fitting(split: ReplySplit, limit: int | None) -> ReplySplit:
    """A reply over one post is a failed reply, never a cut one (failsafe p4)."""
    if limit is None or not split.ok or len(split.reply) <= limit:
        return split
    return replace(split, error=too_long(split.reply, limit))


def resolve_reply(output: str, repair: Callable[[str], str] | None = None, *, log=None,
                  limit: int | None = None) -> tuple[str, ReplySplit, bool]:
    """`(text to post, the split it came from, repaired)`.

    The split of `output`; when it is unusable and a `repair` is given, one
    repair run and its split; when that is unusable too (or there is no
    repair), the visible failure line as the text, with the split saying
    why. The caller records the split's outcome beside the run identity.
    `limit` is the most the reply may hold in its post: a longer one is
    unusable for that reason, and the repair is asked for one that fits.
    """
    split = _fitting(split_reply(output), limit)
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
        split = _fitting(split_reply(again), limit)
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
