"""failsafe p2 step 2: the live execution record and the health probe."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
import textwrap
from pathlib import Path

from agag import execution, health
from agag.agent_config import ResolvedAgent
from agag.execution import LiveExecution
from agag.harness import run_harness


def stub(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def claude(command: Path) -> ResolvedAgent:
    return ResolvedAgent("coding", "p", "claude_code", "anthropic", "anthropic/claude-sonnet-5", {}, str(command),
                         "", {})


STREAM = r'''
import json, sys, time
argv = sys.argv[1:]
sys.stdin.read()
def out(doc):
    print(json.dumps(doc), flush=True)
out({"type": "system", "subtype": "init"})
out({"type": "stream_event", "event": {"type": "content_block_delta"}})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                                    "input": {"command": "pytest -q"}}]}})
open(sys.argv[0] + ".argv", "w").write(json.dumps(argv))
out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
out({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Task",
                                                    "input": {"description": "run the suite"}}]}})
out({"type": "result", "result": "finished", "num_turns": 2})
'''


def test_a_run_keeps_a_live_record_of_its_process_tools_and_end(tmp_path):
    command = stub(tmp_path / "claude", STREAM)
    live = LiveExecution(tmp_path / "exec" / "r1.json", serving={"ack": 41, "channel": "c", "topic": "t"})
    result = run_harness(claude(command), "p", cwd=tmp_path, timeout=30, live=live,
                         transcript_path=tmp_path / "transcript.jsonl")

    assert result.output == "finished"
    doc = execution.read(tmp_path / "exec" / "r1.json")
    assert doc["serving"] == {"ack": 41, "channel": "c", "topic": "t"}
    assert doc["harness"] == "claude_code" and doc["pid"] and doc["tool_results"] is True
    assert doc["ended_at"] and doc["exit_code"] == 0 and doc["outcome"] == "done"
    # t1 returned; t2 (a subagent) never did before the end.
    assert list(doc["open_tools"]) == ["t2"] and doc["open_tools"]["t2"]["detail"] == "run the suite"
    assert doc["tools_done"] == 1 and doc["events"] >= 5
    # A live record asks claude_code for partial messages: a long generation
    # is progress. They are not kept in the output.
    assert "--include-partial-messages" in json.loads((tmp_path / "claude.argv").read_text())
    assert "stream_event" not in (tmp_path / "transcript.jsonl").read_text()


def test_without_a_live_record_nothing_changes_in_the_argv(tmp_path):
    command = stub(tmp_path / "claude", STREAM)
    run_harness(claude(command), "p", cwd=tmp_path, timeout=30, on_event=lambda e: None)
    assert "--include-partial-messages" not in json.loads((tmp_path / "claude.argv").read_text())


# --- the probe -------------------------------------------------------------------------------


def record(directory: Path, name: str, **fields) -> dict:
    doc = {"schema": execution.SCHEMA, "serving": {"ack": 41, "channel": "work-m1", "topic": "workrun-task1-m1"},
           "harness": "claude_code", "pid": 5000, "started_at": 1000.0, "deadline_at": 2200.0,
           "last_event_at": 1000.0, "last_event": "text", "events": 3, "tool_results": True, "open_tools": {},
           "tools_done": 0, "ended_at": None, "exit_code": None, "outcome": None, "written_at": 1000.0}
    for key, value in fields.items():
        if key == "serving":
            doc["serving"] = {**doc["serving"], **value}
        else:
            doc[key] = value
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(doc))
    return doc


def table(*rows):
    """`(pid, ppid, age, command)` rows as `process_table` returns them."""
    return {pid: {"ppid": ppid, "age": age, "command": command} for pid, ppid, age, command in rows}


def queue(path: Path, *, state="acked", ack=41, pending=(), delivered=None):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE pending (channel TEXT, topic TEXT, route TEXT, state TEXT);
        CREATE TABLE servings (id INTEGER PRIMARY KEY, channel TEXT, topic TEXT, route TEXT, state TEXT,
                               ack_id INTEGER, delivered_id INTEGER, failure TEXT, updated_at REAL);
    """)
    db.execute("INSERT INTO servings VALUES (1, 'work-m1', 'workrun-task1-m1', 'owner', ?, ?, ?, '', 0)",
               (state, ack, delivered))
    for row in pending:
        db.execute("INSERT INTO pending VALUES ('work-m1', 'workrun-task1-m1', 'owner', ?)", (row,))
    db.commit()
    db.close()
    return path


