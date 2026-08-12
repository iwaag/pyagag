# pyagag

`pyagag` is the Python implementation of the language-neutral
`ag.agent-config.v1` configuration contract and the `ag.agent-run.v1` harness
record convention. The import package is `agag`.

`run_harness()` keeps the subprocess's real working directory and inherited
`PWD` environment value aligned. This is the shared first defense against
harnesses that trust `PWD`; consumers may deliberately add a CLI-native
directory option as a second, tool-specific defense.

`agag.zulip` is the shared chat entrance: a stdlib-only Zulip bot client
(`ZulipClient.from_env(path)` over a `KEY=value` credentials file) and a
`serve(client, handler)` long-poll loop for direct messages. It carries the
receive-side mechanics that are easy to get wrong — identity lookup inside the
retry loop, unwrapped `RemoteDisconnected`, numeric user ids rather than email
addresses — so a consumer only writes its handler.

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
