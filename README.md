# pyagag

`pyagag` is the Python implementation of the language-neutral
`ag.agent-config.v1` configuration contract and the `ag.agent-run.v1` harness
record convention. The import package is `agag`.

`run_harness()` drives one harness process per run — `opencode`, `claude_code`,
`agcode`, or the test-only `fake` — and normalizes its result. It keeps the
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
pyagag older than the commit that added it; adopting a profile means upgrading
the pin first.

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

## Development

```sh
uv sync
uv run pytest
```

Consumers in the sibling workspace use editable uv path dependencies. Those
lockfiles intentionally assume the sibling checkout keeps the same relative
layout.

See [docs/agent-config-v1.md](docs/agent-config-v1.md) for the language-neutral
configuration, resolution, harness-result, and run-record contracts.
