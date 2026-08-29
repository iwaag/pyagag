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
) -> list[str]:
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
    if agent.harness == "fake":
        return [agent.command, *extra_args]
    raise ValueError(f"unsupported harness: {agent.harness}")


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

    ``on_event`` switches claude_code and agcode to their stream-json modes
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
    stream = (
        (on_event is not None or stream) and agent.harness in ("claude_code", "agcode")
    )
    try:
        argv = build_argv(
            agent,
            allowed_tools=allowed_tools,
            add_dirs=add_dirs,
            extra_args=extra_args,
            skip_permissions=skip_permissions,
            stream=stream,
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
    try:
        if stream:
            returncode, stdout, stderr, consumer_errors = _run_streaming(
                argv, prompt, cwd=cwd, timeout=timeout, env=env,
                on_event=on_event if on_event is not None else lambda event: None,
            )
        else:
            proc = subprocess.run(
                argv,
                input=prompt,
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
    record["outcome"] = outcome or meta.get("outcome", "failed")
    failure = failure or meta.get("failure")
    if failure:
        record["failure"] = failure
    if extra_meta:
        record.update(extra_meta)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
