"""A batch of fixture trials: probes × arms × runs, behind a budget gate (`agent_guide` p3 ex2 step 2).

p2's driver, p3's queues and p3 ex1's per-run wrapper each lived in an
ignored folder; this is that tooling, versioned. Each job is one call of an
agent's own driver (`<agent>.trial <probe> --out <dir> <arm's flags>`), which
stays the unit: the batch only orders, gates and files them.

    python -m agag.fixture batch <plan.toml> --out <dir> [--jobs 2] [--dry-run]

The plan (TOML):

    [arms]                     # name → the driver flags of that arm, in order
    before = ["--guides-rev", "ba28e90^", "--no-shared"]
    now = []

    [[probes]]                 # a group of probes and how many runs each arm gets
    names = ["delegate-answer", "delegate-decision"]
    runs = 12
    arms = ["before", "now"]   # optional: a subset of the arms

    [drivers.agfront]          # where each agent's driver runs (relative to the plan file)
    dir = "../agfront"         # default command: <dir>/.venv/bin/python -m <agent>.trial
    command = ["python", "stub.py"]   # optional

    [budget]
    harness = "claude_code"    # the section of `agbudget --json` whose account the runs spend
    limits = { session = 60 }  # window kind → percent at which the queue pauses
    poll_seconds = 300
    command = ["agbudget", "--json"]

**Order** is p3 ex1's: for run 1, every probe with every arm, then run 2…;
within a probe the arms rotate, so each arm goes first as often as the
others. A job's output is `<out>/<arm>/<probe>-<n>/`; a job whose
`outcome.json` exists is skipped, so the same command resumes a batch.

**The budget gate.** Trials spend the same account as the live agents: in p3
ex1 four parallel trials exhausted the 5-hour window (05:21 JST, reset
07:00) and 13 runs were cut. Before each job starts the gate reads the
budget; while any limited window of the harness's section is at or past its
limit, or the reading fails (unobservable is not 0), no job starts. Jobs in
flight finish. Every reading and every pause goes to `<out>/batch.log`.

**A cut job** is one whose saved reply is the harness's limit message (or
that saved nothing and whose log says it). It is not judged: its directory
moves to `<out>/cut/<arm>/<probe>-<n>.<k>/` as evidence and the job goes
back to the end of the queue, at most `retries` times per invocation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .probes import DRIVERS, PROBES

__all__ = ["LIMIT_MESSAGE", "Gate", "Job", "Plan", "is_cut", "load_plan", "order", "run_batch"]

#: What a harness saves as its reply when the account's window is spent
#: (p3 ex1: "You've hit your session limit · resets 7am (Asia/Tokyo)").
LIMIT_MESSAGE = re.compile(r"You've hit your (?:\w+ )?limit|Claude AI usage limit reached|usage limit reached",
                           re.IGNORECASE)


@dataclass(frozen=True)
class Job:
    arm: str
    probe: str
    n: int

    @property
    def name(self) -> str:
        return f"{self.arm}/{self.probe}-{self.n}"


@dataclass
class Plan:
    path: Path
    arms: dict[str, list[str]]
    groups: list[dict]
    drivers: dict[str, dict] = field(default_factory=dict)
    harness: str = "claude_code"
    limits: dict[str, float] = field(default_factory=lambda: {"session": 60.0})
    poll_seconds: float = 300.0
    budget_command: list[str] = field(default_factory=lambda: ["agbudget", "--json"])
    retries: int = 2

    def jobs(self) -> list[Job]:
        return order(self)

    def driver(self, agent: str) -> tuple[list[str], Path]:
        """The argv prefix and working directory of `agent`'s driver."""
        spec = self.drivers.get(agent) or {}
        base = self.path.parent
        directory = (base / spec["dir"]).resolve() if spec.get("dir") else base
        if spec.get("command"):
            return [str(part) for part in spec["command"]], directory
        return [str(directory / ".venv" / "bin" / "python"), "-m", DRIVERS[agent]], directory


