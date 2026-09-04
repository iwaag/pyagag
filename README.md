# pyagag

`pyagag` is the Python implementation of the language-neutral
`ag.agent-config.v1` configuration contract and the `ag.agent-run.v1` harness
record convention. The import package is `agag`.

`run_harness()` drives one harness process per run — `claude_code`, `agcode`,
`gemini_cli`, or the test-only `fake` — and normalizes its result. It keeps the
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
`agentchat` console script: `agentchat channels [--prefix <p>]`,
`agentchat topics <channel>`, `agentchat read <channel> <topic>`,
`agentchat send <channel> <topic> <text…>` and
`agentchat resolve <channel> <topic>`. A listener speaks Zulip on the
harness's behalf; this is what an agentic run itself calls when it decides to
ask another agent something. Identity is the credentials file that
`AGENTCHAT_ZULIP_ENV` names — whoever's file it is, is who speaks. Reading
touches no subscriptions, because a bot may read any public channel
unsubscribed. `read --since <message-id>` is how a conversation is followed
one step at a time, and it follows a topic across Zulip's resolve rename
(`✔ <topic>`) so a close-out is not what makes a reader lose sight of it.

`channels` prints `<name> — <description>` per line: the description is the
half worth having, because a channel derived from one piece of work is where
that work is named in a sentence a person wrote. Nothing parses it.
`resolve` is the other side of the rename — it takes the name the caller
knows, reports an already-resolved topic instead of touching it, and moves
the whole topic through its last message. A bot may resolve a topic another
bot opened on this realm (checked live in `agent_standardize` p10); unlike
archiving, it needs no creator or admin right.

`run_harness(stream=True)` asks for the stream-json mode with nobody
watching, which is what a caller wants when the *record* is the point:
without it `-p` answers with one result document, and `transcript_path`
captures a cost report rather than a run. `on_event` still implies it.

### A run is one reply; waiting is just not being your turn

There is no `wait`. An agent does one piece of work, says something, and its
run ends; when a post addressed to it arrives it is served again, with that
conversation in front of it. Three pieces carry that:

- **Posting is participating, and the chat remembers it.** `agentchat send`
  subscribes the sender to the channel it posts into — only a subscribed
  channel's messages reach the event stream — and, once per topic and before
  the first real post, writes a root note into it:

      [selfnote][rootchat] <channel>/<topic>

  naming the conversation this run is serving (`AGENTCHAT_HOME`). That note
  is the whole memory: which of its own conversations this agent is here on
  behalf of, kept where the conversation is rather than in a file the agent
  has to hold. String operations and one post; no model is involved.
- **Selfnotes are machine-to-machine.** `agag.selfnote` is the convention: a
  message whose content starts with `[selfnote]` is hidden from every
  rendered `chatlog.md`, every `threads/` file and `agentchat read` unless
  `--all` asks for it — from its own author too, because an agent that reads
  its own notes starts composing them. **And a selfnote is never somebody
  speaking**: every "who spoke last" check — the sweep, the event path, the
  mention test, `serve_topic`'s post-run re-check — goes through
  `last_real_sender`. Miss one and a note an agent wrote to itself buys the
  other agent a run, which is the ack loop of `agent_standardize` p7 in a
  new coat.
- **Two triggers, not one.** `sweep_serve(..., on_mention=…)` serves the
  *owner* of a topic on anybody else's post in it, and a *participant* only
  when a post names it. Mentions come off the event stream's `mentioned`
  flag and are recovered at startup through Zulip's `is:mentioned` narrow and
  through `sweep_rootchats` — the `sender:me search:rootchat` narrow, which
  lists every topic this agent anchored and asks which of them is waiting on
  it. So a mention that arrived while the listener was down is no more lost
  than a swept topic is.
- **And a callback answered once is not answered again.** After serving a
  callback the listener calls `note_served`, which writes into *home*:

      [selfnote][served] <channel>/<topic> <message id>

  Both sides of that follow Zulip's resolve rename — the post that names an
  agent is very often the post that ends the conversation, and a lookup that
  cannot see past the `✔ ` reads an empty topic and drops the callback.

  Recovery needs it because the reply goes home: this agent never becomes the
  last poster in the topic that named it, so "somebody else spoke there and
  named me" is true forever and every restart would re-serve every exchange
  the agent ever had. **Both recovery routes** consult it —
  `sweep_rootchats` and `sweep_mentions` alike — skipping a topic whose newest
  naming post is at or below its mark and serving it the moment a newer one
  arrives. The mention route needs it for the same reason: it used to silence
  itself, because answering a mention meant posting where it was made.
