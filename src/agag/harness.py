"""Common process seam for agent harnesses."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .agent_config import ResolvedAgent
from .agcode import max_tokens_from_options

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
DEFAULT_OUTPUT_TAIL_CHARS = 2000
#: The harnesses with a stream-json mode, and so a live-progress seam.
STREAMING_HARNESSES = ("claude_code", "agcode", "gemini_cli", "agy", "codex")


@dataclass
class HarnessResult:
    output: str
    exit_code: int
    meta: dict = field(default_factory=dict)
    raw_output: str = ""


def identity(agent: ResolvedAgent) -> dict:
    return {
        "role": agent.role,
        "profile": agent.profile,
        "harness": agent.harness,
        "provider": agent.provider,
        "model": agent.model,
    }


def build_argv(
    agent: ResolvedAgent,
    *,
    allowed_tools: str | None = None,
    add_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    skip_permissions: bool = False,
    stream: bool = False,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> list[str]:
    """The argv for one run. `cwd` and `timeout` are what run_harness already
    knows; `agy` reads both (its workspace and print deadline are flags, not
    inherited state), `codex` reads `cwd` (`-C`), the other harnesses ignore
    them."""
    extra_args = list(extra_args or [])
    if "--model" in extra_args or "-m" in extra_args:
        raise ValueError("model selection belongs to the resolved profile")
    if agent.harness == "claude_code":
        # claude_code refuses `-p --output-format stream-json` without
        # --verbose, so the two flags travel together.
        output = ["stream-json", "--verbose"] if stream else ["json"]
        argv = [agent.command, "-p", "--output-format", *output, "--model", agent.native_model]
        for directory in add_dirs or []:
            argv += ["--add-dir", directory]
        if allowed_tools:
            argv += ["--allowedTools", allowed_tools]
        argv += extra_args
        if skip_permissions:
            argv.append("--dangerously-skip-permissions")
        return argv
    if agent.harness == "agcode":
        # agcode is a module of this package: the resolved command is the
        # interpreter. The prompt arrives on stdin and the working directory
        # comes from cwd/PWD, so no directory flag is needed.
        argv = [agent.command, "-m", "agag.agcode", "--model", agent.native_model]
        if agent.provider_base_url:
            argv += ["--base-url", agent.provider_base_url]
        if stream:
            argv += ["--output-format", "stream-json"]
        if "--max-tokens" not in extra_args:
            argv += ["--max-tokens", str(max_tokens_from_options(agent.model_options))]
        return argv + extra_args
    if agent.harness == "gemini_cli":
        # `-p` is what leaves interactive mode; its argument is appended to
        # stdin, so an empty one hands the stdin prompt through untouched.
        # `--skip-trust`: an untrusted cwd otherwise exits 55 and forces the
        # approval mode back to `default`. Headless `default` has nobody to
        # answer the prompt, so the mode is always chosen here: the caller's
        # bypass is `yolo`, a caller that names a mode (a read-only role's
        # `plan`) keeps it, and `yolo` is the default otherwise — the
        # `allowed_tools` grant has no Gemini spelling and is not passed.
        output = "stream-json" if stream else "json"
        argv = [agent.command, "-p", "", "-o", output, "-m", agent.native_model, "--skip-trust"]
        if skip_permissions or "--approval-mode" not in extra_args:
            argv += ["--approval-mode", "yolo"]
        for directory in add_dirs or []:
            argv += ["--include-directories", directory]
        return argv + extra_args
    if agent.harness == "agy":
        # Antigravity CLI. The prompt travels on stdin as one stream-json
        # line (`_agy_stdin`): `-p` takes the prompt as its *value* and
        # nothing else reads stdin, so this is the one route that keeps
        # run_harness's stdin handoff — and every run then leaves a real
        # transcript, streamed or not (both modes produce this same argv).
        # cwd is NOT the workspace: without `--add-dir <cwd>` a "write e.txt
        # here" landed in `~/.gemini/antigravity-cli/scratch/` (2026-09-05).
        # `--print-timeout` defaults to 5m0s and would end a twenty-minute
        # run at five; it is set just under the caller's timeout so agy
        # reports its own deadline as a result document instead of being
        # killed. Headless mode auto-denies every tool that would prompt,
        # reads included, so the bypass is the default: the caller's
        # `skip_permissions`, or no `--mode` of its own in extra_args.
        argv = [
            agent.command, "--input-format", "stream-json", "--output-format", "stream-json",
            "--model", agent.native_model, "--disable-slash-commands",
        ]
        for directory in [str(cwd)] if cwd is not None else []:
            argv += ["--add-dir", directory]
        for directory in add_dirs or []:
            argv += ["--add-dir", directory]
        if timeout is not None:
            argv += ["--print-timeout", f"{max(1, int(timeout) - AGY_PRINT_TIMEOUT_MARGIN_S)}s"]
        if skip_permissions or "--mode" not in extra_args:
            argv.append("--dangerously-skip-permissions")
        return argv + extra_args
    if agent.harness == "codex":
        # OpenAI's Codex CLI, `codex exec`. `-` as the prompt reads stdin, so
        # run_harness's stdin handoff works unchanged (a 400 kB prompt was
        # accepted, 2026-09-05). `--json` is the flat JSONL stream, the same
        # for a watched and an unwatched run. `--skip-git-repo-check`: a cwd
        # that is neither a git repository nor a trusted project exits 1
        # before reading the prompt, and front's topic workspaces are not
        # repositories. `--ephemeral`: every run would otherwise persist a
        # session under `~/.codex/sessions/`. `exec` never prompts, so what a
        # run may do is entirely the sandbox: the caller's bypass is
        # `danger-full-access` (`workspace-write` can neither commit — `.git`
        # is protected — nor reach the network), a caller that names a
        # sandbox in extra_args (a read-only role's `read-only`) keeps it,
        # and full access is the default otherwise. The reasoning effort is
        # a config value, taken from the model's declared options
        # (`[models."openai/<name>"] effort = "low"`); the `-c` value is
        # parsed as TOML, hence the inner quotes. The `allowed_tools` grant
        # has no Codex spelling and is not passed.
        argv = [
            agent.command, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--color", "never",
        ]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        argv += ["-m", agent.native_model]
        effort = agent.model_options.get("effort")
        if isinstance(effort, str) and effort:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        if skip_permissions or not ({"-s", "--sandbox"} & set(extra_args)):
            argv += ["-s", "danger-full-access"]
        for directory in add_dirs or []:
            argv += ["--add-dir", directory]
        return argv + extra_args + ["-"]
    if agent.harness == "fake":
        return [agent.command, *extra_args]
    raise ValueError(f"unsupported harness: {agent.harness}")


#: agy's own print deadline sits this far under run_harness's timeout, so the
#: CLI ends the run with `error: "timeout waiting for response"` (exit 1, a
#: document) before the subprocess kill would leave a truncated stream.
AGY_PRINT_TIMEOUT_MARGIN_S = 10


def _agy_stdin(prompt: str) -> str:
    """The prompt as agy's stream-json input: one NDJSON line per turn."""
    return json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"