def load_plan(path: Path) -> Plan:
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    arms = {str(k): [str(f) for f in v] for k, v in (data.get("arms") or {}).items()}
    if not arms:
        raise ValueError(f"{path}: no [arms]")
    groups = []
    for group in data.get("probes") or []:
        unknown = [p for p in group["names"] if p not in PROBES]
        if unknown:
            raise ValueError(f"{path}: unknown probe(s) {unknown}")
        chosen = list(group.get("arms") or arms)
        if set(chosen) - set(arms):
            raise ValueError(f"{path}: unknown arm(s) {sorted(set(chosen) - set(arms))}")
        groups.append({"names": list(group["names"]), "runs": int(group["runs"]), "arms": chosen})
    budget = data.get("budget") or {}
    plan = Plan(path=path, arms=arms, groups=groups, drivers=dict(data.get("drivers") or {}))
    plan.harness = str(budget.get("harness", plan.harness))
    if "limits" in budget:
        plan.limits = {str(k): float(v) for k, v in budget["limits"].items()}
    plan.poll_seconds = float(budget.get("poll_seconds", plan.poll_seconds))
    if budget.get("command"):
        plan.budget_command = [str(part) for part in budget["command"]]
    plan.retries = int(data.get("retries", plan.retries))
    return plan


def order(plan: Plan) -> list[Job]:
    """p3 ex1's interleaving: run number outermost, then each probe in plan
    order, its arms rotated by the run number."""
    probes = [(name, g["runs"], g["arms"]) for g in plan.groups for name in g["names"]]
    jobs = []
    for n in range(1, max((runs for _, runs, _ in probes), default=0) + 1):
        for name, runs, arms in probes:
            if n > runs:
                continue
            shift = (n - 1) % len(arms)
            for arm in arms[shift:] + arms[:shift]:
                jobs.append(Job(arm, name, n))
    return jobs


def is_cut(directory: Path) -> str:
    """Why the job at `directory` was cut by the account's limit, or ""."""
    directory = Path(directory)
    outcome = directory / "outcome.json"
    if outcome.is_file():
        saved = json.loads(outcome.read_text(encoding="utf-8"))
        replies = [s.get("reply") or "" for s in saved.get("servings") or []] or [saved.get("reply") or ""]
        for reply in replies:
            if (m := LIMIT_MESSAGE.search(reply)) and len(reply) < 400:
                return f"reply: {m.group(0)}"
        return ""
    log = Path(f"{directory}.log")
    if log.is_file() and (m := LIMIT_MESSAGE.search(log.read_text(encoding="utf-8", errors="replace"))):
        return f"log: {m.group(0)}"
    return ""


class Gate:
    """No job starts while the harness's limited windows are at their limits."""

    def __init__(self, plan: Plan, log, *, sleep=time.sleep, search: list[Path] = ()):
        self.plan, self.log, self.sleep = plan, log, sleep
        self.command = self._resolve(plan.budget_command, search)
        self.lock = threading.Lock()
        self.paused_s = 0.0
        self.readings: list[dict] = []

    @staticmethod
    def _resolve(command: list[str], search) -> list[str]:
        head = command[0]
        if os.sep in head or shutil.which(head):
            return command
        for directory in [Path(sys.executable).parent, *search]:
            if (directory / head).is_file():
                return [str(directory / head), *command[1:]]
        return command

    def read(self) -> tuple[dict | None, str]:
        """The limited windows' percents, or why they cannot be read."""
        try:
            done = subprocess.run(self.command, capture_output=True, text=True, timeout=60)
            doc = json.loads(done.stdout)
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            return None, f"budget unreadable: {error}"
        section = (doc.get("harnesses") or {}).get(self.plan.harness) or {}
        if not section.get("ok", True) or section.get("error"):
            return None, f"budget section {self.plan.harness} failed: {section.get('error')}"
        windows = {w.get("kind"): w for w in section.get("windows") or []}
        seen = {kind: windows[kind] for kind in self.plan.limits if kind in windows}
        if not seen:
            return None, f"no limited window ({', '.join(self.plan.limits)}) in {self.plan.harness}"
        return {kind: {"percent": float(w.get("percent") or 0), "resets_at": w.get("resets_at")}
                for kind, w in seen.items()}, ""

    def over(self, reading: dict) -> list[str]:
        return [f"{kind} {w['percent']:g} % ≥ {self.plan.limits[kind]:g} % (resets {_clock(w['resets_at'])})"
                for kind, w in reading.items() if w["percent"] >= self.plan.limits[kind]]

    def wait(self) -> dict | None:
        """Block until every limited window is under its limit; the reading
        that let the job start."""
        with self.lock:
            paused_since = None
            while True:
                reading, trouble = self.read()
                if reading is not None:
                    self.readings.append({"at": time.time(), **{k: v["percent"] for k, v in reading.items()}})
                reasons = [trouble] if trouble else self.over(reading)
                if not reasons:
                    if paused_since is not None:
                        self.paused_s += time.monotonic() - paused_since
                        self.log(f"resume: {_percents(reading)}")
                    return reading
                if paused_since is None:
                    paused_since = time.monotonic()
                    self.log(f"pause: {'; '.join(reasons)}")
                self.sleep(self.plan.poll_seconds)


