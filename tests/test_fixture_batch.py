"""agent_guide p3 ex2 step 2: the batch, its budget gate, and the tally."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from agag.fixture.batch import Job, is_cut, load_plan, order, run_batch
from agag.fixture.tally import classify, load_runs, send_target, table, wilson

LIMIT = "You've hit your session limit · resets 7am (Asia/Tokyo)"


def _plan(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "plan.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_the_order_interleaves_runs_and_rotates_the_arms(tmp_path):
    plan = load_plan(_plan(tmp_path, """
        [arms]
        before = ["--guides-rev", "ba28e90^", "--no-shared"]
        p1 = ["--guides-rev", "447bb03", "--no-shared"]
        now = []
        [[probes]]
        names = ["delegate-answer"]
        runs = 2
        [[probes]]
        names = ["growbox-thing"]
        runs = 1
        arms = ["now"]
    """))
    assert order(plan) == [
        Job("before", "delegate-answer", 1), Job("p1", "delegate-answer", 1), Job("now", "delegate-answer", 1),
        Job("now", "growbox-thing", 1),
        Job("p1", "delegate-answer", 2), Job("now", "delegate-answer", 2), Job("before", "delegate-answer", 2)]
    prefix, cwd = plan.driver("agfront")
    assert prefix[1:] == ["-m", "agfront.trial"] and cwd == tmp_path


def test_a_limit_reply_is_a_cut_not_a_verdict(tmp_path):
    run = tmp_path / "now" / "growbox-thing-1"
    run.mkdir(parents=True)
    (run / "outcome.json").write_text(json.dumps({"reply": LIMIT, "passed": False}), encoding="utf-8")
    assert is_cut(run).startswith("reply: You've hit your session limit")
    (run / "outcome.json").write_text(json.dumps({"reply": "the study is pj-growbox", "passed": True}))
    assert is_cut(run) == ""
    crashed = tmp_path / "now" / "growbox-thing-2"
    Path(f"{crashed}.log").write_text("Error: Claude AI usage limit reached|1790000000\n", encoding="utf-8")
    assert is_cut(crashed).startswith("log:")


STUB_REPLY = '''
import os, sys
from pathlib import Path
sys.stdin.read()
first = Path(__file__).with_name("limit-once")
try:
    os.close(os.open(first, os.O_CREAT | os.O_EXCL))
    print(%r)
except FileExistsError:
    print("growbox は pj-growbox の調査です。")
''' % LIMIT

STUB_DRIVER = '''
import sys
from pathlib import Path
from agag.agent import AgentSpec, run_role
from agag.fixture.run import Trial, trial_parser

ROOT = Path(__file__).with_name("checkout")
args = trial_parser("stub", "", "agfront").parse_args(sys.argv[1:])
trial = Trial.start(args, ROOT)
with trial.session():
    spec = AgentSpec("front", ROOT)
    output, record, code = run_role(spec, "desk", "prompt", cwd=trial.out, timeout=30,
                                    record=spec.records_root / "desk" / "run-0001.json")
sys.exit(trial.finish(output, records=spec.records_root / "desk"))
'''

#: The budget as `agbudget --json` prints it: the first reading is over the
#: limit (the gate pauses), every later one under it.
STUB_BUDGET = '''
import json, os
from pathlib import Path
seen = Path(__file__).with_name("budget-read")
percent = 10.0 if seen.exists() else 75.0
seen.touch()
print(json.dumps({"schema": "ag.budget.v1", "harnesses": {"claude_code": {"ok": True, "windows": [
    {"kind": "session", "percent": percent, "resets_at": 1790667000},
    {"kind": "weekly_all", "percent": 50.0, "resets_at": 1791007200}]}}}))
'''


def test_a_tiny_batch_runs_end_to_end_with_the_stub_profile(tmp_path):
    """Two arms × two runs of growbox-thing on the `fake` harness: the gate
    pauses on the first reading and resumes, the first reply is the limit
    message (cut, kept, queued again), and a second invocation resumes by
    skipping what is done."""
    checkout = tmp_path / "checkout"
    (checkout / ".local").mkdir(parents=True)
    reply = tmp_path / "reply.py"
    reply.write_text(f"#!{sys.executable}\n{STUB_REPLY}", encoding="utf-8")
    reply.chmod(0o755)
    (checkout / "agents.toml").write_text(textwrap.dedent('''
        schema = "ag.agent-config.v2"
        [models."ollama/test"]
        [profiles.stub]
        harness = "fake"
        model = "ollama/test"
        [roles.desk]
        profile = "stub"
        allowed_tools = "Read"
    '''), encoding="utf-8")
    (checkout / ".local" / "agents.local.toml").write_text(
        f'schema = "ag.agent-config.v2"\n[local.harness.fake]\ncommand = "{reply}"\n', encoding="utf-8")
    (tmp_path / "driver.py").write_text(STUB_DRIVER, encoding="utf-8")
    (tmp_path / "budget.py").write_text(STUB_BUDGET, encoding="utf-8")
    plan = load_plan(_plan(tmp_path, f"""
        [arms]
        a = []
        b = ["--no-shared"]
        [[probes]]
        names = ["growbox-thing"]
        runs = 2
        [drivers.agfront]
        command = ["{sys.executable}", "{tmp_path / 'driver.py'}"]
        [budget]
        harness = "claude_code"
        limits = {{ session = 60, weekly_all = 95 }}
        poll_seconds = 0
        command = ["{sys.executable}", "{tmp_path / 'budget.py'}"]
    """))
    out = tmp_path / "out"
    lines: list[str] = []
    tally = run_batch(plan, out, jobs=2, echo=lines.append, sleep=lambda s: None)
    assert tally == {"planned": 4, "skipped": 0, "done": 4, "cut": 1, "failed": 0, "passed": 0, "paused_s": tally["paused_s"]}
    log = (out / "batch.log").read_text(encoding="utf-8")
    assert "pause: session 75 % ≥ 60 %" in log and "resume: session 10 %, weekly_all 50 %" in log
    assert "; queued again" in log
    [kept] = list((out / "cut").glob("*/growbox-thing-*.1"))
    assert LIMIT in (kept / "outcome.json").read_text(encoding="utf-8")
    for arm in ("a", "b"):
        for n in (1, 2):
            saved = json.loads((out / arm / f"growbox-thing-{n}" / "outcome.json").read_text(encoding="utf-8"))
            assert "pj-growbox" in saved["reply"] and saved["shared"] == (arm == "a")
    runs, cut = load_runs(out)
    assert len(runs) == 4 and cut == []
    assert table(runs)["rates"][0]["runs"] == 2  # per arm; the stub's reply is judged, not passed
    again = run_batch(plan, out, jobs=2, echo=lines.append, sleep=lambda s: None)
    assert (again["skipped"], again["done"], again["cut"]) == (4, 0, 0)


def test_the_tally_classifies_by_calls(tmp_path):
    run = tmp_path / "now" / "delegate-decision-3"
    run.mkdir(parents=True)

    def save(**saved):
        base = {"passed": False, "reply": "", "servings": [{"tool_calls": []}]}
        (run / "outcome.json").write_text(json.dumps({**base, **saved}, ensure_ascii=False), encoding="utf-8")
        return classify(run / "outcome.json", {"workplan-growbox-germination-days"})

    ask = "Bash: agentchat send pj-growbox workplan-growbox-lighting-hours --to autolab-agstudio1 \"decide\""
    r = save(servings=[{"tool_calls": [ask]}, {"tool_calls": []}],
             doors=[{"door": "new", "channel": "pj-growbox", "topic": "workplan-growbox-lighting-hours"}])
    assert r.cls == "new request" and r.sends == ["pj-growbox › workplan-growbox-lighting-hours"]
    r = save(servings=[{"tool_calls": [ask]}, {"tool_calls": []}], doors=[{"door": "answer"}])
    assert r.cls == "reached"
    r = save(reply="もし autolab が複数の選択肢を示して選ぶよう求めてきた場合は、電気代を抑えるほうを選んで答えます。")
    assert (r.cls, r.as9) == ("none", True)
    r = save(reply="この進め方でよければ、そのまま進めます。")
    assert (r.cls, r.as9) == ("proposed", False)
    fdwr = "Bash: agentchat send pj-growbox workplan-growbox-germination-days --intent report \"また続き\""
    r = save(servings=[{"tool_calls": ["Bash: find / -iname x.md 2>/dev/null", "Bash: agentchat read front x", fdwr]}])
    assert r.fdwr == ["pj-growbox › workplan-growbox-germination-days"]
    assert r.search and r.search_first and r.search_outside and r.cls == "wrong door"
    body = "Bash: agentchat send autolab-agstudio1 q --re 5 \"the workplan-growbox-germination-days topic\""
    assert send_target(body) == ("autolab-agstudio1", "q")
    assert wilson(12, 12)[0] > 0.75 and wilson(0, 12)[1] < 0.25
