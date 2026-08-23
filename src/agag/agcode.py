"""agcode — a minimal single-file agent harness over the Anthropic Messages API.

Design principles:

1. One base: the harness names exactly one working directory everywhere.
2. Tools resolve paths; the model never absolutizes.
3. Wire-honest: every request/response can be captured verbatim.
4. No ambient identity: nothing is read from the invoking user's home.
5. Stdlib only.

Principles 1 and 2 exist because harnesses that offer the model two candidate
base directories, and ask it to absolutize paths itself, make weak models
resolve relative paths against the wrong base a sizable fraction of the time.
One named base and tool-side resolution remove the choice.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MAX_TURNS = 20
DEFAULT_DEADLINE_S = 900.0
# The response token cap. Sized for a thinking model that writes files: a
# 35B local model's reasoning plus one whole-file write overran the old
# 4096 routinely, and every overrun costs a turn and a re-read of the
# grown context to recover from. Backends cap it at what they can serve.
DEFAULT_MAX_TOKENS = 32768
ANTHROPIC_VERSION = "2023-06-01"
DUMMY_API_KEY = "agcode-local"  # sent because the API shape wants it; never validated


def max_tokens_from_options(options: dict[str, Any]) -> int:
    """Return the model-configured response cap, or the process default.

    ``agents.toml`` model tables deliberately carry provider-specific options.
    Keep the validation at the agcode boundary so both its subprocess and
    in-process callers accept the same safe spelling.
    """
    value = options.get("max_tokens", DEFAULT_MAX_TOKENS)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("model option max_tokens must be a positive integer")
    return value


# Machine-readable failure vocabulary (P3). Every non-done run carries exactly
# one of these in ``meta["failure_kind"]``, beside the human-readable
# ``meta["failure"]`` string. Mirrors pyagag run_harness(): budget/deadline end
# as outcome "aborted", backend/model misbehavior as "failed".
#
# A clean stop with no final text is not among them. It used to be
# ("empty_output"), on the reading that a run which says nothing achieved
# nothing — but what a run achieved is read from what it *did* (the files it
# wrote, the flags it left), which only the caller can judge. Local models
# routinely end a finished job with a thinking block and no text, and failing
# those runs threw away completed work. The fact is reported instead, as
# ``meta["empty_final"]``.
FAILURE_KINDS = frozenset(
    {
        "deadline_exceeded",  # aborted: wall-clock deadline hit
        "turn_budget_exhausted",  # aborted: max_turns hit
        "cancelled",  # aborted: the caller's stop callable asked to end the run
        "connect_error",  # failed: connection refused / HTTP error / timeout
        "malformed_response",  # failed: non-JSON body, missing content, tool_use stop without tool_use blocks
    }
)


def _is_timeout(exc: BaseException) -> bool:
    """Whether an urllib failure is a timeout — directly (a read timeout
    surfaces as ``TimeoutError``) or wrapped (a connect timeout arrives as
    ``URLError`` with the timeout as its reason)."""
    return isinstance(exc, TimeoutError) or isinstance(
        getattr(exc, "reason", None), TimeoutError
    )


class MessagesError(Exception):
    """HTTP-level failure from the Messages endpoint, with the response body."""

    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


# --- Tool layer -------------------------------------------------------------
#
# One base directory. Tools accept relative paths verbatim and resolve them
# here — the model is never asked to absolutize. No path-confinement jail:
# resolution is deterministic and logged; a jail would be wrongness-prevention.

DEFAULT_RUN_TIMEOUT_S = 60.0


def resolve_path(base: Path, path: str) -> Path:
    """The single resolution rule: ``(base / path).resolve()``.

    Relative paths resolve against ``base``; absolute paths pass through
    (pathlib semantics of ``/``).
    """
    return (base / path).resolve()


def tool_read(base: Path, path: str) -> str:
    return resolve_path(base, path).read_text()


def tool_write(base: Path, path: str, content: str) -> str:
    target = resolve_path(base, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"wrote {len(content)} characters to {path}"


def tool_list(base: Path, path: str = ".") -> str:
    entries = sorted(resolve_path(base, path).iterdir(), key=lambda p: p.name)
    return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries) or "(empty)"


def tool_run(base: Path, command: str, timeout_s: float = DEFAULT_RUN_TIMEOUT_S) -> str:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=base,
        env={**os.environ, "PWD": str(base)},
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return json.dumps(
        {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    )


TOOLS_V0: list[dict[str, Any]] = [
    {
        "name": "read",
        "description": (
            "Read a text file and return its contents. Give the path relative "
            "to the working directory; the tool resolves it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path, relative to the working directory."}},
            "required": ["path"],
        },
    },
    {
        "name": "write",
        "description": (
            "Write a text file, creating parent directories as needed. Give the "
            "path relative to the working directory; the tool resolves it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to the working directory."},
                "content": {"type": "string", "description": "Full file content to write."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list",
        "description": (
            "List directory entries (directories have a trailing slash). Give "
            "the path relative to the working directory; default is the working "
            "directory itself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path, relative to the working directory. Defaults to '.'."}},
        },
    },
    {
        "name": "run",
        "description": (
            "Run a shell command in the working directory. Returns JSON with "
            "exit_code, stdout, stderr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Shell command line to execute."}},
            "required": ["command"],
        },
    },
]


@dataclass(frozen=True)
class Tool:
    """One offered tool: its JSON spec and the callable that serves it.

    ``func`` is called as ``func(base, **arguments)`` — the working directory
    first, then the model's arguments by keyword. A tool that has nothing to
    do with the filesystem still receives ``base`` and simply ignores it; one
    signature keeps ``dispatch_tool`` free of special cases. The return value
    is the tool_result content and must be a string.
    """

    spec: dict[str, Any]
    func: Callable[..., str]

    @property
    def name(self) -> str:
        return self.spec["name"]


_SPECS_V0 = {spec["name"]: spec for spec in TOOLS_V0}

# The four built-ins, as the default tool set. Presets are plain sequences:
# a caller composes its own by concatenating, filtering, or writing new Tools.
DEFAULT_TOOLS: tuple[Tool, ...] = (
    Tool(_SPECS_V0["read"], tool_read),
    Tool(_SPECS_V0["write"], tool_write),
    Tool(_SPECS_V0["list"], tool_list),
    Tool(_SPECS_V0["run"], tool_run),
)

# Read-only preset: the tool set *is* the permission. A door that must not
# write is handed this instead of a permission engine — there is no denied
# call to attempt, and nothing to explain to a weak model.
READONLY_TOOLS: tuple[Tool, ...] = (
    Tool(_SPECS_V0["read"], tool_read),
    Tool(_SPECS_V0["list"], tool_list),
)


# Presets reachable from the CLI, so a subprocess caller (run_harness) can pick
# a door's tool set without an in-process import. "default" is what the CLI
# uses when the flag is absent.
TOOL_PRESETS: dict[str, tuple[Tool, ...]] = {
    "default": DEFAULT_TOOLS,
    "read-only": READONLY_TOOLS,
}


def tool_table(tools: Sequence[Tool]) -> dict[str, Tool]:
    """Name → Tool, rejecting duplicates rather than silently shadowing."""
    table: dict[str, Tool] = {}
    for tool in tools:
        if tool.name in table:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        table[tool.name] = tool
    return table


def response_text(response: dict[str, Any]) -> str:
    """Concatenated text blocks of a Messages response.

    Local models (qwen3.6 via ollama) emit a ``thinking`` block before the
    ``text`` block, so extraction is by block type, never by index. Blocks
    that are not objects with a string ``text`` are skipped rather than
    trusted — a backend that mixes junk into ``content`` must not raise here.
    """
    blocks = response.get("content")
    if not isinstance(blocks, list):
        return ""
    return "".join(
        b["text"]
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def accumulate_usage(usage: dict[str, int], reported: Any) -> None:
    """Add one response's usage block into the running totals.

    Usage is telemetry, not contract: a backend that reports ``null``, a
    non-object, or non-integer counts contributes zero — it never fails an
    otherwise good run.
    """
    if not isinstance(reported, dict):
        return
    for key in usage:
        value = reported.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            usage[key] += value


class MessagesClient:
    """Non-streaming client for ``POST {base_url}/v1/messages``.

    Stdlib only. The payload it sends is exactly the dict it builds — no SDK
    defaults sneak in — so wire captures are byte-honest.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float | None = None,
        timeout_s: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_s = timeout_s

    def build_payload(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools is not None:
            payload["tools"] = tools
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def create(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.post(self.build_payload(system, messages, tools))

    def request_timeout(self, remaining_s: float | None) -> float:
        """The timeout for one request: the caller's remaining budget, capped
        by the client's own timeout. Narrows, never widens — a caller with a
        deadline must not wait past it on a stalled backend."""
        if remaining_s is None:
            return self.timeout_s
        return min(self.timeout_s, max(remaining_s, 0.0))

    def post(self, payload: dict[str, Any], *, timeout_s: float | None = None) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-api-key": DUMMY_API_KEY,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout(timeout_s)) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise MessagesError(e.code, e.read().decode("utf-8", "replace")) from e


@dataclass
class AgcodeResult:
    """Result of one harness run.

    ``meta`` speaks the ``ag.agent-run.v1`` field spellings natively
    (contract: the ``ag.agent-run.v1`` record fields, §9): ``num_turns``,
    ``usage`` (input/output tokens as reported by the backend),
    ``duration_ms``, ``outcome`` (``done`` / ``aborted`` / ``failed``),
    ``transcript`` (path, when one was requested), and — when the run is not
    done — ``failure``, a human string prefixed with its machine-readable
    kind (``"<failure_kind>: <detail>"``; the kind also stays available as
    the separate ``failure_kind`` key for in-process callers). Extra keys
    beyond the contract: ``run_id``, ``malformed_tool_calls``, ``truncated``
    (at least one response was cut off at ``max_tokens``; the run continued
    after each, so this is a quality signal, not an outcome), and ``empty_final``
    (the run stopped cleanly with no closing text; also ``done``, with an
    empty ``output`` — what it achieved is read from what it did).

    The contract's identity fields (``role`` / ``profile`` / ``harness`` /
    ``provider`` / ``model``) are caller-side and never appear here, and
    ``cost_usd`` is never emitted (the backend reports no cost; "missing is
    fine, invented is not"), so a caller-side record write is a plain
    ``{**identity, **result.meta}`` merge.

    Once arguments are valid, ``run()`` returns, never raises: any backend or
    model misbehavior normalizes into an outcome + failure kind.
    """

    output: str
    status: str
    meta: dict[str, Any] = field(default_factory=dict)


# What a turn cut off at max_tokens is told. Both name the limit and ask for
# smaller steps, because the model cannot see why its own response stopped.
CUT_OFF_TOOL_RESULT = (
    "This call was cut off by the response token limit and was NOT executed. "
    "Retry it in smaller pieces — for a large file, write one part and append "
    "the rest in further calls."
)
CUT_OFF_NUDGE = (
    "Your previous response was cut off by the response token limit. Continue "
    "from where you stopped, in smaller steps, and keep any single tool call "
    "short enough to complete."
)

SYSTEM_PROMPT = """\
You are agcode, a coding agent.

Working directory: {working_dir}

Complete the user's task using the provided tools. File paths passed to tools
are relative to the working directory; the tools resolve them — never convert
a path yourself. When the task is complete, reply with the final answer as
plain text."""


def compose_system(working_dir: Path | str, system_suffix: str | None = None) -> str:
    """The system prompt actually sent: the pinned template first, then the
    caller's per-role instructions.

    The working-directory sentence is unconditional and comes first, so no
    suffix can displace the one thing the base rule depends on.
    """
    system = SYSTEM_PROMPT.format(working_dir=working_dir)
    if system_suffix and system_suffix.strip():
        system = f"{system}\n\n{system_suffix.strip()}"
    return system


def dispatch_tool(
    base: Path, name: str, args: Any, tools: dict[str, Tool]
) -> tuple[str, bool, bool]:
    """Execute one tool call; return ``(content, is_error, is_malformed)``.

    All errors come back as an error tool_result so the loop continues.
    ``is_malformed`` marks model-side call defects (unknown tool, bad
    arguments) as opposed to legitimate calls that fail at runtime (missing
    file, command timeout); the loop counts malformed calls in ``meta``.
    """
    tool = tools.get(name)
    if tool is None:
        return f"unknown tool: {name!r} (available: {', '.join(tools)})", True, True
    if not isinstance(args, dict):
        return f"tool arguments must be an object, got: {args!r}", True, True
    try:
        return tool.func(base, **args), False, False
    except TypeError as e:
        return f"bad arguments for {name}: {e}", True, True
    except subprocess.TimeoutExpired as e:
        return f"command timed out after {e.timeout}s", True, False
    except (OSError, ValueError) as e:
        # ValueError covers UnicodeDecodeError (a non-UTF-8 file for ``read``,
        # non-decodable command output for ``run``) — a runtime surprise, not
        # a model-side call defect, so the loop continues with an error result.
        return f"{type(e).__name__}: {e}", True, False


TRANSCRIPT_FORMAT = "agcode-transcript-v1"


class _Transcript:
    """Self-describing per-run JSONL capture (transcript format v1).

    Line 1 is a ``{"record": "meta", ...}`` header identifying the run, so
    the file stands alone as evidence without the runner's stdout beside it.
    Every following line is a verbatim ``{"direction": "request"|"response",
    "body": <payload>}`` record, exactly as in P1.
    """

    def __init__(self, path: str | None, header: dict[str, Any] | None = None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("")
            if header is not None:
                self._write({"record": "meta", **header})

    def _write(self, obj: dict[str, Any]) -> None:
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(obj) + "\n")

    def append(self, direction: str, body: dict[str, Any]) -> None:
        self._write({"direction": direction, "body": body})


def run(
    task: str,
    working_dir: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    deadline_s: float = DEFAULT_DEADLINE_S,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float | None = None,
    task_input: str | None = None,
    tools: Sequence[Tool] = DEFAULT_TOOLS,
    system_suffix: str | None = None,
    stop: Callable[[], bool] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    transcript_path: str | None = None,
    transcript_meta: dict[str, Any] | None = None,
) -> AgcodeResult:
    """Run one agentic task to completion against a Messages API backend.

    ``base_url`` and ``model`` fall back to the ``AGCODE_BASE_URL`` /
    ``AGCODE_MODEL`` environment variables. No other ambient configuration is
    read; in particular, nothing is loaded from the invoking user's home.

    ``model`` is the provider-native name (e.g. ``qwen3.6:35b-a3b-coding-nvfp4``);
    the canonical ``provider/name`` model ID of ``ag.agent-config.v1`` §3 is
    caller-side — the caller's loader derives the native name from it.

    ``task_input`` is content-handing mode (roadmap principle 3): the caller
    passes the task's input content inline, and it travels to the model
    verbatim as a second text block of the first user message — no pointer
    (path) has to be transcribed by the model, and the transcript shows the
    exact block on the wire like everything else.

    ``tools`` is the offered tool set, spec and callable together. It is the
    whole permission surface: a door that must not write is handed
    ``READONLY_TOOLS`` (or its own list), not a deny rule. Passing an empty
    sequence offers no tools at all.

    ``system_suffix`` is appended to the pinned system prompt — this is where
    per-role instructions (an ``AGENTS.md``-style file) arrive. It cannot
    displace the working-directory sentence, which stays first.

    ``stop`` is checked between turns; when it returns true the run ends as
    ``aborted`` with failure kind ``cancelled``, carrying whatever usage and
    turn count it had accumulated.

    ``on_event`` receives one dict per conversation step as the run proceeds:
    ``{"type": "assistant", "message": {"role": "assistant", "content": [...]}}``
    after each model response, and ``{"type": "user", "message": {"role":
    "user", "content": [...tool_result...]}}`` after the tool calls it asked
    for. The shapes match claude_code's ``stream-json`` events on purpose, so
    one consumer can watch either harness. Events are telemetry, like usage: a
    consumer that raises never fails an otherwise good run — its first
    complaint lands in ``meta["event_consumer_error"]`` instead, so a display
    that silently stopped working leaves a trace.

    ``transcript_meta`` merges extra caller-known fields (e.g. fixture
    markers) into the transcript's meta header record.
    """
    base_url = base_url or os.environ.get("AGCODE_BASE_URL") or DEFAULT_BASE_URL
    model = model or os.environ.get("AGCODE_MODEL")
    if not model:
        raise ValueError("model is required (argument or AGCODE_MODEL)")

    base = Path(working_dir).resolve()
    tool_specs = [tool.spec for tool in tools]
    table = tool_table(tools)
    client = MessagesClient(
        base_url, model, max_tokens=max_tokens, temperature=temperature
    )
    system = compose_system(base, system_suffix)
    first: str | list[dict[str, Any]] = task
    if task_input is not None:
        first = [
            {"type": "text", "text": task},
            {"type": "text", "text": task_input},
        ]
    messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
    run_id = uuid.uuid4().hex
    header: dict[str, Any] = {
        "format": TRANSCRIPT_FORMAT,
        "run_id": run_id,
        "model": model,
        "base_url": base_url,
        "working_dir": str(base),
        "task": task,
        "content_mode": task_input is not None,
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if transcript_meta:
        header.update(transcript_meta)
    transcript = _Transcript(transcript_path, header)

    consumer_errors: list[str] = []

    def emit(event: dict[str, Any]) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception as error:  # noqa: BLE001 - events are telemetry, never fatal
            consumer_errors.append(f"{type(error).__name__}: {error}")

    started = time.monotonic()
    turns = 0
    usage = {"input_tokens": 0, "output_tokens": 0}
    malformed_tool_calls = 0
    output = ""
    truncated = False
    outcome, failure_kind, failure = "aborted", None, None

    try:
        while True:
            if stop is not None and stop():
                failure_kind = "cancelled"
                failure = "cancelled by the caller"
                break
            if turns >= max_turns:
                failure_kind = "turn_budget_exhausted"
                failure = f"max_turns ({max_turns}) exhausted"
                break
            remaining_s = deadline_s - (time.monotonic() - started)
            if remaining_s < 0:
                failure_kind = "deadline_exceeded"
                failure = f"deadline ({deadline_s}s) exceeded"
                break

            payload = client.build_payload(system, messages, tool_specs)
            transcript.append("request", payload)
            try:
                # The per-request timeout is the remaining deadline (capped by
                # the client's own), so a backend that accepts the connection
                # and then stalls cannot outlive the deadline.
                resp = client.post(payload, timeout_s=remaining_s)
            except (MessagesError, urllib.error.URLError, TimeoutError) as e:
                if _is_timeout(e) and time.monotonic() - started >= deadline_s:
                    failure_kind = "deadline_exceeded"
                    failure = f"deadline ({deadline_s}s) exceeded waiting for the backend"
                    break
                outcome, failure_kind = "failed", "connect_error"
                failure = f"{type(e).__name__}: {e}"
                break
            except json.JSONDecodeError as e:
                outcome, failure_kind = "failed", "malformed_response"
                failure = f"non-JSON response body: {e}"
                break
            transcript.append("response", resp)
            turns += 1

            if not isinstance(resp, dict):
                outcome, failure_kind = "failed", "malformed_response"
                failure = f"response body is {type(resp).__name__}, expected a JSON object"
                break
            accumulate_usage(usage, resp.get("usage"))
            cut_off = resp.get("stop_reason") == "max_tokens"
            truncated = truncated or cut_off

            content_blocks = resp.get("content")
            if not isinstance(content_blocks, list):
                outcome, failure_kind = "failed", "malformed_response"
                failure = (
                    f"response content is {type(content_blocks).__name__}, "
                    "expected a block list"
                )
                break
            messages.append({"role": "assistant", "content": content_blocks})
            emit({"type": "assistant", "message": {"role": "assistant", "content": content_blocks}})
            tool_uses = [
                b for b in content_blocks if isinstance(b, dict) and b.get("type") == "tool_use"
            ]
            if resp.get("stop_reason") == "tool_use":
                if not tool_uses:
                    outcome, failure_kind = "failed", "malformed_response"
                    failure = "stop_reason is tool_use but the response has no tool_use blocks"
                    break
                results = []
                for block in tool_uses:
                    content, is_error, is_malformed = dispatch_tool(
                        base, block.get("name"), block.get("input"), table
                    )
                    malformed_tool_calls += is_malformed
                    result: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": content,
                    }
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
                messages.append({"role": "user", "content": results})
                emit({"type": "user", "message": {"role": "user", "content": results}})
                continue

            if cut_off:
                # The response hit max_tokens: the model was mid-action, not
                # finished. Ending here reads a cut-off preamble ("Now let me
                # create the core simulation logic:") as the final answer and
                # throws the turn away — observed on a 35B local model, whose
                # long thinking blocks plus one whole-file write overrun the
                # response cap routinely. So the run continues: any
                # tool call that was cut off is answered as an error (its
                # arguments are incomplete and must never be executed), and a
                # response cut off before any call gets a plain nudge. The
                # turn budget and the deadline remain the only stopping
                # conditions, as for any other turn.
                if tool_uses:
                    results = [
                        {
                            "type": "tool_result",
                            "tool_use_id": block.get("id"),
                            "content": CUT_OFF_TOOL_RESULT,
                            "is_error": True,
                        }
                        for block in tool_uses
                    ]
                    messages.append({"role": "user", "content": results})
                    emit({"type": "user", "message": {"role": "user", "content": results}})
                else:
                    nudge = [{"type": "text", "text": CUT_OFF_NUDGE}]
                    messages.append({"role": "user", "content": nudge})
                    emit({"type": "user", "message": {"role": "user", "content": nudge}})
                continue

            # A clean stop is a done run, whatever the final response carried:
            # a stop with no text at all is a run that ended without a closing
            # message (meta["empty_final"]), which is not the harness's to
            # call a failure.
            output = response_text(resp)
            outcome = "done"
            break
    except Exception as e:  # noqa: BLE001 - last-resort backstop, see below
        # The known holes each have specific handling above; this exists for
        # the unknown ones, so the never-raises promise holds unconditionally.
        outcome, failure_kind = "failed", "malformed_response"
        failure = f"unexpected exception in the run loop: {e!r}"

    assert failure_kind is None or failure_kind in FAILURE_KINDS
    status = {"done": "ok"}.get(outcome, outcome)
    meta: dict[str, Any] = {
        "run_id": run_id,
        "num_turns": turns,
        "usage": usage,
        "malformed_tool_calls": malformed_tool_calls,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "outcome": outcome,
        "truncated": truncated,
        "empty_final": not output.strip(),
    }
    if consumer_errors:
        # Never a failure: the consumer watches the run, it does not do it.
        meta["event_consumer_error"] = (
            f"{len(consumer_errors)} event(s) not consumed; first: {consumer_errors[0]}"
        )
    if failure is not None:
        meta["failure"] = f"{failure_kind}: {failure}"
        meta["failure_kind"] = failure_kind
    if transcript.path:
        meta["transcript"] = str(transcript.path)
    return AgcodeResult(output=output, status=status, meta=meta)


# --- CLI entry point ---------------------------------------------------------
#
# ``python -m agcode`` — a thin wrapper over run() with no logic of its own
# beyond argument parsing and result serialization. Wire contract (P4): the
# task arrives on stdin (matching how pyagag's run_harness() feeds prompts),
# stdout carries exactly one JSON document with ``output``, ``status`` and the
# meta fields flat, and the exit code is 0 iff the outcome is ``done``
# (1 failed, 2 aborted) — so a future caller-side extractor stays tiny.

EXIT_CODES = {"done": 0, "failed": 1, "aborted": 2}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agcode",
        description=(
            "Run one agentic task against a Messages API backend. The task is "
            "read from stdin; the result is one JSON document on stdout."
        ),
    )
    parser.add_argument(
        "--working-dir",
        default=".",
        help="the single base directory tools resolve paths against (default: cwd)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="provider-native model name (default: AGCODE_MODEL)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Messages API base URL (default: AGCODE_BASE_URL or local ollama)",
    )
    parser.add_argument(
        "--tools",
        choices=sorted(TOOL_PRESETS),
        default="default",
        help=(
            "the offered tool set: 'default' is read/write/list/run, "
            "'read-only' is read/list. The set is the permission; there is no "
            "deny rule (default: default)"
        ),
    )
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--deadline-s", type=float, default=DEFAULT_DEADLINE_S)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=None)
    # Both at once is a usage error rather than a silent precedence rule —
    # there is no reading of "hand the model this content" that wants two.
    task_input_group = parser.add_mutually_exclusive_group()
    task_input_group.add_argument(
        "--task-input",
        default=None,
        help="content-handing mode: input content passed verbatim to the model",
    )
    task_input_group.add_argument(
        "--task-input-file",
        default=None,
        help="like --task-input, but read the content from this file",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        help="write the verbatim wire transcript (JSONL) to this path",
    )
    parser.add_argument(
        "--output-format",
        choices=["json", "stream-json"],
        default="json",
        help=(
            "'json' is the P4 contract: exactly one JSON document on stdout. "
            "'stream-json' emits one JSON line per conversation step as the "
            "run proceeds (claude_code's event spellings), ending with a "
            "'type':'result' line carrying the same fields as the single "
            "document (default: json)"
        ),
    )
    args = parser.parse_args(argv)

    task = sys.stdin.read()
    if not task.strip():
        parser.error("no task on stdin")
    task_input = args.task_input
    if args.task_input_file is not None:
        task_input = Path(args.task_input_file).read_text()

    streaming = args.output_format == "stream-json"

    def emit_line(event: dict[str, Any]) -> None:
        # flush per line: a pipe reader sees the event when it happens, not
        # when the block buffer fills.
        print(json.dumps(event, ensure_ascii=False), flush=True)

    try:
        result = run(
            task,
            args.working_dir,
            base_url=args.base_url,
            model=args.model,
            max_turns=args.max_turns,
            deadline_s=args.deadline_s,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            task_input=task_input,
            tools=TOOL_PRESETS[args.tools],
            on_event=emit_line if streaming else None,
            transcript_path=args.transcript,
        )
    except ValueError as e:  # argument-validation errors, e.g. missing model
        parser.error(str(e))

    doc = {"output": result.output, "status": result.status, **result.meta}
    if streaming:
        doc = {"type": "result", **doc}
    print(json.dumps(doc, ensure_ascii=False))
    return EXIT_CODES[result.meta["outcome"]]


if __name__ == "__main__":
    sys.exit(main())
