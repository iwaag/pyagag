"""Judge saved results again under today's rules (`agent_guide` p3 ex1).

An `outcome.json` keeps everything a probe's rule reads — the last reply,
every serving's tool calls, the posts sent and how many servings there
were — so a corrected rule can be applied to old runs without running a
model. p3 left four correct answers failing on spellings its rules did not
hold, and kept the rules as they were so the counts stayed comparable; with
this, the counts under both rules are one command.

    python -m agag.fixture rejudge <out-dir>…    # every outcome.json below each directory

A result is only re-judged when today's rule could have judged it: every
fact the saved verdict names must still be a fact of the rule (the same
spellings, or more of them). One judged by another question — the first
`delegate-answer` asked about the control loop, "4 hours / 20 s" — is
listed as `other rule` and left out of the counts. A result from an earlier
board is judged in that board's mission names (`probes.for_board`: board 1's
m20402 is board 2's m20510); `board_version` is absent, and so 1, before
board 2. Dry runs are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .probes import PROBES, for_board, judge, rule_digest

__all__ = ["Rejudged", "rejudge", "rejudge_all"]

_PREFIXES = ("tool call: ", "sent: ")


@dataclass
class Rejudged:
    path: Path
    probe: str
    board: int
    before: bool
    after: bool | None
    #: Why today's rule cannot judge it (`other rule: …`), or "".
    skipped: str = ""
    missing: list[str] = field(default_factory=list)
    against: list[str] = field(default_factory=list)
    old_missing: list[str] = field(default_factory=list)
    old_against: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.after is not None and self.after != self.before

    def as_dict(self) -> dict:
        return {"path": str(self.path), "probe": self.probe, "board": self.board, "before": self.before,
                "after": self.after, "skipped": self.skipped, "missing": self.missing, "against": self.against,
                "old_missing": self.old_missing, "old_against": self.old_against}


def _groups(probe) -> list[set[str]]:
    return ([set(g) for g in probe.must] + [set(g) for g in probe.tools_must]
            + [set(g) for g in probe.sends_must])


def _foreign(saved: dict, probe) -> str:
    """A fact the saved verdict names that today's rule does not hold."""
    if saved.get("rule") and saved["rule"] == rule_digest(probe):
        return ""
    groups = _groups(probe)
    for hit in saved.get("met") or []:
        if not any(hit in group for group in groups):
            return f"met {hit!r}"
    for entry in saved.get("missing") or []:
        if entry.startswith("servings: "):
            continue
        text = next((entry[len(p):] for p in _PREFIXES if entry.startswith(p)), entry)
        spellings = set(text.split(" / "))
        if not any(spellings <= group for group in groups):
            return f"missing {entry!r}"
    return ""


def rejudge(path: Path) -> Rejudged | None:
    """One saved outcome under today's rule; None for a dry run or a file
    that is not a probe's outcome."""
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    probe = PROBES.get(str(saved.get("probe") or ""))
    if probe is None or saved.get("dry_run"):
        return None
    board = int(saved.get("board_version") or 1)
    probe = for_board(probe, board)
    result = Rejudged(path=Path(path), probe=probe.name, board=board,
                      before=bool(saved.get("passed")), after=None,
                      old_missing=list(saved.get("missing") or []), old_against=list(saved.get("against") or []))
    foreign = _foreign(saved, probe)
    if foreign:
        result.skipped = f"other rule: {foreign}"
        return result
    servings = saved.get("servings")
    verdict = judge(probe, str(saved.get("reply") or ""), list(saved.get("tool_calls") or []),
                    sends=list(saved.get("sends") or []), servings=len(servings) if servings else 1)
    result.after, result.missing, result.against = verdict["passed"], verdict["missing"], verdict["against"]
    return result


def rejudge_all(directories: list[Path]) -> list[Rejudged]:
    found: list[Rejudged] = []
    for directory in directories:
        paths = [Path(directory)] if Path(directory).is_file() else sorted(Path(directory).rglob("outcome.json"))
        for path in paths:
            if "records" in path.parts or "overlay" in path.parts:
                continue
            result = rejudge(path)
            if result is not None:
                found.append(result)
    return found
