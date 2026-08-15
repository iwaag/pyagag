"""Common process seam for agent harnesses."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .agent_config import ResolvedAgent

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
) -> list[str]:
    extra_args = list(extra_args or [])
    if "--model" in extra_args or "-m" in extra_args:
        raise ValueError("model selection belongs to the resolved profile")
    if agent.harness == "claude_code":
        argv = [agent.command, "-p", "--output-format", "json", "--model", agent.native_model]
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
        return argv + extra_args
    if agent.harness == "fake":
        return [agent.command, *extra_args]
    raise ValueError(f"unsupported harness: {agent.harness}")


def _extract_claude(raw: str) -> tuple[str, dict]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
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
        return raw, {}
    if not isinstance(doc, dict):
        return raw, {}
    meta = {
        key: doc[key]
        for key in ("duration_ms", "num_turns", "usage", "outcome", "failure", "transcript")
        if key in doc
    }
    return doc.get("output") if isinstance(doc.get("output"), str) else "", meta


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
    transcript_path: Path | None = None,
    output_tail_chars: int = DEFAULT_OUTPUT_TAIL_CHARS,
) -> HarnessResult:
    """Launch, extract, and normalize one harness process without fallback."""
    meta = identity(agent)
    if timeout <= 0:
        return HarnessResult(
            "agent run timed out (no budget left)", -1,
            {**meta, "outcome": "aborted", "failure": "timeout"},
        )
    try:
        argv = build_argv(
            agent,
            allowed_tools=allowed_tools,
            add_dirs=add_dirs,
            extra_args=extra_args,
            skip_permissions=skip_permissions,
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
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
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
    raw = ANSI_RE.sub("", proc.stdout or "")
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
    stderr_tail = ANSI_RE.sub("", proc.stderr or "").strip()[-output_tail_chars:]
    failure = None
    reported_failure = reported.get("failure") if isinstance(reported.get("failure"), str) else None
    if proc.returncode != 0:
        # A harness that reports its own failure string keeps it when it had no
        # output to quote — agcode's is kind-prefixed and worth more than "no
        # output".
        tail = output.strip()[-output_tail_chars:] or reported_failure or "no output"
        failure = f"{agent.harness} exited {proc.returncode}: {tail}"
        if stderr_tail:
            failure += f"; stderr tail: {stderr_tail}"
    elif meta.get("is_error"):
        tail = output.strip()[-output_tail_chars:] or "no output"
        failure = f"{agent.harness} reported an error ({meta.get('subtype')}): {tail}"
    elif not output.strip():
        failure = f"{agent.harness} produced no output" + (f": {stderr_tail}" if stderr_tail else "")
    meta["outcome"] = "failed" if failure else "done"
    # A harness that ended itself on a budget or deadline reports "aborted";
    # that stays, because run_harness spells its own timeout the same way. Only
    # the aborted/failed distinction is preserved — the failure text above is
    # still recomposed by this function, per the harness-result contract.
    if failure and reported.get("outcome") == "aborted":
        meta["outcome"] = "aborted"
    if failure:
        meta["failure"] = failure
    exit_code = proc.returncode if failure is None else (proc.returncode or -1)
    return HarnessResult(output if output.strip() else (failure or ""), exit_code, meta, raw)


def write_run_record(
    path: Path,
    *,
    request_id: str,
    meta: dict,
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
    record["outcome"] = outcome or meta.get("outcome", "failed")
    failure = failure or meta.get("failure")
    if failure:
        record["failure"] = failure
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
