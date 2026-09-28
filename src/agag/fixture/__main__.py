"""`python -m agag.fixture build|probes|check|reader` — see `agag.fixture`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .board import build_store
from .probes import DRIVERS, PROBES, judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agag.fixture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="write the fixture board as <dir>/mirror.sqlite, from scratch")
    build.add_argument("directory", type=Path)
    sub.add_parser("probes", help="the probes and their pass rules")
    check = sub.add_parser("check", help="judge a reply (a file) against one probe's rule")
    check.add_argument("probe", choices=sorted(PROBES))
    check.add_argument("reply", type=Path)
    reader = sub.add_parser("reader", help="the claims reader on fixed cases: right readings and seconds per reading")
    reader.add_argument("--runs", type=int, default=4, help="readings per case (default 4)")
    reader.add_argument("--model", default="", help="another model than the host's claims.toml names")
    reader.add_argument("--url", default="", help="another ollama than the host's claims.toml names")
    reader.add_argument("--json", type=Path, help="write every reading here")
    args = parser.parse_args(argv)
    if args.command == "reader":
        return _reader(args)
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
            print(f"  run: cd {probe.agent} && .venv/bin/python -m {DRIVERS[probe.agent]} {probe.name} --out <dir>")
    else:
        print(json.dumps(judge(PROBES[args.probe], args.reply.read_text(encoding="utf-8")), ensure_ascii=False,
                         indent=1))
    return 0


def _reader(args) -> int:
    from .reader import host_reader, measure

    found = host_reader(args.model, args.url)
    if isinstance(found, str):
        print(f"no reader: {found}", file=sys.stderr)
        return 2
    print(f"reader {found.model} at {found.url}, {args.runs} run(s) per case")
    result = measure(found, args.runs, echo=print)
    result["model"], result["url"] = found.model, found.url
    print(f"{result['right']} of {result['readings']} readings right, {result['errors']} errors, "
          f"{result['seconds_min']}–{result['seconds_max']} s per reading")
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