def _result_line(raw: str) -> dict | None:
    """The last `"type": "result"` JSON line of a stream-json capture, or None.

    Both harnesses end a streaming run with one such line carrying exactly the
    fields their single-document mode would have printed, so the extractors
    below work on either capture without being told which mode ran. A stream
    that died before its result line (timeout, kill) yields None and falls
    through to the raw-passthrough branch, and run_harness's normalization
    names the failure.
    """
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def _extract_claude(raw: str) -> tuple[str, dict]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        doc = _result_line(raw)
        if doc is None:
            return raw, {}
    if not isinstance(doc, dict):
        return raw, {}
    meta = {key: doc[key] for key in ("duration_ms", "num_turns", "is_error", "subtype") if key in doc}
    if isinstance(doc.get("total_cost_usd"), (int, float)):
        meta["cost_usd"] = doc["total_cost_usd"]
    if isinstance(doc.get("usage"), dict):
        meta["usage"] = doc["usage"]
    return doc.get("result") if isinstance(doc.get("result"), str) else "", meta


def _extract_gemini(raw: str) -> tuple[str, dict]:
    """Read gemini CLI's `-o json` document or its `-o stream-json` lines.

    The single document is `{"session_id", "response", "stats"}`; the stream
    ends with `{"type": "result", "status", "error"?, "stats"}` and carries
    the answer in earlier `{"type": "message", "role": "assistant"}` lines.
    Either way the exit code says nothing — an API failure printed
    `"status": "error"` and exited 0 (2026-09-04) — so failure is read from
    the document into `is_error`/`subtype`, which run_harness already names.
    Token counts become `usage`; the CLI prints no cost, so none is claimed.
    """
    try:
        doc = json.loads(raw)
        streamed = False
    except json.JSONDecodeError:
        doc = _result_line(raw)
        streamed = True
        if doc is None:
            return raw, {}
    if not isinstance(doc, dict):
        return raw, {}
    meta: dict = {}
    error = doc.get("error")
    if doc.get("status") == "error" or (isinstance(error, dict) and error):
        meta["is_error"] = True
        meta["subtype"] = (error or {}).get("type", "error") if isinstance(error, dict) else "error"
    else:
        meta["is_error"] = False
    stats = doc.get("stats") if isinstance(doc.get("stats"), dict) else {}
    usage = _gemini_usage(stats)
    if usage:
        meta["usage"] = usage
    turns = sum(
        model.get("api", {}).get("totalRequests", 0)
        for model in stats.get("models", {}).values()
        if isinstance(model, dict) and isinstance(model.get("api"), dict)
    )
    if turns:
        meta["num_turns"] = turns
    if streamed:
        output = _gemini_stream_text(raw)
    else:
        output = doc.get("response") if isinstance(doc.get("response"), str) else ""
    if meta["is_error"] and not output.strip() and isinstance(error, dict):
        output = str(error.get("message", ""))
    return output, meta


