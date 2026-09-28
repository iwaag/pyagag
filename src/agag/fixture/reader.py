"""The claims reader, measured on fixed cases (`agent_guide` p3 step 4).

failsafe p7 chose its reader (`agag.claims.OllamaReader` on the host's
local model) by running 13 cases through it four times and timing each
reading, with a script kept outside git. When the host's model changes
(`~/.config/agag/claims.toml`), the same measurement is this:

    python -m agag.fixture reader [--runs 4] [--model <name>] [--url <ollama>] [--json <file>]

Each case (`reader_cases.json`) is a reply and the set of acts the reader
should list for it. The reader is given what the listener gives it — the
reply's words (`agag.claims.reply_words`) under `READER_SYSTEM` — so a
reading here is a reading there. A case is right when the set of acts read
equals the set wanted; the reader's targets and quotes are kept in the
JSON, not judged. Nothing is written anywhere but `--json`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from importlib import resources

from ..claims import ClaimCheck, OllamaReader, Reader, ReaderError, reply_words


@dataclass(frozen=True)
class Case:
    name: str
    want: frozenset[str]
    text: str
    note: str = ""


def cases() -> list[Case]:
    document = json.loads(resources.files(__package__).joinpath("reader_cases.json").read_text(encoding="utf-8"))
    return [Case(row["name"], frozenset(row["want"]), row["text"], row.get("note", "")) for row in document["cases"]]


def measure(reader: Reader, runs: int = 4, *, only: list[Case] | None = None, clock=time.monotonic,
            echo=None) -> dict:
    """Every case `runs` times. A reading that fails is `error`, never an
    empty (right-looking) list."""
    rows = []
    for case in only if only is not None else cases():
        readings = []
        for _ in range(runs):
            started = clock()
            try:
                claims = reader.read(reply_words(case.text))
                got, error = sorted({claim.act for claim in claims}), ""
            except ReaderError as failure:
                claims, got, error = [], [], str(failure)
            seconds = round(clock() - started, 2)
            right = not error and set(got) == case.want
            readings.append({"acts": got, "right": right, "seconds": seconds, "error": error,
                             "claims": [claim.as_dict() for claim in claims]})
            if echo:
                shown = "ERROR " + error[:80] if error else ",".join(got) or "-"
                echo(f"{case.name:16s} want={','.join(sorted(case.want)) or '-':22s} got={shown:22s} "
                     f"{seconds:5.1f}s{'' if right else '  WRONG'}")
        rows.append({"case": case.name, "want": sorted(case.want), "readings": readings})
    all_readings = [reading for row in rows for reading in row["readings"]]
    seconds = [reading["seconds"] for reading in all_readings]
    return {"cases": len(rows), "runs": runs, "right": sum(r["right"] for r in all_readings),
            "readings": len(all_readings), "errors": sum(bool(r["error"]) for r in all_readings),
            "seconds_min": min(seconds, default=0.0), "seconds_max": max(seconds, default=0.0),
            "rows": rows}


def host_reader(model: str = "", url: str = "", timeout: float = 0.0) -> Reader | str:
    """The host's reader, with any of its settings replaced; a string says
    why there is none."""
    check = ClaimCheck.from_host()
    configured = check.reader if isinstance(check.reader, OllamaReader) else None
    url = url or (configured.url if configured else "")
    model = model or (configured.model if configured else "")
    if not url or not model:
        return check.problem or "no reader url and model"
    return OllamaReader(url, model, timeout or (configured.timeout if configured else 60.0))
