"""`python -m agag.fixture build|probes|check` — see `agag.fixture`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .board import build_store
from .probes import PROBES, judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agag.fixture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="write the fixture board as <dir>/mirror.sqlite, from scratch")
    build.add_argument("directory", type=Path)
    sub.add_parser("probes", help="the probes and their pass rules")
    check = sub.add_parser("check", help="judge a reply (a file) against one probe's rule")
    check.add_argument("probe", choices=sorted(PROBES))
    check.add_argument("reply", type=Path)
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build_store(args.directory))
    elif args.command == "probes":
        for probe in PROBES.values():
            print(f"{probe.name} — {probe.agent}/{probe.role} in #{probe.channel} › {probe.topic}")
            if probe.text:
                print(f"  asks: {probe.text}")
            print(f"  must: " + "; ".join(" / ".join(s) for s in probe.must))
            if probe.must_not:
                print(f"  must not: " + ", ".join(probe.must_not))
            print(f"  {probe.note}")
    else:
        print(json.dumps(judge(PROBES[args.probe], args.reply.read_text(encoding="utf-8")), ensure_ascii=False,
                         indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
