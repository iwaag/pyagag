# pyagag

`pyagag` is the Python implementation of the language-neutral
`ag.agent-config.v1` configuration contract and the `ag.agent-run.v1` harness
record convention. The import package is `agag`.

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
