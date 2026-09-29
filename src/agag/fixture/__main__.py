"""`python -m agag.fixture build|consistency|probes|check|rejudge|doors|batch|classify|table|reader` — see `agag.fixture`."""

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
    doors = sub.add_parser("doors", help="saved scripted trials' sends put through today's responder doors "
                                         "(which runs a door change would have treated differently)")
    doors.add_argument("directories", type=Path, nargs="+", help="trial --out directories, or trees of them")
    batch = sub.add_parser("batch", help="probes × arms × runs from a plan, interleaved, resumable, behind a budget "
                                         "gate (agent_guide p3 ex2)")
    batch.add_argument("plan", type=Path, help="the batch plan (TOML; see agag.fixture.batch)")
    batch.add_argument("--out", type=Path, required=True, help="<out>/<arm>/<probe>-<n>/ per job")
    batch.add_argument("--jobs", type=int, default=2, help="jobs at a time (default 2)")
    batch.add_argument("--probe", action="append", help="only these probes (repeatable)")
    batch.add_argument("--dry-run", action="store_true", help="pass --dry-run to every driver; no budget read")
    batch.add_argument("--list", action="store_true", help="print the job order and what is done, run nothing")
    classify = sub.add_parser("classify", help="one line per run: verdict, delegation class, fd-wr, filesystem "
                                               "reach, as9")
    classify.add_argument("out", type=Path)
    classify.add_argument("--probe", action="append", help="only these probes (repeatable)")
    table = sub.add_parser("table", help="pass counts per probe and arm with Wilson 95 %% intervals, and the "
                                         "process measures")
    table.add_argument("out", type=Path)
    table.add_argument("--json", type=Path, help="write the table here")
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
    if args.command == "doors":
        return _doors(args)
    if args.command == "batch":
        return _batch(args)
    if args.command == "classify":
        return _classify(args)
    if args.command == "table":
        return _table(args)
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


def _doors(args) -> int:
    from .responder import replay

    changed = 0
    for path in sorted({p for d in args.directories for p in ([d / "outcome.json"] if (d / "outcome.json").is_file()
                                                               else Path(d).rglob("outcome.json"))}):
        saved = json.loads(path.read_text(encoding="utf-8"))
        probe = PROBES.get(saved.get("probe", ""))
        if probe is None or not probe.script or not (path.parent / "overlay" / "mirror.sqlite").is_file():
            continue
        sends = replay(path.parent, probe.script)
        differs = any(s["changed"] for s in sends)
        changed += differs
        print(f"{'CHANGED' if differs else 'same   '} {path.parent}  [{probe.name}, "
              f"{'pass' if saved.get('passed') else 'fail'}]")
        for s in sends:
            print(f"    #{s['id']} {s['channel']} › {s['topic']}: then {s['then']}, now {s['now']}")
    print(f"{changed} run(s) a door change treats differently")
    return 0


def _batch(args) -> int:
    from .batch import load_plan, run_batch

    plan = load_plan(args.plan)
    if args.list:
        for job in plan.jobs():
            done = (args.out / job.arm / f"{job.probe}-{job.n}" / "outcome.json").is_file()
            if not args.probe or job.probe in args.probe:
                print(f"{'done ' if done else '     '}{job.name}")
        return 0
    tally = run_batch(plan, args.out, jobs=args.jobs, dry_run=args.dry_run,
                      only=set(args.probe) if args.probe else None)
    return 1 if tally["failed"] else 0


def _classify(args) -> int:
    from .tally import load_runs

    runs, cut = load_runs(args.out)
    for r in sorted(runs, key=lambda r: (r.probe, r.arm, r.n)):
        if args.probe and r.probe not in args.probe:
            continue
        marks = [m for m in (r.cls, "as9" if r.as9 else "", f"fd-wr {r.fdwr}" if r.fdwr else "",
                             "search" + ("/first" if r.search_first else "") + ("/outside" if r.search_outside else "")
                             if r.search else "") if m]
        print(f"{r.arm}/{r.probe}-{r.n}  {'PASS' if r.passed else 'fail'}  s{r.servings} t{r.turns} ${r.cost:.2f}  "
              + "  ".join(marks))
        for send in r.sends:
            print(f"      send → {send}")
    for path in cut:
        print(f"CUT {path}")
    return 0


def _table(args) -> int:
    from .tally import load_runs, table

    runs, cut = load_runs(args.out)
    found = table(runs)
    print(f"{'probe':<22} {'arm':<8} pass   interval      turns   cost  failed")
    for row in found["rates"]:
        print(f"{row['probe']:<22} {row['arm']:<8} {row['passed']:>2}/{row['runs']:<3} [{row['low']:4.0%}, "
              f"{row['high']:4.0%}]  {row['turns'][0]:>2}–{row['turns'][1]:<3} ${row['cost']:6.2f}  "
              f"{','.join(map(str, row['failed']))}")
    print()
    for arm, measure in found["process"].items():
        print(f"{arm:<8} " + "  ".join(f"{name} {m['runs']}/{m['of']} [{m['low']:.0%}, {m['high']:.0%}]"
                                       for name, m in measure.items()))
    print()
    for key, row in found["delegation"].items():
        print(f"{key:<28} " + "  ".join(f"{cls}={len(ns)} ({','.join(map(str, ns))})"
                                        for cls, ns in sorted(row["classes"].items()))
              + (f"  as9={row['as9']}" if row["as9"] else ""))
    print(f"\n{found['runs']} runs, ${found['cost']:.2f}; {len(cut)} cut (not judged)")
    if args.json:
        found["cut"] = [str(p) for p in cut]
        args.json.write_text(json.dumps(found, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
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