def run_batch(plan: Plan, out: Path, *, jobs: int = 2, dry_run: bool = False, only: set[str] | None = None,
              echo=print, sleep=time.sleep) -> dict:
    """Run every job of `plan` not yet done under `out`, `jobs` at a time.
    Returns the tally: done, skipped, cut, failed, paused seconds."""
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    logfile = out / "batch.log"
    guard = threading.Lock()

    def log(line: str) -> None:
        stamped = f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {line}"
        with guard:
            with logfile.open("a", encoding="utf-8") as f:
                f.write(stamped + "\n")
            echo(stamped)

    queue = [j for j in plan.jobs() if only is None or j.probe in only]
    todo = [j for j in queue if not (out / j.arm / f"{j.probe}-{j.n}" / "outcome.json").is_file()]
    tally = {"planned": len(queue), "skipped": len(queue) - len(todo), "done": 0, "cut": 0, "failed": 0,
             "passed": 0, "paused_s": 0.0}
    log(f"batch {plan.path.name}: {len(queue)} jobs, {len(todo)} to run, {jobs} at a time"
        f"{' (dry run)' if dry_run else ''}; gate {plan.harness} {plan.limits}")
    gate = Gate(plan, log, sleep=sleep, search=[d / ".venv" / "bin" for d in {plan.driver(PROBES[j.probe].agent)[1]
                                                                          for j in todo}])
    attempts: dict[Job, int] = {}
    pending = list(todo)
    pending_lock = threading.Lock()

    def take() -> Job | None:
        with pending_lock:
            return pending.pop(0) if pending else None

    def one(job: Job) -> None:
        target = out / job.arm / f"{job.probe}-{job.n}"
        if (target / "outcome.json").is_file():
            return
        prefix, cwd = plan.driver(PROBES[job.probe].agent)
        argv = [*prefix, job.probe, "--out", str(target), *plan.arms[job.arm]] + (["--dry-run"] if dry_run else [])
        reading = None if dry_run else gate.wait()
        target.parent.mkdir(parents=True, exist_ok=True)
        log(f"start {job.name}" + (f" [{_percents(reading)}]" if reading else ""))
        started = time.monotonic()
        with open(f"{target}.log", "w", encoding="utf-8") as sink:
            code = subprocess.run(argv, cwd=cwd, stdout=sink, stderr=subprocess.STDOUT).returncode
        seconds = time.monotonic() - started
        cut = is_cut(target)
        if cut:
            attempts[job] = attempts.get(job, 0) + 1
            k = 1
            while (out / "cut" / job.arm / f"{job.probe}-{job.n}.{k}").exists():
                k += 1
            kept = out / "cut" / job.arm / f"{job.probe}-{job.n}.{k}"
            kept.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.move(str(target), str(kept))
            shutil.move(f"{target}.log", f"{kept}.log")
            with guard:
                tally["cut"] += 1
            again = attempts[job] <= plan.retries
            log(f"cut {job.name} ({cut}) → {kept.relative_to(out)}" + ("; queued again" if again else ""))
            if again:
                with pending_lock:
                    pending.append(job)
            return
        if not (target / "outcome.json").is_file():
            with guard:
                tally["failed"] += 1
            log(f"FAILED {job.name}: exit {code}, no outcome.json (see {target.name}.log)")
            return
        saved = json.loads((target / "outcome.json").read_text(encoding="utf-8"))
        with guard:
            tally["done"] += 1
            tally["passed"] += bool(saved.get("passed"))
        log(f"end {job.name}: {'pass' if saved.get('passed') else 'fail'}, exit {code}, {seconds:.0f} s, "
            f"${saved.get('cost_usd') or 0:.2f}")

    def worker() -> None:
        while (job := take()) is not None:
            try:
                one(job)
            except Exception as error:  # a job's trouble must not stop the queue
                with guard:
                    tally["failed"] += 1
                log(f"FAILED {job.name}: {type(error).__name__}: {error}")

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, jobs))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    tally["paused_s"] = round(gate.paused_s, 1)
    (out / "budget.jsonl").open("a", encoding="utf-8").writelines(json.dumps(r) + "\n" for r in gate.readings)
    log(f"batch over: {json.dumps(tally)}")
    return tally


def _clock(stamp) -> str:
    return datetime.fromtimestamp(float(stamp), timezone.utc).strftime("%H:%MZ") if stamp else "?"


def _percents(reading: dict | None) -> str:
    return ", ".join(f"{k} {v['percent']:g} %" for k, v in (reading or {}).items())