def _gemini_usage(stats: dict) -> dict:
    """Token counts as `usage`, whichever of the two stat spellings arrived.

    `-o json` nests them per model as `stats.models.<name>.tokens` with the
    keys `input`/`candidates`/`cached`/`thoughts`/`tool`; the stream's result
    line writes `input_tokens`/`output_tokens`/`cached` flat on `stats` (and
    again under `stats.models.<name>`). The model name is the one the CLI
    *used*, not the one asked for — a `-m gemini-2.5-flash` run reported
    `gemini-3.5-flash` — so nothing is looked up by name: the flat spelling
    is read when present, else the per-model tables are summed.
    """
    spellings = (
        ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
        ("cached_tokens", "cached_tokens"), ("cached", "cached_tokens"),
        ("thoughts_tokens", "thoughts_tokens"), ("tool_tokens", "tool_tokens"),
        ("input", "input_tokens"), ("candidates", "output_tokens"),
        ("thoughts", "thoughts_tokens"), ("tool", "tool_tokens"),
    )

    def read(table: dict) -> dict:
        found: dict = {}
        for key, spelled in spellings:
            value = table.get(key)
            if isinstance(value, (int, float)) and spelled not in found:
                found[spelled] = value
        return found

    usage = read(stats)
    if "input_tokens" in usage:
        return usage
    totals: dict = {}
    models = stats.get("models")
    for model in (models.values() if isinstance(models, dict) else ()):
        if not isinstance(model, dict):
            continue
        table = model.get("tokens") if isinstance(model.get("tokens"), dict) else model
        for key, value in read(table).items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _gemini_stream_text(raw: str) -> str:
    """The assistant's text from a stream-json capture: every
    `{"type": "message", "role": "assistant"}` line's `content`, joined —
    chunks marked `delta` run together, whole messages get a newline."""
    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "message":
            continue
        if event.get("role") != "assistant":
            continue
        content = event.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str):
            continue
        if event.get("delta") or not parts:
            parts.append(content)
        else:
            parts.append("\n" + content)
    return "".join(parts)