def probe(tmp_path, now, rows, **kwargs):
    return health.probe(tmp_path / "exec", ack=41, channel="work-m1", topic="workrun-task1-m1", now=now,
                        table=rows, **kwargs)


def test_a_harness_that_died_without_its_end_is_stopped(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1100.0)
    q = queue(tmp_path / "listener.sqlite", state="acked")
    report = probe(tmp_path, 1200.0, table((1, 0, 9999, "launchd")), queue=q)

    assert report["schema"] == health.SCHEMA
    assert report["process"]["state"] == "exited" and report["process"]["how"] == "not in the process table"
    assert report["verdict"] == "stopped", report["why"]
    assert "never recorded an end" in report["why"] and "journal: acked" in report["why"]
    assert report["progress"]["age_seconds"] == 100.0


def test_a_run_whose_serving_is_served_again_is_not_stopped(tmp_path):
    """A listener killed with its harness restarts and requeues the serving
    (`agag.listen`): nothing to recover."""
    record(tmp_path / "exec", "a.json")
    q = queue(tmp_path / "listener.sqlite", state="interrupted", pending=("pending",))
    assert probe(tmp_path, 1200.0, table(), queue=q)["verdict"] == "ended"


def test_a_run_that_ended_and_delivered_is_ended(tmp_path):
    record(tmp_path / "exec", "a.json", ended_at=1150.0, exit_code=0, outcome="done")
    q = queue(tmp_path / "listener.sqlite", state="delivered", delivered=990)
    report = probe(tmp_path, 1200.0, None, queue=q)
    assert report["verdict"] == "ended" and report["process"]["state"] == "exited"


def test_a_serving_that_ended_posting_nothing_is_stopped(tmp_path):
    """The `silent-exit` shape: the harness was killed and the serving
    ended with an empty reply — `delivered` with no message."""
    record(tmp_path / "exec", "a.json", ended_at=1150.0, exit_code=-9, outcome="failed")
    q = queue(tmp_path / "listener.sqlite", state="delivered")
    report = probe(tmp_path, 1200.0, None, queue=q)
    assert report["verdict"] == "stopped" and "delivered, nothing posted" in report["why"]


def test_a_run_that_ended_and_delivered_nothing_is_stopped(tmp_path):
    record(tmp_path / "exec", "a.json", ended_at=1150.0, exit_code=-9, outcome="failed")
    q = queue(tmp_path / "listener.sqlite", state="acked")
    assert probe(tmp_path, 1200.0, None, queue=q)["verdict"] == "stopped"


def test_a_recent_event_is_running(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1150.0)
    report = probe(tmp_path, 1200.0, table((5000, 1, 200, "claude -p")))
    assert report["verdict"] == "running" and report["process"]["state"] == "alive"


def test_a_quiet_tool_call_with_a_process_under_it_is_a_named_wait(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1000.0,
           open_tools={"t1": {"name": "Bash", "detail": "sleep 600", "since": 1000.0, "parent": None}})
    rows = table((5000, 1, 900, "claude -p"), (5001, 5000, 800, "/bin/zsh -c sleep 600"), (5002, 5001, 800, "sleep 600"))
    report = probe(tmp_path, 1900.0, rows)
    assert report["verdict"] == "waiting", report["why"]
    assert report["wait"]["kind"] == "tool" and report["wait"]["name"] == "Bash"
    assert [c["pid"] for c in report["wait"]["children"]] == [5001, 5002]


def test_background_processes_explain_a_quiet_run(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1000.0)
    rows = table((5000, 1, 900, "claude -p"), (5003, 5000, 600, "python train.py"))
    report = probe(tmp_path, 1900.0, rows)
    assert report["verdict"] == "waiting" and report["wait"]["kind"] == "children"


