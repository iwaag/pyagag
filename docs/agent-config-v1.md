# Agent configuration and run-record contract v1

This document defines two language-neutral interchange contracts implemented by
`pyagag`:

- `ag.agent-config.v1` selects a harness and model for a project-owned role;
- `ag.agent-run.v1` records the resolved backend and factual run outcome.

Prompts, charters, tool grants, permissions, working directories,
orchestration, artifacts, HTTP, and success judgment remain application policy
and are outside these contracts.

## Configuration files

A project commits `agents.toml` and may load a git-ignored
`.local/agents.local.toml`. Both declare:

```toml
schema = "ag.agent-config.v1"
```

The committed file owns models, profiles, roles, and capabilities. The overlay
may only provide machine facts, secret references, extra capabilities, and
role-to-profile selection among profiles already committed. A missing overlay
is valid; a missing, unreadable, or wrongly versioned committed file is
`E_SCHEMA`.

### Committed shape

```toml
schema = "ag.agent-config.v1"
project = "example"

[models."ollama/example-model"]

[models."anthropic/example-model"]
effort = "low" # opaque model options are allowed

[profiles.local]
harness = "agcode"
model = "ollama/example-model"

[profiles.hosted]
harness = "claude_code"
model = "anthropic/example-model"

[profiles.stub]
harness = "fake"
model = "ollama/example-model"

[roles.generator]
profile = "local"
requires = ["workspace_fs"]

[capabilities]
provides = []
```

The optional `project` value is descriptive.

### v2: the role carries its tool grant

`schema = "ag.agent-config.v2"` is v1 plus one required key per role:

```toml
schema = "ag.agent-config.v2"

[roles.front]
profile = "hosted"
requires = []
allowed_tools = "Read,Glob,Grep,Bash(agentchat:*)"

[roles.generator]
profile = "hosted"
allowed_tools = ["Read", "Write", "Bash(ffmpeg:*)"]   # the same thing, as a list
```

`allowed_tools` is the harness's tool grant for that role — claude_code's
`--allowedTools` — written as the comma-separated string the harness takes or
as an array of patterns. It reaches the application as
`ResolvedAgent.allowed_tools` (joined with commas). A v2 role without it is
`E_SCHEMA`: every consumer of v1 kept a separate `ROLE_ALLOWED_TOOLS` table
with the same warning beside it — a role missing from the table gets no grant,
and claude_code then waits for an interactive permission answer until the
timeout — and v2 makes the table and the role list one thing. Under v1
`allowed_tools` is accepted when present and `ResolvedAgent.allowed_tools` is
`None` otherwise. The overlay cannot set it.

### Local overlay shape

```toml
schema = "ag.agent-config.v1"

[local.harness.claude_code]
command_glob = "~/.local/lib/claude-*/claude"

[local.provider.ollama]
base_url = "http://example.invalid:11434"

[local.secrets]
anthropic_api_key_file = "~/.secrets/anthropic"
# alternatively: anthropic_api_key_env = "ANTHROPIC_API_KEY_SOURCE"
google_api_key_file = "~/.secrets/gemini"    # becomes GEMINI_API_KEY for gemini_cli

[roles.generator]
profile = "hosted"

[capabilities]
provides = ["deployment_capability"]
```

Each `local.harness` entry sets exactly one of `command` or
`command_glob`; the newest glob match wins. Provider entries may only contain
`base_url`. Secret keys must end in `_file` or `_env`, and their values are
references, never secret values. Role overrides may only set `profile` and may
not introduce roles. The overlay cannot add models, profiles, harnesses, or
other top-level data.

## Resolution

Resolution follows this order:

1. Find the requested role in the committed `[roles]` table.
2. Select the explicit per-run profile override, then the overlay role profile,
   then the committed role profile.
3. Resolve that profile to its committed harness and model.
4. Validate model declaration and harness/provider compatibility.
5. Combine harness-intrinsic, committed, and overlay capabilities and verify
   every role requirement.
6. Resolve the harness command, provider endpoint, and supported secret
   references from local facts.