def _agy_result(raw: str) -> dict | None:
    """agy's result document: the `result` payload of the last
    `{"event": "result"}` stream line, or the single `-o json` document."""
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        doc = None
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("event") == "result":
                doc = event
                break
        if doc is None:
            return None
    if isinstance(doc, dict) and doc.get("event") == "result":
        doc = doc.get("result")
    return doc if isinstance(doc, dict) else None


def _extract_agy(raw: str) -> tuple[str, dict]:
    """Read agy's stream-json capture (or its `--output-format json` document).

    The stream is keyed by `event`, and the result payload is **nested**:
    `{"event": "result", "result": {"status": "SUCCESS"|"ERROR", "response",
    "error"?, "duration_seconds", "num_turns", "usage", "denied_actions"?}}`.
    `response` keeps its trailing newline, which is dropped once. A failure
    is `status: ERROR` (exit 1 too, but the document says why: an unknown
    model carries the whole catalog in `error`). A tool denial is the trap:
    **exit 0, empty response, `denied_actions`** — kept in meta, because
    without it that run is an unexplained `empty_final`. The CLI prints
    tokens and no cost (2026-09-05).
    """
    doc = _agy_result(raw)
    if doc is None:
        return raw, {}
    meta: dict = {}
    error = doc.get("error")
    if doc.get("status") == "ERROR" or (isinstance(error, str) and error):
        meta["is_error"] = True
        first = str(error or "").strip().splitlines()
        meta["subtype"] = first[0][:120] if first else "error"
    else:
        meta["is_error"] = False
    if isinstance(doc.get("duration_seconds"), (int, float)):
        meta["duration_ms"] = int(doc["duration_seconds"] * 1000)
    if isinstance(doc.get("num_turns"), int):
        meta["num_turns"] = doc["num_turns"]
    if isinstance(doc.get("usage"), dict):
        meta["usage"] = doc["usage"]
    if isinstance(doc.get("denied_actions"), list) and doc["denied_actions"]:
        meta["denied_actions"] = doc["denied_actions"]
    output = doc.get("response") if isinstance(doc.get("response"), str) else ""
    if output.endswith("\n"):
        output = output[:-1]
    if meta["is_error"] and not output.strip() and isinstance(error, str):
        output = error
    return output, meta


#: agy's tool parameter names → the detail keys a claude-shaped progress
#: reader looks for. All seen in the CLI's conversation store after one
#: workrun (2026-09-05): `CommandLine` (+`Cwd`) on run_command,
#: `AbsolutePath` on view_file, `TargetFile` on write_to_file,
#: `DirectoryPath` on list_dir, `SearchDirectory`/`Query` and
#: `SearchPath`/`Pattern` on the search tools, `Url` on read_url_content.
AGY_PARAMETER_KEYS = {
    "CommandLine": "command",
    "TargetFile": "file_path", "AbsolutePath": "file_path", "TargetPath": "file_path",
    "DirectoryPath": "path", "SearchDirectory": "path", "SearchPath": "path",
    "Query": "pattern", "Pattern": "pattern",
    "Url": "url",
}


