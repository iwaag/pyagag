"""What a batch's runs did, from their saved calls (`agent_guide` p3 ex2 step 2).

p3's `classify.py`/`tab.py` and p3 ex1's `analyze.py`, versioned. A run is
`<out>/<arm>/<probe>-<n>/outcome.json`; nothing here runs a model.

    python -m agag.fixture classify <out> [--probe p…]   # one line per run
    python -m agag.fixture table <out> [--json f]         # pass counts, Wilson 95 %, process measures

Per run:

- **delegation class** (delegation probes, p3): `reached` — a send took a
  scripted agent's answer (by the run's door log since p3 ex2; by a second
  serving before); `new request` — the only door taken was one live
  autolab treats as a new mission (a new `workplan-` topic); `wrong door`
  — sent, and nothing answered; `proposed` — nothing sent and the reply
  proposes and waits for the person's go; `none`.
- **fd-wr attempts**: sends whose target (channel and topic, parsed from the
  call's arguments, not its body) is a ✔ topic of the board, by its bare or
  its ✔ name, refused or not (p3 ex1).
- **filesystem reach**: a search of the filesystem — `find`, `tree`,
  `grep -r`, `ls -R`, the Glob/Grep/LS tools. p3 counted it anywhere in the
  run (before p1 10/24, p1 1/24, now 0/24; this reproduces those counts);
  `first` is the subset before the first `agentchat` call, `outside` a
  search whose path leaves the run's own directory (`/…`, `..`, `~`).
- **unasked send**: a send in a probe that asks for no delegation — on board
  2 that is a "checking in" post into the running task (p3 ex2: before p1
  6 of 18 growbox-thing/guard-status runs, p1 and now none).
- **as9 miss** (delegation probes): nothing sent, and the reply says it will
  answer when the agent asks or reports (p3 ex1 fix delegate-decision 4:
  "選ぶよう求めてきた場合は … 答えます").
"""

from __future__ import annotations

import json
import math
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .batch import is_cut

__all__ = ["Run", "classify", "load_runs", "table", "wilson"]

DELEGATION = ("delegate-answer", "delegate-decision")

#: A reply that proposes the delegation and waits for the person's go.
PROPOSES = re.compile(r"(よければ|よろしければ|よろしいですか|聞きましょうか|確認しましょうか|依頼しましょうか|"
                      r"進めてよい|しますか[？?]|shall I|should I|want me to)")
#: A reply that leaves the asking to the other agent: "if autolab asks me
#: to choose, I will answer" (as9 on running work).
WAITS_TO_BE_ASKED = re.compile(r"((?:聞か|尋ねら|求めら)れた(?:ら|場合)|(?:求めて|聞いて|示して|言って|報告して)きた(?:ら|場合)|"
                               r"報告があれば|問い合わせがあれば|確認が来たら|"
                               r"if (?:autolab|it) asks|when (?:autolab|it) (?:asks|reports))", re.IGNORECASE)
SEARCH = re.compile(r"\bfind\s|\btree\b|\bgrep\s+-[a-zA-Z]*r|\bls\s+-[a-zA-Z]*R")
_VALUED = {"--intent", "--to", "--ask", "--re", "--relation", "--unit", "--evidence"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _resolved_topics() -> set[str]:
    from .board import build_board

    return {row["topic"][2:] for row in build_board().rows if row["topic"].startswith("✔ ")}


def send_target(call: str) -> tuple[str, str] | None:
    """(channel, topic) of an `agentchat send` call: its first two
    positionals. Only the head is read, so a message body cannot be taken
    for a target."""
    head = call[call.index("agentchat send") + len("agentchat send"):]
    lexer = shlex.shlex(head, posix=True)
    lexer.whitespace_split = True
    positionals, skip = [], False
    try:
        for token in lexer:
            if skip:
                skip = False
                continue
            if token.startswith("--"):
                skip = token in _VALUED
                continue
            positionals.append(token)
            if len(positionals) == 2:
                return positionals[0], positionals[1]
    except ValueError:
        pass
    return None


@dataclass
class Run:
    path: Path
    arm: str
    probe: str
    n: int
    passed: bool
    cost: float
    turns: int
    servings: int
    board: int | None
    cls: str = ""
    fdwr: list[str] = field(default_factory=list)
    search: list[str] = field(default_factory=list)
    search_first: bool = False
    search_outside: bool = False
    as9: bool = False
    unasked: bool = False
    sends: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in self.__dict__.items()}