7. Return the resolved identity and subprocess environment without fallback.

Profiles name a `(harness, model)` pair. Roles are an open, project-owned set.
Applications may expose a per-run profile override, but the override must still
name a committed profile.

## Harnesses and models

The v1 harness vocabulary is closed:

| harness | command convention | provider compatibility | intrinsic capabilities |
|---|---|---|---|
| `claude_code` | `claude -p --output-format json --model <native-name>` | `anthropic` only | `agentic_tools`, `workspace_fs` |
| `agcode` | `python -m agag.agcode --model <native-name> [--base-url <endpoint>]` | any provider serving a Messages API endpoint | `agentic_tools`, `workspace_fs` |
| `gemini_cli` | `gemini -p "" -o json -m <native-name> --skip-trust --approval-mode <mode>` | `google` only | `agentic_tools`, `workspace_fs` |
| `agy` | `agy --input-format stream-json --output-format stream-json --model <native-name> --disable-slash-commands --add-dir <cwd> --print-timeout <N>s [--dangerously-skip-permissions]` | `antigravity` only | `agentic_tools`, `workspace_fs` |
| `codex` | `codex exec --json --skip-git-repo-check --ephemeral --color never -C <cwd> -m <native-name> [-c model_reasoning_effort="<effort>"] -s <sandbox> -` | `openai` only | `agentic_tools`, `workspace_fs` |
| `fake` | configured test executable | any | none |

Ollama is a provider, not a harness. `agcode` is the vocabulary's one direct
Messages-API caller: it is a harness because it runs a complete agentic loop —
tools over one working directory, a turn budget, a wall-clock deadline, results
normalized into `ag.agent-run.v1` — not because of the API it speaks. A single
model request is still not a harness.

`agcode` is this package's own module, so its resolved "executable" is a Python
interpreter: the command defaults to the interpreter running the resolver
(`sys.executable`), and `local.harness.agcode.command` overrides it with a
foreign one. It does not require `local.provider.ollama.base_url` — it defaults
to `http://localhost:11434` — but the endpoint is passed as `--base-url`
whenever one is resolved. Note the spelling: agcode posts to
`{base_url}/v1/messages`, so the declared endpoint is the bare base URL, with
no OpenAI-compatible `/v1` suffix.