def _agy_events(on_event: Callable[[dict], None]) -> Callable[[dict], None]:
    """Wrap a progress consumer so agy's `step_update` stream also arrives
    claude-shaped: `{"type": "assistant", "message": {"content": [...]}}`
    with `text` and `tool_use` blocks, which is what autolab's display reads.
    Every raw event is passed through first, so a consumer that knows agy's
    own spelling loses nothing. An `agent_response` step's `text_delta`
    chunks are gathered until the step is DONE and emitted as one `text`
    block; a `tool` step becomes one `tool_use` block the first time it is
    seen, with its parameters both as agy spells them and under the
    snake_case keys of `AGY_PARAMETER_KEYS`.
    """
    text_parts: dict[int, list[str]] = {}
    tools_seen: set[int] = set()

    def emit(block: dict) -> None:
        on_event({"type": "assistant", "message": {"role": "assistant", "content": [block]}})

    def wrapped(event: dict) -> None:
        on_event(event)
        if event.get("event") != "step_update":
            return
        step = event.get("step_update")
        if not isinstance(step, dict):
            return
        index = step.get("step_index", -1)
        kind = step.get("step_type")
        if kind == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str):
                text_parts.setdefault(index, []).append(delta)
            if step.get("state") in ("DONE", "ERROR"):
                text = "".join(text_parts.pop(index, []))
                if text.strip():
                    emit({"type": "text", "text": text})
        elif kind == "tool" and index not in tools_seen:
            tools_seen.add(index)
            info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
            parameters = info.get("parameters") if isinstance(info.get("parameters"), dict) else {}
            arguments = dict(parameters)
            for key, spelled in AGY_PARAMETER_KEYS.items():
                if key in parameters and spelled not in arguments:
                    arguments[spelled] = parameters[key]
            name = step.get("tool_name") or info.get("name") or "?"
            emit({"type": "tool_use", "name": str(name), "input": arguments})

    return wrapped


def _codex_lines(raw: str) -> list[dict]:
    """The JSON objects of a `codex exec --json` capture, in order."""
    events: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _extract_codex(raw: str) -> tuple[str, dict]:
    """Read a `codex exec --json` capture.

    The stream is flat and typed: `thread.started`, `turn.started`,
    `item.started`/`item.completed` with an `item` of type `agent_message`
    (`text`), `command_execution` (`command`, `aggregated_output`,
    `exit_code`, `status`), `file_change` (`changes: [{path, kind}]`),
    `reasoning` or `error` (`message`); then `turn.completed` with `usage`,
    or `error` + `turn.failed` (`error.message`, the API's 400 JSON as a
    string for an unknown model; exit 1 too). The answer is the **last**
    `agent_message`: the model narrates before acting, so the first one is
    a preamble. A sandbox denial is not a failure — exit 0, and the last
    message explains ("the workspace is read-only"). `num_turns` here is
    the number of tool items (commands run + patches applied), a unit of its
    own: claude_code counts API turns, agy user messages. No cost is printed
    (a ChatGPT account); `usage` keeps `cached_input_tokens`, which is most
    of every run (2026-09-05). A capture with no `turn.completed` and no
    `turn.failed` is a killed run, and run_harness's timeout branch names it.
    """
    events = _codex_lines(raw)
    if not events:
        return raw, {}
    meta: dict = {"is_error": False}
    output = ""
    tools = 0
    failure: str | None = None
    for event in events:
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if kind == "item.completed" and item:
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                output = item["text"]
            elif item.get("type") in ("command_execution", "file_change"):
                tools += 1
        elif kind == "turn.completed":
            if isinstance(event.get("usage"), dict):
                meta["usage"] = event["usage"]
        elif kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            failure = str(message) if message else (failure or "turn failed")
        elif kind == "error" and failure is None:
            message = event.get("message")
            failure = str(message) if message else "error"
    if failure is not None:
        meta["is_error"] = True
        meta["subtype"] = "turn_failed"
        if not output.strip():
            output = failure
    meta["num_turns"] = tools
    return output, meta


def _codex_events(on_event: Callable[[dict], None]) -> Callable[[dict], None]:
    """Wrap a progress consumer so codex's item stream also arrives
    claude-shaped: `{"type": "assistant", "message": {"content": [...]}}`
    with `text` and `tool_use` blocks, which is what autolab's display reads.
    Every raw event is passed through first. A completed `agent_message`
    becomes one `text` block; a `command_execution` becomes a `tool_use`
    named `shell` with `command` the first time its item id is seen (on
    `item.started`, so the display shows the command as it starts); a
    `file_change` a `tool_use` named `apply_patch` with `path` from the
    first change and the whole `changes` list. `reasoning` items are
    skipped.
    """
    tools_seen: set[str] = set()

    def emit(block: dict) -> None:
        on_event({"type": "assistant", "message": {"role": "assistant", "content": [block]}})

    def wrapped(event: dict) -> None:
        on_event(event)
        kind = event.get("type")
        if kind not in ("item.started", "item.completed"):
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        item_type = item.get("type")
        if item_type == "agent_message":
            if kind == "item.completed" and isinstance(item.get("text"), str) and item["text"].strip():
                emit({"type": "text", "text": item["text"]})
            return
        if item_type not in ("command_execution", "file_change"):
            return
        item_id = str(item.get("id", ""))
        if item_id in tools_seen:
            return
        tools_seen.add(item_id)
        if item_type == "command_execution":
            emit({"type": "tool_use", "name": "shell", "input": {"command": item.get("command", "")}})
        else:
            changes = item.get("changes") if isinstance(item.get("changes"), list) else []
            first = changes[0] if changes and isinstance(changes[0], dict) else {}
            emit({"type": "tool_use", "name": "apply_patch",
                  "input": {"path": first.get("path", ""), "changes": changes}})

    return wrapped