- **The turn is handed over mechanically.** `serve_topic` prefixes every
  reply with `@**<name>**` of the last other speaker in the topic it is
  replying into, and `reply_to` lets a run brought back by a mention work on
  its own task while answering where the question was asked. No guide has to
  ask an agent to address the requester; the code does it.

A serving that answers in somebody else's topic posts **once**, and posts no
ack. An ack is how a bot's own sweep skips a topic it is already serving; in
a topic it does not own it buys nothing and costs the owner a whole serving —
triggered by "Message received", against a conversation that does not yet
hold the reply being acknowledged.

`agentchat --help` is the tool's documentation and is
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

`{instance}` inside the Markdown is replaced with the instance's name as it is
posted, so the tracked file carries no host label and one introduction serves
every instance of that agent.

Nothing deduplicates: re-post after a behavior change and the newest
introduction is simply the newest message. **The introduction is the contract.**
It is what another agent reads to learn this one's entrance, which is why that
knowledge travels as posted content rather than as vocabulary compiled into
someone else's guide.

### The agent skeleton and `agag init`

`agag.agent` is what every standardized agent used to carry as five copied
modules: the instance name, the introduction, the role run with its
`agentchat` handover, the pull-sweep listener and the entrance serving. An
agent is now an `AgentSpec` plus its `agents.toml`, its guides and its own
topic handlers:

```python
from agag.agent import AgentSpec, listener_main

SPEC = AgentSpec("agecho", ROOT, plan_prefix="agechoplan-", run_prefix="agechorun-")
listener_main(SPEC, {"agechoplan-": handle_plan})   # anything else → agag.entrance
```

`run_role(SPEC, role, prompt, …)` resolves the role against
`<root>/agents.toml` (+ `.local/agents.local.toml`), puts `agentchat` on PATH
with `AGENTCHAT_ZULIP_ENV` = `<root>/.local/zulip.env` and `AGENTCHAT_HOME` =
the conversation served, and passes the role's own `allowed_tools` grant
(`ag.agent-config.v2`). `agag.entrance.handle_entrance(SPEC, …)` answers a
plain topic in the instance's own channel with a `front` run; the guide is the
agent's `agent/guides/entrance_front/guide.md` when it has one, else a built-in
default naming its `plan_prefix`/`run_prefix`. `intro_main(SPEC)` posts
`params/intro.md`.

`agag init <agent>` generates a project on that skeleton: `pyproject.toml`
(pyagag from GitHub), `agents.toml` (v2, one grant per role),
`params/intro.md`, one guide stub, `src/<agent>/listener.py` (the spec and
one `listener_main` call), `src/<agent>/intro.py`, `service/listen.sh`,
`params/channel.md`, `.gitignore`, `.local/instance.toml` — files only, no `git init`; where the
project lives in version control is the caller's. It asks for the
instance name, the two prefixes, the roles, the profile and the destination
(`--yes` takes every default). `--like <sibling-root>` copies that instance's
local machine overlay while dropping role overrides the new agent does not
declare, and `--provision` immediately runs the Zulip setup.

`agag provision [root]` uses the owner-class credentials whose **path** is in
`AGAG_ZULIP_ADMIN_ENV` (or `--admin-env`). It creates a generic bot, writes
`<root>/.local/zulip.env` with mode 0600, subscribes the bot to `#agents`, and
creates or updates the instance's own channel from `params/channel.md`. That
channel is subscribed by the realm's organization owners — the humans who
watch the new agent, never the provisioner account itself — and filed in the
`agents` channel folder, which is minted if the realm has none and which a
channel that already existed unfiled is moved into (`--folder` names another;
`--no-folder` leaves it where it is). It
refuses when the bot email already exists rather than regenerating a running
bot's key. The remaining human checklist is the once-per-realm provisioner
account, an optional per-agent Plane account, and permanent listener service
installation. `agag init --help` and `agag provision --help` have the flags.

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