def test_alive_without_progress_or_a_named_wait_is_unknown(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1000.0)
    report = probe(tmp_path, 1400.0, table((5000, 1, 400, "claude -p")))
    assert report["verdict"] == "unknown" and "no tool call or child process explains" in report["why"]
    assert report["wait"]["kind"] == "none"


def test_alive_past_its_own_deadline_is_unknown_whatever_it_waits_on(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1000.0,
           open_tools={"t1": {"name": "Bash", "detail": "sleep 9999", "since": 1000.0, "parent": None}})
    report = probe(tmp_path, 2300.0, table((5000, 1, 1300, "claude -p")))
    assert report["verdict"] == "unknown" and "past the run's own deadline" in report["why"]


def test_a_reused_pid_is_not_the_run(tmp_path):
    record(tmp_path / "exec", "a.json")
    report = probe(tmp_path, 1200.0, table((5000, 1, 30, "some other thing")))
    assert report["process"]["state"] == "exited" and "later process" in report["process"]["how"]


def test_another_serving_s_record_is_never_applied(tmp_path):
    """Stale evidence: the conversation's previous serving ran and ended;
    this serving (ack 41) has no record of its own."""
    record(tmp_path / "exec", "old.json", serving={"ack": 30}, ended_at=900.0, exit_code=0, outcome="done")
    report = probe(tmp_path, 1200.0, table())
    assert report["verdict"] == "unknown"
    assert report["other_servings"][0]["ack"] == 30
    assert any("belong to other servings" in u for u in report["unknowns"])


def test_an_unreadable_process_table_is_an_unknown_not_a_death(tmp_path, monkeypatch):
    record(tmp_path / "exec", "a.json")
    monkeypatch.setattr(health, "process_table", lambda timeout=health.PS_TIMEOUT: None)
    report = health.probe(tmp_path / "exec", ack=41, now=1200.0)
    assert report["verdict"] == "unknown" and "the process table could not be read" in report["unknowns"]