def _extract_agcode(raw: str) -> tuple[str, dict]:
    """Read agcode's single stdout JSON document.

    agcode already speaks the ag.agent-run.v1 field spellings, so extraction is
    one parse plus a fixed key copy; keys outside the record contract (run_id,
    truncated, malformed_tool_calls, failure_kind) stay out of the meta.
    Non-JSON stdout passes through as the output, as with _extract_claude.
    """
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        doc = _result_line(raw)
        if doc is None:
            return raw, {}
    if not isinstance(doc, dict):
        return raw, {}
    meta = {
        key: doc[key]
        for key in (
            "duration_ms", "num_turns", "usage", "outcome", "failure", "transcript",
            # Not a §9 field, but the one signal that explains an otherwise
            # baffling run: a coding run whose answer was a cut-off preamble
            # took a live reproduction to diagnose because this was dropped.
            "truncated",
        )
        if key in doc
    }
    return doc.get("output") if isinstance(doc.get("output"), str) else "", meta


def _run_streaming(
    argv: list[str],
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    env: dict[str, str],
    on_event: Callable[[dict], None],
) -> tuple[int, str, str]:
    """Launch one harness process and forward its stdout JSON lines as events.

    Mirrors ``subprocess.run(capture_output=True, timeout=...)``: returns
    ``(returncode, stdout, stderr, consumer_errors)``, raises ``subprocess.TimeoutExpired``
    carrying the stdout captured so far, and lets ``OSError`` from the launch
    propagate. Each stdout line that parses as a JSON object reaches
    ``on_event`` the moment it arrives (on the reader thread); a consumer
    that raises does not kill the run — progress is telemetry — but its
    first complaint is returned rather than swallowed, so a display that
    silently stopped working leaves a trace.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
    )
    out_lines: list[str] = []
    err_chunks: list[str] = []
    consumer_errors: list[str] = []

    def pump_stdout() -> None:
        for line in proc.stdout:
            out_lines.append(line)
            stripped = ANSI_RE.sub("", line).strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                try:
                    on_event(event)
                except Exception as error:  # noqa: BLE001 - see docstring
                    consumer_errors.append(f"{type(error).__name__}: {error}")

    def drain_stderr() -> None:
        err_chunks.append(proc.stderr.read())

    # Both pipes get their own reader so neither can fill its buffer and
    # deadlock the child while the parent waits on the other.
    threads = [
        threading.Thread(target=pump_stdout, daemon=True),
        threading.Thread(target=drain_stderr, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except OSError:
        pass  # the process died before reading its prompt; its exit says why
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        for thread in threads:
            thread.join(timeout=5)
        raise subprocess.TimeoutExpired(argv, timeout, output="".join(out_lines)) from None
    for thread in threads:
        thread.join(timeout=5)
    return proc.returncode, "".join(out_lines), "".join(err_chunks), consumer_errors


def run_harness(
    agent: ResolvedAgent,
    prompt: str,
    *,
    cwd: Path,
    timeout: float,
    allowed_tools: str | None = None,
    add_dirs: list[str] | None = None,
    extra_args: list[str] | None = None,
    skip_permissions: bool = False,
    on_event: Callable[[dict], None] | None = None,
    stream: bool = False,
    transcript_path: Path | None = None,
    output_tail_chars: int = DEFAULT_OUTPUT_TAIL_CHARS,
) -> HarnessResult:
    """Launch, extract, and normalize one harness process without fallback.

    ``on_event`` switches the streaming harnesses to their stream-json modes
    and receives each event dict as the run produces it — the live-progress
    seam. The extracted result is identical either way (the stream's final
    ``"type": "result"`` line carries the single-document fields), and the
    ``fake`` harness, which has no stream mode, simply runs without events.

    ``stream`` asks for that same mode with nobody watching, which is what a
    caller wants when the *record* is the point: without it ``-p`` answers
    with one result document and ``transcript_path`` captures a cost report
    rather than a run. `agent_standardize` p10 found that out the hard way —
    an entrance answered without looking at a whole project, and the
    transcript kept for exactly that question could not say so.
    """
    meta = identity(agent)
    if timeout <= 0:
        return HarnessResult(
            "agent run timed out (no budget left)", -1,
            {**meta, "outcome": "aborted", "failure": "timeout"},
        )
    stream = (on_event is not None or stream) and agent.harness in STREAMING_HARNESSES
    try:
        argv = build_argv(
            agent,
            allowed_tools=allowed_tools,
            add_dirs=add_dirs,
            extra_args=extra_args,
            skip_permissions=skip_permissions,
            stream=stream,
            cwd=cwd,
            timeout=timeout,
        )
    except ValueError as error:
        return HarnessResult(str(error), -1, {**meta, "outcome": "failed", "failure": str(error)})
    env = {**os.environ, **agent.environment, "NO_COLOR": "1"}
    # Working-directory defense 1/2: subprocess(cwd=...) changes the real cwd
    # but does not update an inherited PWD. Some harness CLIs trust PWD, so
    # keep both views consistent. Consumers may also pass a CLI-native
    # directory flag as a second, harness-specific defense.
    env["PWD"] = str(cwd.resolve())
    if agent.provider_base_url:
        env[f"AGENT_PROVIDER_{agent.provider.upper()}_BASE_URL"] = agent.provider_base_url
    started = time.monotonic()
    consumer_errors: list[str] = []
    stdin_payload = _agy_stdin(prompt) if agent.harness == "agy" else prompt
    consumer = on_event if on_event is not None else (lambda event: None)
    if agent.harness == "agy" and on_event is not None:
        consumer = _agy_events(on_event)
    elif agent.harness == "codex" and on_event is not None:
        consumer = _codex_events(on_event)
    try:
        if stream:
            returncode, stdout, stderr, consumer_errors = _run_streaming(
                argv, stdin_payload, cwd=cwd, timeout=timeout, env=env, on_event=consumer,
            )
        else:
            proc = subprocess.run(
                argv,
                input=stdin_payload,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
            returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as error:
        raw = (
            error.stdout if isinstance(error.stdout, str)
            else error.stdout.decode(errors="replace") if isinstance(error.stdout, bytes)
            else ""
        )
        if transcript_path and raw:
            transcript_path.parent.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(raw, encoding="utf-8")
            meta["transcript"] = str(transcript_path)
        failure = f"{agent.harness} timed out after {int(timeout)}s"
        return HarnessResult(
            failure,
            -1,
            {
                **meta,
                "outcome": "aborted",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "failure": failure,
            },
            raw,
        )
    except OSError as error:
        failure = f"could not launch {agent.harness} ({argv[0]}): {error}"
        return HarnessResult(
            failure,
            -1,
            {
                **meta,
                "outcome": "failed",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "failure": failure,
            },
        )
    raw = ANSI_RE.sub("", stdout or "")
    if transcript_path:
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(raw, encoding="utf-8")
        meta["transcript"] = str(transcript_path)
    if agent.harness == "claude_code":
        output, reported = _extract_claude(raw)
    elif agent.harness == "agcode":
        output, reported = _extract_agcode(raw)
    elif agent.harness == "gemini_cli":
        output, reported = _extract_gemini(raw)
    elif agent.harness == "agy":
        output, reported = _extract_agy(raw)
    elif agent.harness == "codex":
        output, reported = _extract_codex(raw)
    else:
        # `fake`: whatever the stub printed is the output, minus the trailing
        # newline `print` adds, and no statistics of its own. The extractor
        # this replaced parsed an event stream; a stub has none to parse.
        output, reported = raw.rstrip("\n"), {}
    meta.update(reported)
    meta.setdefault("duration_ms", int((time.monotonic() - started) * 1000))
    stderr_tail = ANSI_RE.sub("", stderr or "").strip()[-output_tail_chars:]
    failure = None
    reported_failure = reported.get("failure") if isinstance(reported.get("failure"), str) else None
    if returncode != 0:
        # A harness that reports its own failure string keeps it when it had no
        # output to quote — agcode's is kind-prefixed and worth more than "no
        # output".
        tail = output.strip()[-output_tail_chars:] or reported_failure or "no output"
        failure = f"{agent.harness} exited {returncode}: {tail}"
        if stderr_tail:
            failure += f"; stderr tail: {stderr_tail}"
    elif meta.get("is_error"):
        tail = output.strip()[-output_tail_chars:] or "no output"
        failure = f"{agent.harness} reported an error ({meta.get('subtype')}): {tail}"
    # A clean exit with no output is reported, not failed: a run's achievement
    # is its files and flags, which only the caller can weigh, and a harness
    # that fails such a run throws finished work away. `empty_final` is that
    # report; the caller decides what an empty answer means for its flow.
    if failure is None and not output.strip():
        meta["empty_final"] = True
        # An empty run's stderr is the only trace of what happened inside it;
        # keep it as evidence for the caller instead of discarding it with the
        # process.
        if stderr_tail:
            meta["stderr_tail"] = stderr_tail
    # A progress display that quietly stopped working leaves this behind. It
    # says nothing about the run itself, which is why it is not a failure.
    if consumer_errors:
        meta["event_consumer_error"] = (
            f"{len(consumer_errors)} event(s) not consumed; first: {consumer_errors[0]}"
        )
    meta["outcome"] = "failed" if failure else "done"
    # A harness that ended itself on a budget or deadline reports "aborted";
    # that stays, because run_harness spells its own timeout the same way. Only
    # the aborted/failed distinction is preserved — the failure text above is
    # still recomposed by this function, per the harness-result contract.
    if failure and reported.get("outcome") == "aborted":
        meta["outcome"] = "aborted"
    if failure:
        meta["failure"] = failure
    exit_code = returncode if failure is None else (returncode or -1)
    return HarnessResult(output if output.strip() else (failure or ""), exit_code, meta, raw)


def write_run_record(
    path: Path,
    *,
    request_id: str,
    meta: dict,
    extra_meta: dict | None = None,
    outcome: str | None = None,
    failure: str | None = None,
) -> Path:
    """Write a normalized ag.agent-run.v1 record to an app-selected path."""
    record = {"schema": "ag.agent-run.v1", "request_id": request_id}
    for key in (
        "role", "profile", "harness", "provider", "model", "duration_ms",
        "cost_usd", "usage", "num_turns", "transcript",
    ):
        if key in meta:
            record[key] = meta[key]
    # Not §9 fields, and absent on the runs they do not describe: a run that
    # said nothing, or one whose responses were cut off at the token limit,
    # is exactly what a reader of a puzzling record needs to see.
    for key in ("empty_final", "truncated"):
        if meta.get(key):
            record[key] = True
    # agy: a headless tool denial exits 0 with an empty answer; this is the
    # only thing that tells the reader why the run said nothing.
    if meta.get("denied_actions"):
        record["denied_actions"] = meta["denied_actions"]
    record["outcome"] = outcome or meta.get("outcome", "failed")
    failure = failure or meta.get("failure")
    if failure:
        record["failure"] = failure
    if extra_meta:
        record.update(extra_meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
