# `ag.post.v1` — what a post is for

Implementation: `src/agag/post.py`. Introduced by `clearer_chat_ui` step 1.

A mention says whose turn it is. It does not say whether the speaker waits
for an answer: a finished report and a question both name the requester.
`ag.post.v1` is the one place a post says what it is.

## Wire format

One line at the very end of the message's raw Markdown, outside any code
fence, as an inline code span:

```
`ag-post intent=<intent> [to=<user id>] [ask=<kind>] [re=<id>[,<id>…] | answer=none] [seen=<id>]`
```

| key | values | meaning |
|---|---|---|
| `intent` | `progress` | work is under way; nobody has to answer |
| | `report` | information or a result; nobody has to answer |
| | `response_request` | the poster cannot go on until `to` answers |
| `to` | Zulip user id | whose answer is requested — **required** with `response_request`, refused otherwise |
| `ask` | `question`, `confirmation` | optional nuance of a request; refused without one |
| `re` | message id(s), comma separated | the request(s) this post answers — or, from the asker, withdraws or supersedes; may stand alone, without `intent` |
| `answer` | `none` | the post answers **no** request — not even the one the next-post rule would give it (`clearer_chat_ui` ex1); may stand alone; contradicts `re=` |
| `seen` | message id | the newest post the poster had read when it wrote this (a listener's processed-input boundary); written on requests |

Rules:

- **No line, no meaning.** A post without the line is *unclassified*.
  Unclassified never means "somebody is waiting".
- **Only the last non-blank line counts, and only outside a code fence**
  (fence-aware like the `ag-reply` splitter: a four-backtick block holding a
  three-backtick one closes only at four). Quoting the format inside a code
  block is text.
- **A malformed line is still a machine line.** It is removed from what a
  person reads, and the post is read as unclassified; `parse_post` returns
  why (`error`). Unknown keys, repeated keys, non-numeric ids, an unknown
  intent or ask, `to=` without a request, a request without `to=`, an
  `answer=` other than `none` and `answer=none` together with `re=` are all
  malformed.
- **Three correlation choices.** No `re=`/`answer=`: let the reader
  correlate (the next-post rule). `re=<ids>`: answers exactly those.
  `answer=none`: answers nothing. The two explicit choices exclude each
  other.
- **One message, one write.** The line is part of the content, so it is
  prepared, journaled and delivered with the words: `agag.delivery` matches a
  read-back on the whole content, and a redelivery after a crash or a
  restart carries the same meaning byte for byte. There is no second post to
  lose.
- **Intent is not identity, transport or execution.** The sender is Zulip's;
  a listener's ack is a transport receipt and carries no line; whether a run
  is executing is the serving journal's. A progress post does not settle a
  request.

## Examples

```
@**Developer**

I would open a workplan in pj-demo for the three steps above. Shall I go ahead?

`ag-post intent=response_request to=8 ask=confirmation`
```

```
Rendering scene 2 of 5.

`ag-post intent=progress`
```

```
@**Front**

The plan is posted in pj-demo › workplan-login; task 1 has started.

`ag-post intent=report re=9120`
```

A person answering through a room (the relay writes the reference):

```
Go ahead with B.

`ag-post re=9120`
```

A person writing something else while a question waits for them (the room's
"not an answer"):

```
Unrelated: the staging box is back up.

`ag-post answer=none`
```

A person answering in Zulip itself with *Quote and reply* makes the same
reference without knowing this contract: `agag.post.quoted_ids` reads the
`…/near/<id>` link Zulip writes.

## Producing it

- **A run's reply** declares it on the opening fence of its `ag-reply`
  block (`agag.reply`):

  ````
  ```ag-reply intent=response_request ask=question
  Which branch should I use?
  ```
  ````

  `to=` may be left out: the listener addresses the request to the requester
  it recorded from the processed input (`agag.topics.requester_of`). Several
  blocks are one post whose intent is the strongest any block declares
  (`response_request` > `report` > `progress`). A misspelt attribute never
  costs the answer: the reply is posted unclassified and the reason is
  logged. A handler failure and the "produced no reply" line are `report`s.
  A handler posting literal sections says what they are with
  `TopicResult(meta=PostMeta(...))`.
- **Handler and run together** (`agag.post.combine`, `clearer_chat_ui`
  ex1). One delivered post can carry a run's words and a handler's state
  (autolab's task serving: the run reports its work, the handler still
  waits for the requester to agree). The two metas combine by role, not
  by strength:

  | handler `TopicResult.meta` | run's fence | post |
  |---|---|---|
  | `response_request` (a **requirement**) | anything, or nothing | `response_request`; `to` and `ask` are the handler's; a `to`/`ask` the handler left out comes from the run's request (`ask` only when it asked the same person); a `to` still missing is the recorded requester |
  | `progress` / `report` (a **default**) | an intent | the run's |
  | `progress` / `report` | none | the handler's |
  | none | anything | the run's |

  `re=` is the union (run's first); `seen` is always the listener's. A
  failed reply (no usable mark after the repair) counts as the run
  declaring `report` — a requirement still stands over it. A handler that
  raised reached no state and requires nothing: its failure line is a
  `report`. A request with nobody to address (no requester recorded, or
  only the bot itself) falls back to the run's own non-request intent, or
  to unclassified.
- **`agentchat send`** takes `--intent`, `--to <user id | exact Zulip name>`,
  `--ask`, `--re <id>` (repeatable) and `--not-answer` (refused with `--re`). A request without `--to` is refused
  before anything is posted.
- **Anything else** calls `agag.post.compose(text, PostMeta(...))`, which
  refuses a meta it could not read back.

## Reading it

- `parse_post(content)` → `ParsedPost(text, meta, error)`; `strip(content)`
  for the words alone.
- Agent-facing renderings replace the line with its meaning:
  `format_chatlog` writes `[Front] (asks Developer to answer (question)) …`,
  `agentchat read` puts the same words in each message header. No rendering
  shows the raw line, so no run learns to type it by hand.
- `agag.selfnote.is_progress` is true for `intent=progress` (and, as before,
  for posts made of nothing but `🔧`/`💬` progress lines); a declared other
  intent wins over the line shapes.

## Outstanding requests (`agag.outstanding`, `ag.outstanding.v1`)

`read_requests(messages, complete=…, closed=…, stale=…, is_ack=…)` is the
shared read model: a pure function of one conversation's history, so a
restart or a rebuilt mirror concludes the same. A request is its message
id; its post keeps its intent forever, and its **state** is derived:
`pending`, `overtaken` (the recipient spoke after `seen` and before the
request landed — their input is owed a serving, so they are not being
asked yet), `answered` (by `reference`, `quote` or `next_post`), `withdrawn`
/ `superseded` (the asker's own `re=`), `closed` (unanswered in a ✔'d
conversation; `pending` again if it is reopened).

Correlation: an explicit `re=` or a Zulip quote-and-reply by the recipient
settles exactly what it names; otherwise a post by the recipient settles
their one pending request, and only when there is exactly one — with two
or more it settles none and is listed in `unmatched`. A post marked
`answer=none` settles nothing and is never `unmatched` (a quote in it
included); it still overtakes a request composed before it. Progress, acks,
selfnotes, system notices and anybody but the recipient settle nothing. A
receipt is not approval, acceptance or completion.

`complete=False` marks next-post answers `certain=False`; `stale=True` and
`complete=False` are said in `uncertain`. Edits are read as the post is
now; a deleted request is absent, a deleted answer returns its request to
`pending`.

`agag.trace` builds on it: in a person's conversation that the agent
answered last, `awaiting_human` now means an explicit request is pending,
`queued` an overtaken one, and `answered` that nothing is asked of anybody.
