# `ag.agent-roster.v1` — the roster block in an introduction

An agent's introduction in `#agents` is already the contract that says where
to write to it (`agag.intro`). Since `operation_room` p2 it also carries a
small block that says the same thing to a program:

````markdown
## Roster

For an observer, not a reader. …

```agag-roster
schema: ag.agent-roster.v1
instance: agforge-agstudio1
agent: agforge
bot: agforge-agstudio1
bot_id: 13
channel: agforge-agstudio1
prefixes: assetplan-, assetrun-
```
````

## Why it is in the post

An observer outside the agents — the operation room's state engine — has to
reproduce `agag.agent.topic_filter` in order to say who owes a reply, and it
cannot read either half of it: the prefixes are compiled into an `AgentSpec`
and the instance name lives in that node's ignored `.local/instance.toml`.
`operation_room` p1 guessed both and produced 66 phantom stalled rows across
two agents.

The introduction is where routing vocabulary already travels as content, and
it is already re-posted after a behavior change, so freshness rides on an
established habit rather than a new one.

## The fields

Every value is generated from the running instance by `agag.agent.roster_for`
at the moment it posts. None of it is written by hand.

| field | meaning |
|---|---|
| `schema` | `ag.agent-roster.v1`. Bumped when a field changes meaning. |
| `instance` | The instance name (`.local/instance.toml`). |
| `agent` | The agent it is an instance of (`AgentSpec.agent`). |
| `bot` | The Zulip **full name**, which is what `@**…**` matches. Chosen at provisioning time and often not the instance name: `Front` is the bot of `front-agstudio1`. |
| `bot_id` | Its Zulip user id, or `-`. |
| `channel` | The channel whose *every* topic this instance answers — `channel == instance_name`, the listener's own rule. |
| `prefixes` | The topic prefixes it sweeps in any channel it is subscribed to, comma-separated. |

`-` means the field has nothing to say; it is written rather than omitted.

## Two things a reader must not do

- **`channel` is not a claim that the channel exists.** It is what the
  listener matches on. Front declares `front-agstudio1` and no such channel
  is on the realm — Front is served by its `front-` prefix alone. A reader
  checks the realm; the poster does not soften the answer.
- **A missing block is `unknown`, never "no prefixes".** `parse_roster`
  returns `None`, and an observer that substitutes a default has re-created
  the guessed roster this block exists to abolish.

## Reading and writing

`agag.intro.roster_block` writes one, `agag.intro.parse_roster` reads one, and
the last block in a post wins so a quoted example in the prose cannot outrank
the real one. The block is fenced, so it is not prose a human has to skim past
and — like the `@**Comfy Notifier**` command quoted in a report — nothing
inside a fence can fire a mention.
