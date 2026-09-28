"""Serving one probe against the fixture board, as a listener would.

An agent's trial driver (`agfront.trial`, …) builds the serving with the
agent's own code — the same chatlog, tools files and prompt its listener
would build — and runs it inside `fixture_environment`, which does two
things for the duration:

- every run started through `agag.agent` gets `AGENTCHAT_MIRROR` set to the
  fixture store instead of the listener's own mirror, so what the run reads
  with `agentchat` (and `agproject status`, whose repository facts are the
  fixture's own) is the fixture board, and what it tries to post is refused;
  its `AGENTCHAT_JOURNAL` is beside the store, not the listener's;
- `client()` is a `MirrorReads` over the fixture, the client the serving's
  own reads go through.

`probe_history` is the conversation a probe is served: the person's one
post, with an id above anything on the board. `outcome` pulls the reply the
run marked and judges it (`probes.judge`).

The rest is the kit every agent's driver (`agfront.trial`, `archsage.trial`,
`agautolab.trial`, `agobserver.trial`) shares (`agent_guide` p2 ex1):
`trial_parser` is the command line they all take, `Trial.start` prepares a
trial from it (the board, built fresh under `--out` unless `--store` names
one; the guide tree, from `--guides` or materialised from git at
`--guides-rev`), `Trial.session` is what a serving runs inside, and
`Trial.finish` judges the reply beside the run record's facts and the run's
tool calls (`newest`, `record_facts`, `tool_calls`, `session_log`).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from agag import agent as agent_module
from agag import topics as topics_module
from agag.agent import RECORDS_ROOT_VARIABLE
from agag.harness import HarnessResult
from agag.mirror.reads import MirrorReads
from agag.reply import split_reply

from .board import DEV, NAMES, OBSERVER, build_store
from .probes import PROBES, Probe, judge

__all__ = ["DRY_REPLY", "PROBE_ID", "Trial", "TrialError", "client", "fixture_environment", "guides_at", "newest",
           "outcome", "probe_history", "record_facts", "session_log", "tool_calls", "trial_parser"]

#: The probe post's id: above every id the board holds.
PROBE_ID = 30_001


@contextlib.contextmanager
def fixture_environment(store: Path):
    """Runs started inside read the fixture board and can post nowhere."""
    original = agent_module.chat_environment

    def chat_environment(spec, **kwargs):
        environment = original(spec, **kwargs)
        environment["AGENTCHAT_MIRROR"] = str(store)
        # `agentchat receipt` reads a listener journal for evidence: the
        # board's (none, unless a trial puts one beside it), never the live
        # listener's.
        environment["AGENTCHAT_JOURNAL"] = str(Path(store).parent / "listener.sqlite")
        return environment

    agent_module.chat_environment = chat_environment
    try:
        yield
    finally:
        agent_module.chat_environment = original


def client(store: Path) -> MirrorReads:
    return MirrorReads(Path(store))


def probe_history(probe: Probe, board=None) -> list[dict]:
    """The probe's one post; or, for a probe with no text, its conversation
    as it stands on the board (`board`, a fixture client)."""
    if not probe.text and board is not None:
        return board.topic_history(probe.channel, probe.topic, 200)
    sender = OBSERVER if probe.speaker == NAMES[OBSERVER] else DEV
    return [{"id": PROBE_ID, "type": "stream", "display_recipient": probe.channel, "subject": probe.topic,
             "sender_id": sender, "sender_full_name": probe.speaker, "sender_realm_str": "",
             "timestamp": int(time.time()), "content": probe.text}]


def outcome(probe: Probe, output: str, directory: Path, **facts) -> dict:
    """The marked reply, judged, written beside the run as `outcome.json`."""
    split = split_reply(output or "")
    reply = split.reply if split.reply else (output or "")
    verdict = judge(probe, reply, facts.get("tool_calls"))
    result = {**verdict, "marked": bool(split.reply), **facts, "reply": reply}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "outcome.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    (directory / "reply.md").write_text(reply + "\n", encoding="utf-8")
    return result


# --- what a run left behind ---------------------------------------------------------


def newest(directory: Path, pattern: str) -> Path | None:
    """The most recently written match of `pattern` in `directory`, or None."""
    directory = Path(directory)
    found = sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime) if directory.is_dir() else []
    return found[-1] if found else None


def tool_calls(transcript: Path | None) -> list[str]:
    """The Bash commands and other tool calls a run made, in order, from a
    Claude Code session log (or a harness transcript in the same shape)."""
    calls: list[str] = []
    if transcript is None or not Path(transcript).is_file():
        return calls
    for line in Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        for block in ((message or {}).get("content") or []) if isinstance(message, dict) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                arguments = block.get("input") or {}
                calls.append(f"{block.get('name')}: {arguments.get('command') or arguments.get('file_path') or ''}"[:200])
    return calls


def session_log(cwd: Path) -> Path | None:
    """Claude Code keeps a run's session under `~/.claude/projects/<its cwd,
    slugged>`; the newest one there."""
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return newest(Path.home() / ".claude" / "projects" / slug, "*.jsonl")


def record_facts(records: Path) -> dict:
    """Cost, turns, duration and model of the newest run record in `records`."""
    path = newest(records, "run-*.json")
    record = json.loads(path.read_text(encoding="utf-8")) if path else {}
    return {"record": str(path or ""), "cost_usd": record.get("cost_usd"), "turns": record.get("num_turns"),
            "duration_s": round((record.get("duration_ms") or 0) / 1000, 1), "model": record.get("model")}


# --- the kit ------------------------------------------------------------------------


class TrialError(RuntimeError):
    """A trial could not be prepared (an unknown revision, a tree without guides)."""


#: What a dry run's harness answers: a marked reply, so the serving completes.
DRY_REPLY = "<ag-reply intent=report>\n(dry run)\n</ag-reply>"


def guides_at(repository: Path, rev: str, into: Path, subtree: str = "agent/guides") -> Path:
    """`repository`'s `subtree` at `rev`, written under `into` (`git archive`);
    returns the materialised tree. The same revision gives the same tree, so a
    baseline is a commit, not a copy someone kept."""
    listing = subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"],
                             capture_output=True, text=True)
    if listing.returncode != 0:
        raise TrialError(f"no commit {rev!r} in {repository}")
    commit = listing.stdout.strip()
    archive = subprocess.run(["git", "-C", str(repository), "archive", "--format=tar", commit, subtree],
                             capture_output=True)
    if archive.returncode != 0:
        raise TrialError(f"{repository} has no {subtree} at {rev}: {archive.stderr.decode(errors='replace').strip()}")
    into = Path(into)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(into, filter="data")
    (into / "REVISION").write_text(f"{rev} {commit}\n", encoding="utf-8")
    return into / subtree


def trial_parser(prog: str, description: str, agent: str) -> argparse.ArgumentParser:
    """The command line every agent's driver takes; its probes are the ones
    `agag.fixture.probes` gives to `agent`."""
    parser = argparse.ArgumentParser(prog=prog, description=description,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("probe", choices=sorted(n for n, p in PROBES.items() if p.agent == agent))
    parser.add_argument("--out", type=Path, required=True,
                        help="where outcome.json, reply.md (and prompt.md on a dry run) go")
    parser.add_argument("--store", type=Path, default=None,
                        help="a fixture board's mirror.sqlite (default: built fresh as <out>/board/mirror.sqlite)")
    guides = parser.add_mutually_exclusive_group()
    guides.add_argument("--guides", type=Path, default=None,
                        help="serve with this guide tree (an agent/guides directory; default: this checkout's)")
    guides.add_argument("--guides-rev", default=None, metavar="COMMIT",
                        help="serve with this checkout's agent/guides as of COMMIT (git archive into <out>)")
    parser.add_argument("--no-shared", action="store_true",
                        help="leave out pyagag's shared guide sections (the composition before agent_guide p2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the serving and write its prompt to <out>/prompt.md; run no model")
    parser.add_argument("--records", type=Path, default=None,
                        help="where the serving's workspace and run record go (default: <out>/records); never the "
                             "checkout's .local/, where the relay counts runs as live")
    return parser


@dataclass
class Trial:
    """One probe, prepared: what is served, against which board, with which guides."""

    probe: Probe
    out: Path
    store: Path
    guides: Path | None
    guides_rev: str
    shared: bool
    dry_run: bool
    records: Path

    @classmethod
    def start(cls, args: argparse.Namespace, repository: Path) -> "Trial":
        """From `trial_parser`'s arguments; `repository` is the agent's
        checkout, where `--guides-rev` is looked up.

        It sets `AGAG_RECORDS_ROOT` for the rest of the process, so every
        `AgentSpec` puts workspaces and run records under the trial's own
        root; a driver imports its agent's modules after this, because some
        keep those paths as module constants."""
        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        records = Path(args.records).resolve() if args.records else out / "records"
        records.mkdir(parents=True, exist_ok=True)
        os.environ[RECORDS_ROOT_VARIABLE] = str(records)
        store = Path(args.store).resolve() if args.store else build_store(out / "board")
        guides = Path(args.guides).resolve() if args.guides else None
        if args.guides_rev:
            try:
                guides = guides_at(repository, args.guides_rev, out / f"guides@{args.guides_rev}")
            except TrialError as error:
                raise SystemExit(f"trial: {error}") from None
        return cls(probe=PROBES[args.probe], out=out, store=store, guides=guides, guides_rev=args.guides_rev or "",
                   shared=not args.no_shared, dry_run=bool(args.dry_run), records=records)

    @contextlib.contextmanager
    def session(self):
        """Runs started inside read the fixture; without shared sections when
        asked; and on a dry run no harness starts: its prompt is written to
        `<out>/prompt.md` and it answers `DRY_REPLY`."""
        with contextlib.ExitStack() as stack:
            stack.enter_context(fixture_environment(self.store))
            if not self.shared:
                stack.enter_context(_patched(topics_module, "shared_sections", lambda names: ""))
            if self.dry_run:
                stack.enter_context(_patched(agent_module, "run_harness", self._dry_harness))
            yield self

    def _dry_harness(self, agent, prompt, *, cwd, **_):
        (self.out / "prompt.md").write_text(prompt, encoding="utf-8")
        print(f"prompt: {len(prompt)} chars, workspace {cwd}")
        return HarnessResult(output=DRY_REPLY, exit_code=0, meta={"dry_run": True})

    def facts(self) -> dict:
        return {"guides": str(self.guides or "(this checkout's)"), "guides_rev": self.guides_rev,
                "shared": self.shared, "store": str(self.store), "dry_run": self.dry_run,
                "records_root": str(self.records)}

    def finish(self, output: str, *, records: Path | None = None, calls: list[str] | None = None, **facts) -> int:
        """Judge `output` under the probe's rule, beside the newest run
        record in `records` and the run's tool calls; print the verdict and
        return the driver's exit status (0 passed, 2 failed)."""
        found = record_facts(records) if records is not None else {}
        result = outcome(self.probe, output or "", self.out, **self.facts(), **found, **facts,
                         tool_calls=list(calls or ()))
        print(json.dumps({k: v for k, v in result.items() if k != "reply"}, ensure_ascii=False, indent=1))
        return 0 if result["passed"] else 2


@contextlib.contextmanager
def _patched(module, name: str, value):
    original = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, original)
