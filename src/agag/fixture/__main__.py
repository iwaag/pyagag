"""`python -m agag.fixture build|consistency|probes|check|rejudge|reader` — see `agag.fixture`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .board import BOARD_VERSION, build_store
from .probes import DRIVERS, PROBES, judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agag.fixture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="write the fixture board as <dir>/mirror.sqlite, from scratch")
    build.add_argument("directory", type=Path)
    consistency = sub.add_parser("consistency", help="what a built board says against what it records "
                                                      "(exit 1 on any disagreement)")
    consistency.add_argument("directory", type=Path, help="holding mirror.sqlite")
    sub.add_parser("probes", help="the probes and their pass rules")
    check = sub.add_parser("check", help="judge a reply (a file) against one probe's rule")
    check.add_argument("probe", choices=sorted(PROBES))
    check.add_argument("reply", type=Path)
    rejudge = sub.add_parser("rejudge", help="today's rules over saved outcome.json files (no model runs)")
    rejudge.add_argument("directories", type=Path, nargs="+", help="trial --out directories, or trees of them")
    rejudge.add_argument("--json", type=Path, help="write every result here")
    reader = sub.add_parser("reader", help="the claims reader on fixed cases: right readings and seconds per reading")
    reader.add_argument("--runs", type=int, default=4, help="readings per case (default 4)")
    reader.add_argument("--model", default="", help="another model than the host's claims.toml names")
    reader.add_argument("--url", default="", help="another ollama than the host's claims.toml names")
    reader.add_argument("--json", type=Path, help="write every reading here")
    args = parser.parse_args(argv)
    if args.command == "reader":
        return _reader(args)
    if args.command == "rejudge":
        return _rejudge(args)
    if args.command == "build":
        print(build_store(args.directory))
        print(f"board version {BOARD_VERSION}")
    elif args.command == "consistency":
        from .consistency import problems

        found = problems(args.directory / "mirror.sqlite")
        for line in found:
            print(line)
        print(f"{len(found)} problem(s)")
        return 1 if found else 0
    elif args.command == "probes":
        for probe in PROBES.values():
            print(f"{probe.name} — {probe.agent}/{probe.role} in #{probe.channel} › {probe.topic}")
            if probe.text:
                print(f"  asks: {probe.text}")
            print(f"  must: " + "; ".join(" / ".join(s) for s in probe.must))
            if probe.must_not:
                print(f"  must not: " + ", ".join(probe.must_not))
            if probe.must_not_patterns:
                print(f"  must not match: " + ", ".join(probe.must_not_patterns))
            print(f"  {probe.note}")
            print(f"  run: cd {probe.agent} && .venv/bin/python -m {DRIVERS[probe.agent]} {probe.name} --out <dir>")
    else:
        print(json.dumps(judge(PROBES[args.probe], args.reply.read_text(encoding="utf-8")), ensure_ascii=False,
                         indent=1))
    return 0


def _rejudge(args) -> int:
    from .rejudge import rejudge_all

    results = rejudge_all(args.directories)
    common = Path(*Path(str(args.directories[0])).resolve().parts[:-1]) if len(args.directories) == 1 else None
    for r in results:
        name = str(r.path.parent.relative_to(common)) if common and r.path.is_relative_to(common) else str(r.path.parent)
        verdict = r.skipped or (f"{_word(r.before)} → {_word(r.after)}" + ("  CHANGED" if r.changed else ""))
        print(f"{name}  [{r.probe}, board {r.board}]  {verdict}")
        if r.changed:
            print(f"    before: missing {r.old_missing} against {r.old_against}")
            print(f"    now:    missing {r.missing} against {r.against}")
    judged = [r for r in results if r.after is not None]
    by_probe: dict[str, list] = {}
    for r in judged:
        by_probe.setdefault(r.probe, []).append(r)
    print()
    print(f"{'probe':<24} runs  before  now")
    for probe, rs in sorted(by_probe.items()):
        print(f"{probe:<24} {len(rs):>4}  {sum(r.before for r in rs):>6}  {sum(bool(r.after) for r in rs):>3}")
    print(f"{'all':<24} {len(judged):>4}  {sum(r.before for r in judged):>6}  {sum(bool(r.after) for r in judged):>3}"
          f"   ({sum(r.changed for r in judged)} changed, {len(results) - len(judged)} under another rule)")
    if args.json:
        args.json.write_text(json.dumps([r.as_dict() for r in results], ensure_ascii=False, indent=1) + "\n",
                             encoding="utf-8")
    return 0


def _word(passed) -> str:
    return "pass" if passed else "fail"


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