def test_the_command_line_prints_one_document(tmp_path, capsys):
    record(tmp_path / "exec", "a.json", pid=os.getpid(), started_at=0.0, last_event_at=0.0, deadline_at=None)
    assert health.main(["--dir", str(tmp_path / "exec"), "--ack", "41", "--window", "1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema"] == health.SCHEMA and report["process"]["state"] == "alive"


def test_without_a_record_a_delivered_serving_is_ended(tmp_path):
    q = queue(tmp_path / "listener.sqlite", state="delivered", delivered=77)
    report = health.probe(tmp_path / "exec", ack=41, queue=q, now=1200.0)
    assert report["verdict"] == "ended" and "#77" in report["why"]


# --- work and housekeeping, and a wait's own bound (failsafe p3) ------------------------


def test_the_record_keeps_work_and_housekeeping_apart():
    clock = [1000.0]
    live = LiveExecution(Path("/nonexistent/never-written.json"), clock=lambda: clock[0])
    live.begin(harness="claude_code", pid=1, timeout=1200)
    clock[0] = 1010.0
    live.event({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sleep 300", "timeout": 400000}}]}})
    for at in (1060.0, 1120.0, 1180.0):
        clock[0] = at
        live.event({"type": "system", "subtype": "task_started"})
    clock[0] = 1190.0
    live.event({"type": "stream_event", "event": {"type": "ping"}})
    doc = live.doc
    assert doc["last_work_at"] == 1010.0 and doc["last_work"] == "tool Bash"
    assert doc["last_event_at"] == 1190.0 and doc["housekeeping"]["count"] == 4
    assert doc["housekeeping"]["last"] == "stream_event ping"
    assert doc["open_tools"]["t1"]["bound"] == 400.0
    clock[0] = 1200.0
    live.event({"type": "assistant", "parent_tool_use_id": "t9", "message": {"content": [{"type": "text", "text": "x"}]}})
    assert doc["last_work_at"] == 1200.0 and doc["last_work"].startswith("subagent:")


def test_a_tool_s_own_bound():
    bound = execution.tool_bound
    assert bound("Bash", {"command": "make"}) == 120.0
    assert bound("Bash", {"command": "make", "timeout": 600000}) == 600.0
    assert bound("Bash", {"command": "make", "timeout": 9_000_000}) == 600.0, "Claude Code's ceiling"
    assert bound("Bash", {"command": "serve", "run_in_background": True}) == 5.0
    assert bound("Task", {"description": "x"}) is None and bound("WebFetch", {"url": "u"}) is None


def test_housekeeping_alone_is_not_progress(tmp_path):
    """p2 trial B read `running` at every check because Claude Code emits
    `system` events while a long Bash call runs: the same events would
    certify a hung run for as long as the harness talks."""
    record(tmp_path / "exec", "a.json", last_event_at=1395.0, last_event="system task_started",
           last_work_at=1000.0, last_work="text", housekeeping={"count": 40, "last_at": 1395.0,
                                                               "last": "system task_started"})
    report = probe(tmp_path, 1400.0, table((5000, 1, 400, "claude -p")))
    assert report["verdict"] == "unknown", report["why"]
    assert "no work for 400 s (only housekeeping since: system task_started, 5 s ago)" in report["why"]
    assert report["progress"]["work_age_seconds"] == 400.0 and report["progress"]["age_seconds"] == 5.0


def test_a_bash_call_inside_its_bound_is_an_explained_wait_and_past_it_is_not(tmp_path):
    tool = {"t1": {"name": "Bash", "detail": "sleep 300", "since": 1010.0, "bound": 400.0, "parent": None}}
    record(tmp_path / "exec", "a.json", last_event_at=1390.0, last_work_at=1010.0, last_work="tool Bash",
           open_tools=tool)
    rows = {5000: {"ppid": 1, "age": 400, "cpu": 3.0, "command": "claude -p"},
            5001: {"ppid": 5000, "age": 380, "cpu": 0.02, "command": "/bin/zsh -c sleep 300"},
            5002: {"ppid": 5001, "age": 380, "cpu": 0.0, "command": "sleep 300"}}
    inside = probe(tmp_path, 1400.0, rows)
    assert inside["verdict"] == "waiting" and "within its bound of 400 s" in inside["why"]
    assert inside["wait"]["bound_seconds"] == 400.0 and inside["wait"]["cpu_seconds"] == 0.02
    later = 1010.0 + 400 + health.TOOL_GRACE + 1
    for row in rows.values():
        row["age"] += later - 1400.0
    past = probe(tmp_path, later, rows)
    assert past["verdict"] == "unknown" and "past its own bound of 400 s" in past["why"]
    assert past["wait"]["over_bound"] is True


def test_the_process_table_reads_cpu_time():
    assert health._cpu_seconds("0:00.03") == 0.03
    assert health._cpu_seconds("1:02:03.50") == 3723.5
    assert health._cpu_seconds("2-00:00:01") == 172801.0
    table_now = health.process_table()
    assert table_now and all("cpu" in row for row in table_now.values())


def test_a_trial_fault_beside_the_record_is_reported(tmp_path):
    record(tmp_path / "exec", "a.json", last_event_at=1100.0)
    (tmp_path / "exec" / "a.injected").write_text(json.dumps({"fault": "silent-exit", "at": 1101.0}))
    report = probe(tmp_path, 1200.0, table((1, 0, 9999, "launchd")))
    assert report["verdict"] == "stopped" and report["run"]["injected"] == {"fault": "silent-exit", "at": 1101.0}
    record(tmp_path / "exec", "a.json", last_event_at=1100.0)
    (tmp_path / "exec" / "a.injected").unlink()
    assert probe(tmp_path, 1200.0, table((1, 0, 9999, "launchd")))["run"]["injected"] is None


# --- a post nobody acknowledged yet: where is it in the queue (failsafe p5) -------------------


def listener_queue(path: Path):
    from agag.listen import Queue

    return Queue(path)


def growbox_task_running(tmp_path, *, last_event_at=1290.0, pid=5000):
    """autolab's executor serves growbox's task (acked #41) while worldtrend's
    plan conversation waits (trial B, #13702)."""
    q = listener_queue(tmp_path / "listener.sqlite")
    q.enqueue("work-m1", "workrun-task1-m1", "owner", revision=1, message_id=40, at=1000.0)
    entry = q.take(at=1001.0)
    q.open_serving(entry, at=1001.0).acked(41)
    q.enqueue("pj-w", "workplan-w", "owner", revision=2, message_id=70, at=1100.0)
    record(tmp_path / "exec", "a.json", last_event_at=last_event_at, last_work_at=last_event_at, pid=pid)
    return q


def queued_probe(tmp_path, now, rows, **kwargs):
    return health.probe_queue(tmp_path / "exec", channel="pj-w", topic="workplan-w",
                              queue=tmp_path / "listener.sqlite", since=1099.0, now=now, table=rows, **kwargs)


def test_a_post_behind_a_healthy_serving_is_queued_with_what_it_waits_behind(tmp_path):
    growbox_task_running(tmp_path)
    report = queued_probe(tmp_path, 1300.0, table((5000, 1, 299, "claude")))
    assert report["verdict"] == "queued", report["why"]
    facts = report["queue"]
    assert facts["enqueued_at"] == 1100.0 and facts["position"] == 1 and facts["served_since"] == 0
    (ahead,) = facts["ahead"]
    assert (ahead["channel"], ahead["ack"], ahead["verdict"]) == ("work-m1", 41, "running")
    assert "behind work-m1/workrun-task1-m1" in report["why"]


def test_a_post_behind_a_stopped_serving_is_not_excused(tmp_path):
    growbox_task_running(tmp_path)
    report = queued_probe(tmp_path, 1300.0, table())  # the task's harness is gone
    assert report["verdict"] == "unknown"
    assert report["queue"]["ahead"][0]["verdict"] == "stopped"
    assert "whose serving is stopped" in report["why"]


def test_a_post_the_idle_executor_does_not_pick_up_is_stopped(tmp_path):
    q = listener_queue(tmp_path / "listener.sqlite")
    q.enqueue("pj-w", "workplan-w", "owner", revision=2, message_id=70, at=1100.0)
    soon = queued_probe(tmp_path, 1100.0 + health.PICKUP_SECONDS - 5, table())
    assert soon["verdict"] == "unknown" and "should take it within seconds" in soon["why"]
    late = queued_probe(tmp_path, 1100.0 + health.PICKUP_SECONDS + 5, table())
    assert late["verdict"] == "stopped" and "has not picked the conversation up" in late["why"]


def test_a_post_passed_over_while_others_are_served_is_stopped(tmp_path):
    q = listener_queue(tmp_path / "listener.sqlite")
    q.enqueue("pj-w", "workplan-w", "mention", revision=2, message_id=70, at=1100.0)
    for n in range(health.PASSED_OVER):
        q.enqueue("front", f"front-{n}", "owner", revision=3 + n, message_id=80 + n, at=1110.0 + n)
        entry = q.take(at=1120.0 + n)
        q.open_serving(entry, at=1120.0 + n)
        q.drop(entry)
    report = queued_probe(tmp_path, 1200.0, table())
    assert report["verdict"] == "stopped" and "passed over" in report["why"]


def test_a_post_that_was_picked_up_answers_for_its_serving(tmp_path):
    q = growbox_task_running(tmp_path)
    q.drop(q.entries("running")[0])
    entry = q.take(at=1310.0)
    q.open_serving(entry, at=1310.0)
    report = queued_probe(tmp_path, 1312.0, table())
    assert report["verdict"] == "running" and "picked up" in report["why"]


def test_a_post_the_listener_never_took_in_is_unknown(tmp_path):
    listener_queue(tmp_path / "listener.sqlite")
    report = queued_probe(tmp_path, 1300.0, table())
    assert report["verdict"] == "unknown" and "not in the listener's queue" in report["why"]


def test_the_command_line_answers_for_a_queued_post(tmp_path, capsys):
    growbox_task_running(tmp_path, last_event_at=10**10)
    assert health.main(["--dir", str(tmp_path / "exec"), "--queue", str(tmp_path / "listener.sqlite"),
                        "--queued", "--since", "1099", "--channel", "pj-w", "--topic", "workplan-w"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["subject"]["queued"] is True and report["queue"]["ahead"][0]["ack"] == 41
