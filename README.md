# pyagag

`pyagag` is the Python implementation of the language-neutral
`ag.agent-config.v1` configuration contract and the `ag.agent-run.v1` harness
record convention. The import package is `agag`.

`run_harness()` drives one harness process per run — `claude_code`, `agcode`,
or the test-only `fake` — and normalizes its result. It keeps the
subprocess's real working directory and inherited `PWD` environment value
aligned. This is the shared first defense against harnesses that trust `PWD`;
consumers may deliberately add a CLI-native directory option as a second,
tool-specific defense.

`agag.agcode` is the only harness that ships inside this package: a single
stdlib-only file running an agentic loop over the Anthropic Messages API
(local ollama serves one, so it needs no account and no key). It exists because
harnesses that show the model two candidate base directories, and ask it to
absolutize paths itself, make weak models resolve relative paths against the
wrong base a sizable fraction of the time. agcode names exactly one working
directory, resolves every path tool-side, reads nothing from the invoking
user's home, and can write a verbatim wire transcript of every request and
response. Use it in a profile as `harness = "agcode"`, or directly:

```sh
echo "your task" | python -m agag.agcode --model qwen3.6:35b-a3b-coding-nvfp4
```

Note that `harness = "agcode"` is rejected as `E_UNKNOWN_HARNESS` by any
pyagag older than the commit that added it, and `harness = "opencode"` — which
this package used to drive — is rejected by any pyagag newer than the commit
that removed it. Either way the fix is to move the pin and the profiles
together; there is no compatibility shim.

### Calling agcode in-process

`agcode.run()` is importable, and three keyword arguments let a consumer host
an agent without a config file, a permission engine, or an MCP server. The CLI
uses none of them, so `python -m agag.agcode` behaves exactly as documented
above.

```python
from agag import agcode

def nctl(base, subcommand):                      # base arrives first, always
    return agcode.tool_run(base, f"uv run nctl {subcommand}")

result = agcode.run(
    task,
    working_dir,
    model="qwen3.6:35b-a3b-coding-nvfp4",
    tools=[*agcode.READONLY_TOOLS, agcode.Tool(NCTL_SPEC, nctl)],
    system_suffix=Path("AGENTS.md").read_text(),
    stop=cancelled.is_set,
)
```

- `tools=` is the offered tool set: each `Tool` pairs a JSON spec with the
  callable that serves it, invoked as `func(base, **arguments)` and returning
  the tool_result string. It defaults to `agcode.DEFAULT_TOOLS`, the four
  built-ins. **The tool set is the permission surface** — `READONLY_TOOLS`
  (`read` + `list`) offers a door no way to write, rather than denying calls it
  offered anyway. There is nothing forbidden for a weak model to attempt.
  The two presets are also reachable across the subprocess boundary as
  `--tools default` (the CLI default) and `--tools read-only`, so a
  `run_harness()` caller can pick a door's tool set through `extra_args`.
- `system_suffix=` appends per-role instructions to the pinned system prompt.
  Read it from disk per run and editing the file takes effect on the next
  request. The working-directory sentence stays first and unconditional.
- `stop=` is a zero-argument predicate checked between turns; returning true
  ends the run as `aborted` with failure kind `cancelled`, keeping the usage
  and turn count accumulated so far. An in-flight turn always finishes.

`agag.zulip` is the shared chat entrance: a stdlib-only Zulip bot client
(`ZulipClient.from_env(path)` over a `KEY=value` credentials file) and a
`serve(client, handler)` long-poll loop for direct messages. It carries the
receive-side mechanics that are easy to get wrong — identity lookup inside the
retry loop, unwrapped `RemoteDisconnected`, numeric user ids rather than email
addresses — so a consumer only writes its handler.

`topic_dump(channel, topic, chatlog)` preserves numbered topic snapshots under
the caller's ignored `.local/topics/` tree. `topic_write(topic, text)` is the
matching outbound convenience function; a listener can inject its client and
channel, while standalone callers use `ZULIP_ENV` and `ZULIP_CHANNEL`.