def classify(path: Path, resolved: set[str] | None = None) -> Run:
    path = Path(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    probe, n = path.parent.name.rsplit("-", 1)
    servings = saved.get("servings") or []
    calls = [c for s in servings for c in s.get("tool_calls") or []] if servings else list(saved.get("tool_calls") or [])
    sends = [c for c in calls if "agentchat send" in c and "--help" not in c]
    run = Run(path=path.parent, arm=path.parent.parent.name, probe=probe, n=int(n), passed=bool(saved.get("passed")),
              cost=float(saved.get("cost_usd") or 0), turns=int(saved.get("turns") or 0),
              servings=len(servings) or 1, board=saved.get("board_version"))
    resolved = _resolved_topics() if resolved is None else resolved
    for call in sends:
        target = send_target(call)
        if target is None:
            continue
        run.sends.append(f"{target[0]} › {target[1]}")
        topic = target[1][2:] if target[1].startswith("✔ ") else target[1]
        if topic.strip() in resolved:
            run.fdwr.append(f"{target[0]} › {target[1]}")
    run.fdwr = sorted(set(run.fdwr))
    first = next((i for i, c in enumerate(calls) if "agentchat" in c), len(calls))
    for i, call in enumerate(calls):
        tool, _, rest = call.partition(": ")
        if tool in ("Glob", "Grep", "LS") or (tool == "Bash" and "agentchat" not in call and SEARCH.search(rest)):
            run.search.append(call[:160])
            run.search_first |= i < first
            run.search_outside |= _leaves(rest, run.path.resolve())
    reply = saved.get("reply") or ""
    run.unasked = bool(sends) and probe not in DELEGATION
    if probe in DELEGATION:
        doors = saved.get("doors")
        if doors is not None:
            taken = {d["door"] for d in doors}
            run.cls = ("reached" if "answer" in taken else "new request" if "new" in taken
                       else "wrong door" if sends else "")
        else:
            run.cls = "reached" if len(servings) >= 2 else ("wrong door" if sends else "")
        if not run.cls:
            run.cls = "proposed" if PROPOSES.search(reply) else "none"
        run.as9 = not sends and bool(WAITS_TO_BE_ASKED.search(reply))
    return run


def _leaves(command: str, home: Path) -> bool:
    """A search whose path is outside the run's own directory."""
    for token in re.findall(r"(?:^|[\s=])(/[^\s;|&\"')]*|\.\.(?:/[^\s;|&\"')]*)?|~[^\s;|&\"')]*)", command):
        if token.startswith("/dev/"):
            continue
        if token.startswith("/") and Path(token).is_relative_to(home):
            continue
        if len(token) > 1 and command.rstrip().endswith(token) and home.as_posix().startswith(token):
            continue  # a saved call cut short on its way into the run's directory
        return True
    return False


def load_runs(out: Path) -> tuple[list[Run], list[Path]]:
    """Every judged run under `out` (`<arm>/<probe>-<n>/outcome.json`), and
    the ones left out as cut by the account's limit."""
    resolved = _resolved_topics()
    runs, cut = [], []
    for path in sorted(Path(out).glob("*/*/outcome.json")):
        if path.parent.parent.name == "cut" or not re.search(r"-\d+$", path.parent.name):
            continue
        if json.loads(path.read_text(encoding="utf-8")).get("dry_run"):
            continue
        if is_cut(path.parent):
            cut.append(path.parent)
            continue
        runs.append(classify(path, resolved))
    return runs, cut


def table(runs: list[Run]) -> dict:
    """Pass counts per probe and arm with Wilson intervals, and the process
    measures per arm."""
    cells: dict[tuple[str, str], list[Run]] = defaultdict(list)
    for run in runs:
        cells[(run.probe, run.arm)].append(run)
    rates = []
    for (probe, arm), rs in sorted(cells.items()):
        k, n = sum(r.passed for r in rs), len(rs)
        lo, hi = wilson(k, n)
        rates.append({"probe": probe, "arm": arm, "passed": k, "runs": n, "low": lo, "high": hi,
                      "cost": round(sum(r.cost for r in rs), 2), "turns": [min(r.turns for r in rs), max(r.turns for r in rs)],
                      "failed": sorted(r.n for r in rs if not r.passed)})
    arms = sorted({r.arm for r in runs})
    process = {}
    for arm in arms:
        rs = [r for r in runs if r.arm == arm]
        measure = {}
        for name, hit in (("fdwr", lambda r: bool(r.fdwr)), ("unasked", lambda r: r.unasked),
                          ("search", lambda r: bool(r.search)),
                          ("search_first", lambda r: r.search_first), ("search_outside", lambda r: r.search_outside)):
            hits = [r for r in rs if hit(r)]
            lo, hi = wilson(len(hits), len(rs))
            measure[name] = {"runs": len(hits), "of": len(rs), "low": lo, "high": hi,
                             "which": [f"{r.probe}-{r.n}" for r in hits]}
        process[arm] = measure
    delegation = {}
    for probe in DELEGATION:
        for arm in arms:
            rs = [r for r in runs if r.probe == probe and r.arm == arm]
            if not rs:
                continue
            classes: dict[str, list[int]] = defaultdict(list)
            for r in sorted(rs, key=lambda r: r.n):
                classes[r.cls].append(r.n)
            delegation[f"{probe} {arm}"] = {"classes": dict(classes),
                                            "as9": [r.n for r in rs if r.as9], "runs": len(rs)}
    return {"rates": rates, "process": process, "delegation": delegation,
            "cost": round(sum(r.cost for r in runs), 2), "runs": len(runs)}
