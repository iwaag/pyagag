# Execution health (`agag.execution.v1`, `agag.health.v1`)

failsafe p2. Whether a serving's work is still being done cannot be read
from its conversation: an ack with no reply looks the same whether the run
is busy, waiting on a test suite, or dead. An owner can expose the answer
through one command. A monitor (Observer) calls that command with a timeout
of its own.

## The live execution record (`agag.execution.v1`)

`run_harness(..., live=LiveExecution(path, serving={...}))`, or
`run_role(..., live=path)` inside a listener serving, writes one JSON file
per run. The writes are atomic. Tool starts and ends are written at once,
and other events at most every 5 s.

| field | meaning |
|---|---|
| `serving` | the listener's serving: `id`, `ack` (the acknowledgement that opened it), `channel`, `topic`, `route`, `home_anchor`, `role` |
| `harness`, `pid`, `started_at`, `deadline_at` | the process and when `run_harness` will kill it |
| `last_event_at`, `last_event`, `events` | activity: the last harness event of any kind (`tool Bash`, `tool result`, `text`, `generating`, `system …`) |
| `last_work_at`, `last_work` | progress (since `failsafe` p3): the last **work** event — a tool call starting or returning, the model's text or partial output, a subagent's own events (`subagent: …`) |
| `housekeeping` | `count`, `last_at`, `last`: events that are not work — `system` events (Claude Code emits them while a long Bash call runs), stream pings, kinds nobody named |
| `tool_results`, `open_tools`, `tools_done` | the tool calls started and not yet returned (only where the stream reports results: `claude_code`, `agcode`); each open call keeps its `bound`, the seconds it declares it may take (Bash: its `timeout`, 120 s by default, 600 s at most; a background call 5 s), or null |
| `ended_at`, `exit_code`, `outcome` | the end, written by the runner. A missing end and a dead pid means the run died unnoticed |

When a record is kept, `claude_code` runs with `--include-partial-messages`.
The partial chunks reach the record only: they count as work, except a
stream `ping`, and are dropped from the output and the transcript.

## The probe (`agag.health.v1`)

```sh
python -m agag.health --dir <records> [--queue <listener.sqlite>] --ack <id> \
    [--channel <c> --topic <t>] [--window 120]
```

It prints one document. It never raises, and it is bounded: one `ps`
(5 s) and one read-only query (2 s).

| field | meaning |
|---|---|
| `observed_at`, `source`, `subject` | when, from where, about which serving (by ack) |
| `process` | `alive` / `exited` / `unknown`: pid, how it was concluded (the runner's recorded end, not in the process table, pid reused) |
| `progress` | the last work and its age (`work_age_seconds`), the last event of any kind and its age, the housekeeping count and its last kind |
| `wait` | `tool` (id, name, detail, since, `bound_seconds`, `over_bound`, processes under it), `children`, `none`, `unknown`; `cpu_seconds` is the CPU time of the processes under the run, for a monitor comparing looks |
| `serving` | the journal row for that ack (`state`, `delivered_id`), and whether the conversation is queued or served again |
| `verdict`, `why`, `unknowns` | the conclusion, its reason, and what could not be established |

The verdicts:

- `running`: alive, with **work** within the window. Housekeeping never
  makes a run `running`.
- `waiting`: alive, no recent work, with a named tool call inside its own
  bound (plus 60 s) or declaring none, or a child process, inside the
  deadline. A tool call past its own bound is `unknown`: the harness kills
  a call at its bound, so one still open is not doing its work.
- `stopped`: the process is gone, or ended with nothing delivered, and
  nothing is queued.
- `ended`: a reply was delivered or is being delivered, or the
  conversation is served again.
- `unknown`: everything else. Alive with no work and no named wait is
  `unknown`, not healthy, however much housekeeping it emits.

A probe is one look. Whether a named wait is still advancing is a
comparison between looks, which is the monitor's: Observer compares
`cpu_seconds` and treats a wait that declares no bound and has not
advanced for 15 min as uncertainty (`agobserver.monitor.WAIT_IDLE`).

**A record of another serving is never applied.** The subject is the ack,
and records of the same conversation that belong to other servings are
listed in `other_servings` only.