`agag.chat` is the same entrance from the *agent's* side, exposed as the
`agentchat` console script: `agentchat topics <channel>`,
`agentchat read <channel> <topic>` and
`agentchat send <channel> <topic> <text…>`. A listener speaks Zulip on the
harness's behalf; this is what an agentic run itself calls when it decides to
ask another agent something. Identity is the credentials file that
`AGENTCHAT_ZULIP_ENV` names — whoever's file it is, is who speaks — and it
never touches subscriptions, because a bot may post into and read any public
channel unsubscribed and an agent's own subscriptions are its listener's
routing decision. `agentchat --help` is the tool's documentation and is
written as a usage document, so an agent handed the command can learn it
without being told anything else. Its examples name no real channel or topic
prefix on purpose: where to write is what the addressed agent's own
introduction says, and an example a caller can copy would quietly become the
source of that knowledge.

### Project channels: subscription is the routing decision

A project channel (`#pj-<name>` in the agag realm) is a room, and **who is
subscribed to it is who is being asked to do the work**. Whoever creates the
project makes that choice by subscribing the accounts it wants: this agent
rather than that one, this node's instance rather than another node's. There is
no second routing layer underneath it — for the topic kinds that carry work,
being in the room *is* the assignment.

Two rules follow, and they are the whole convention:

- **A listener never widens its own subscriptions.** No reconciliation loop, no
  subscribing itself to channels it discovers, no subscribing anyone else.
  Zulip delivers no events for an unsubscribed channel, which makes
  subscription a boundary a listener cannot accidentally cross — but only for
  as long as listeners leave it alone. A listener that reconciles subscriptions
  erases the creator's decision, silently and every time it runs.
- **Give each instance its own bot account.** An account is what makes an agent
  addressable and subscribable, so one account per running instance — not one
  per kind of agent — is what makes the choice expressible at all.

Agents of *different kinds* can share a room safely, because they are separated
by disjoint topic prefixes: each kind acts only on the prefixes it owns, and a
mention gate breaks the loop where two kinds would otherwise answer each other
forever. That separation does not extend to two instances of the *same* kind:
they own the same prefixes, and the last-poster rule that keeps one listener
from answering itself does not keep it from answering its twin — each one's
post re-arms the other. So put two same-kind instances in one project channel
only when their topics carry an explicit addressee; otherwise the creator picks
one instance per project channel, which is the point of picking at all.

### Instance identity and the introduction board

Two small modules carry the standardized shape of "an agent that can be found
and asked". `agag.instance.instance_name()` reads an instance's own name from
its local `.local/instance.toml` — the name its Zulip account, its own channel
and its `intro-<name>` topic all agree on — falling back to the plain agent
name when there is no file, and letting an optional environment variable win so
a second instance on one host needs no second checkout. The file is local-only
because the instance label carries host information.

`agag.intro.post_intro()` appends that instance's committed introduction to the
shared `agents` channel, in the append-only `intro-<instance>` topic, stamped
with the date and the repository's short revision:

```python
from agag.instance import instance_name
from agag.intro import post_intro

name = instance_name(ROOT / ".local" / "instance.toml", fallback="autolab")
post_intro(client, instance=name, intro_path=ROOT / "params" / "intro.md", root=ROOT)
```

Nothing deduplicates: re-post after a behavior change and the newest
introduction is simply the newest message. **The introduction is the contract.**
It is what another agent reads to learn this one's entrance, which is why that
knowledge travels as posted content rather than as vocabulary compiled into
someone else's guide.

## Development

```sh
uv sync
uv run pytest
```

Consumers in the sibling workspace resolve pyagag from GitHub
(`[tool.uv.sources] pyagag = { git = ..., branch = "main" }`), so a change here
reaches them only after it is pushed and they run
`uv lock --upgrade-package pyagag`.

See [docs/agent-config-v1.md](docs/agent-config-v1.md) for the language-neutral
configuration, resolution, harness-result, and run-record contracts.