`gemini_cli` drives Google's Gemini CLI headless, and differs from
`claude_code` in two ways a consumer should know. Permissions are an
**approval mode**, not a grant: headless `default` mode has nobody to answer
its prompt, so `run_harness()` always names one — `yolo` for
`skip_permissions`, whatever `--approval-mode` the caller put in
`extra_args` otherwise (a read-only role passes `plan`), and `yolo` when
neither says. The role's `allowed_tools` has no Gemini spelling and is not
passed. And **the exit code is not a failure signal**: an API failure prints
`"status": "error"` and exits 0, so failure is read from the result document
into `is_error`. The CLI prints token counts but no cost, and needs
`--skip-trust` because a workspace nobody trusted interactively is refused.
Its API key lives in the user's encrypted store, which an interactive shell
opens and a launchd-started run does not (exit 41, "you must specify the
GEMINI_API_KEY environment variable"); `local.secrets.google_api_key_file`
or `google_api_key_env` hands the key to the run as `GEMINI_API_KEY`, the
way the `anthropic_api_key_*` pair does for `ANTHROPIC_API_KEY`.

`agy` drives Google's **Antigravity CLI** headless. Its provider is
`antigravity`, not `google`: the catalog is the Antigravity account's
(`agy models` lists Gemini, Claude and GPT-OSS entries alike), the CLI
carries its own OAuth token, and no `local.secrets` entry feeds it. The
prompt goes in on stdin as one stream-json line — `-p` takes the prompt as
its *value* and reads nothing from stdin — so streamed and single-document
runs are the same process and every run leaves a real transcript; the
result is the `result` payload of the last `{"event": "result"}` line.
Three things a consumer should know. **cwd is not the workspace**: without
`--add-dir <cwd>` a file "in the current directory" lands in
`~/.gemini/antigravity-cli/scratch/`, so `run_harness()` always passes it.
**Headless mode auto-denies every tool that would prompt, reads included**
(there is no read-only door: `--mode plan` writes a plan file instead of
doing the work), so the bypass is the default — `skip_permissions`, or no
`--mode` of the caller's own in `extra_args`. And **a denial is not a
failure**: the CLI exits 0 with an empty response and `denied_actions`,
which the record keeps next to `empty_final`. `--print-timeout` is set just
under the run's timeout, because its 5-minute default would otherwise end a
long run early; an unknown model exits 1 with `status: ERROR` and the
catalog in `error`. Tokens are recorded, no cost. For a live-progress
consumer the `step_update` events are also delivered claude-shaped
(`assistant` events with `text`/`tool_use` blocks, the tool's `CommandLine`
doubled as `command`, `TargetFile` as `file_path`), after the raw event.

`codex` drives OpenAI's **Codex CLI** (`codex exec`) headless, on the
ChatGPT account the CLI is logged into; its provider is `openai`, the
CLI carries its own auth (`~/.codex/auth.json`), and no `local.secrets`
entry feeds it. The prompt goes in on stdin (`-` as the prompt argument),
so run_harness's stdin handoff works unchanged; `--json` is a flat, typed
JSONL stream, the same for a watched and an unwatched run, and every run
leaves a real transcript. What a consumer should know. **`exec` never
prompts**, so what a run may do is entirely its sandbox: `run_harness()`
passes `-s danger-full-access` for `skip_permissions` and when the caller
names nothing (`workspace-write` can neither commit — `.git` is protected —
nor reach the network, so it serves no role that commits or posts), and a
caller that names `-s`/`--sandbox` in `extra_args` keeps it — `read-only`
is a real read-only door for a role that only reads. The `allowed_tools`
grant has no Codex spelling and is not passed. **The reasoning effort is a
model option**: `[models."openai/gpt-5.6-terra"] effort = "low"` becomes
`-c model_reasoning_effort="low"`. **The answer is the last
`agent_message`** — the model narrates before acting, so the first one is
a preamble. **A sandbox denial is not a failure**: the run exits 0 and the
last message explains why the write did not happen. An unknown model exits
1 with `turn.failed`, whose message is the API's 400 JSON as a string;
`is_error` is set and `subtype` is `turn_failed`. `num_turns` is the number
of tool items (commands run + patches applied) — a unit of its own:
claude_code counts API turns, agy user messages. Tokens are recorded
(`cached_input_tokens` is most of every run), no cost. `--skip-git-repo-check`
is always on, because a cwd that is neither a git repository nor a trusted
project exits 1 before reading the prompt, and `--ephemeral` keeps a run
from persisting a session under `~/.codex/sessions/`. There is no timeout
flag; run_harness's kill is the deadline, and a killed capture has no
`turn.completed`. For a live-progress consumer the item events are also
delivered claude-shaped (`assistant` events with `text` blocks per
message, `tool_use` blocks named `shell` with `command` and `apply_patch`
with `path`), after the raw event.

The vocabulary has changed five times, and no change carries a compatibility
shim — silent fallback is what this contract forbids. `agcode` was **added**
after `claude_code` and `fake`; a pyagag older than that commit rejects
`harness = "agcode"` as `E_UNKNOWN_HARNESS`. `opencode` was later **removed**;
a pyagag newer than that commit rejects `harness = "opencode"` the same way.
`gemini_cli` was **added** after that, then `agy`, then `codex`, with the
same consequence for older pins. A consumer moves its pin and its profiles together.

A canonical model ID is `<provider>/<native-name>`. The provider matches
`[a-z0-9_-]+`; the native name is non-empty and contains no slash or
whitespace. Claude Code and agcode receive the native name after the first
slash; records keep the canonical ID either way.

## Stable errors

Implementations may vary message prose, but these codes and classes are stable:

| code | condition |
|---|---|
| `E_SCHEMA` | missing/wrong schema, malformed TOML, wrong table/list type, or missing committed file |
| `E_UNKNOWN_HARNESS` | harness is outside the v1 vocabulary |
| `E_BAD_MODEL_ID` | model ID is malformed or lacks its provider prefix |
| `E_UNKNOWN_MODEL` | profile references an undeclared model |
| `E_INCOMPATIBLE` | harness cannot serve the model provider |
| `E_UNKNOWN_PROFILE` | role, role selection, or per-run override is unknown |
| `E_OVERLAY_SCOPE` | overlay contains data outside its permitted scope |
| `E_SECRET_VALUE` | overlay secret entry is not a string reference |
| `E_CAPABILITY_UNMET` | resolved capabilities do not cover role requirements |
| `E_UNAVAILABLE` | selected executable, required endpoint, or referenced secret is unavailable |

Selection never silently falls back to another harness, model, or profile.

## Harness execution

`pyagag` exposes a non-raising process seam. `run_harness()` returns a
`HarnessResult` with:

| member | meaning |
|---|---|
| `output` | normalized agent text, or a factual failure description |
| `exit_code` | process exit code; `-1` for timeout, launch, or normalized failure with a zero process exit |
| `meta` | resolved identity, observed usage/timing, and `outcome` |
| `raw_output` | ANSI-stripped stdout retained for evidence |

Applications supply policy arguments such as allowed tools, extra arguments,
additional directories, permission mode, working directory, transcript path,
and timeout. The runner injects `ResolvedAgent.environment`, `NO_COLOR=1`, and
`AGENT_PROVIDER_<PROVIDER>_BASE_URL` when a provider URL exists.

`extra_args` are appended after the harness-specific flags and may not carry
`--model`/`-m`: model selection belongs to the resolved profile. Harness-specific
notes: agcode's `--task-input` and `--task-input-file` are mutually exclusive
(passing both is a usage error and exits 2), and an agcode `--deadline-s` should
stay *below* the `timeout` given to `run_harness()`, so agcode returns its own
structured result instead of being killed mid-run.

`meta["outcome"]` is `done`, `failed`, or `aborted`. Applications decide
whether to translate a non-done result into an exception or another local
control-flow shape. `run_harness()` derives the outcome from the process exit
and output, with one exception: when a harness reports `outcome: "aborted"`
itself — a run it ended on its own budget or deadline, as agcode does — that
distinction is kept rather than flattened into `failed`, matching how the
runner labels its own timeout.

A run that stops cleanly with no closing text is `done`, with an empty
`output` and `meta["empty_final"] = True`. It was once a failure, on the
reading that a run which says nothing achieved nothing; but what a run
achieved is read from what it *did* — the files it wrote, the flags it left —
and only the application knows which of those count. Weak models routinely
end a finished job with a thinking block and no text, and failing those runs
threw completed work away. The harness reports the fact; the application
decides what an empty answer means for its flow (a conversation may have
nothing to post, while a work run is judged by its report and flag).

## `ag.agent-run.v1`

Applications select the evidence path. The shared writer produces this JSON
shape:

```json
{
  "schema": "ag.agent-run.v1",
  "request_id": "application-owned-id",
  "role": "generator",
  "profile": "local",
  "harness": "agcode",
  "provider": "ollama",
  "model": "ollama/example-model",
  "duration_ms": 1234,
  "cost_usd": 0.01,
  "usage": {"input": 10, "output": 20},
  "num_turns": 1,
  "transcript": ".local/out/id.agent.jsonl",
  "outcome": "done"
}
```

`schema`, `request_id`, and `outcome` are required. Resolved identity fields
are expected after a successful resolution. Timing, cost, usage, turn count,
and transcript are included only when observed; values are never invented.
Failed records may include `failure`. A timeout uses `aborted`. A record
carries `empty_final: true` when the run left no closing text, and omits the
key otherwise.

## Implementations

Python applications consume `pyagag` through the `agag` import package.
agdevworld's `assistant/agent-config.mjs` is an independent JavaScript sibling
implementation of the same configuration and error-code contract; it is not a
consumer of this Python package. Cross-language conformance is defined by
equivalent accept/reject decisions and stable error codes, not by a shared
runtime API.
