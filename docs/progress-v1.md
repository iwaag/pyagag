# Progress for people (`agag.progress.v1`)

progress_panel p1. One request's plans, tasks and runs as a card a person
can read at a glance, interpreted once for every reader (the agentroom
relay's `/progress` and the Front Desk's panel today). Pure: the caller
supplies the trace (`agag.trace`), the health checks, the monitor's records
and whether its source was live.

    card(trace, now=, health={anchor: agag.health.v1 report}, recovery={anchor: …},
         viewer_id=, pending=[response requests in the origin], source_live=, syncs=[sagesync notes])
    queue_behind(cards)   # across requests: what a queued post waits behind

## A card

| field | meaning |
|---|---|
| `origin`, `anchor` | the request: its origin conversation's first post (`o<id>`) |
| `state`, `reason`, `next` | the display state of the unit that decides it (`focus`), why, and whose move |
| `stages` | `tasks_agreed`, `plan_accepted`, `run_ended`, `report_delivered`, `knowledge_refreshed` (study routines), each `done`/`pending` by its record |
| `root` | the unit tree |
| `stale` | the source was not live: everything is last known |

## A unit

Three facts, never merged:

- `work` — the trace state, the record word and its detail;
- `execution` — the serving the conversation shows (`open`/`ended`/`unknown`,
  ack, end) and `health`, the check of **that** ack only, with `evidence`
  `confirmed` (≤ 120 s old), `stale`, or `conversation` (no check);
- `recovery` — the monitor's incident on this unit (kind, state, open or
  reported-and-unrecovered) or a person's hold/retirement.

`display.state` is one of `planning`, `queued`, `working`, `waiting`,
`awaiting_you`, `answered`, `completed`, `cancelled`, `stopped`, `unknown`.
An open serving without a fresh check is `working` only while its owner
showed work within `WORK_QUIET` (1800 s), and never animates; past that it
is `unknown`. `run` is the activity under a serving: `active`, `waiting`,
`claimed`, `stopped`, `ended`, `unknown`; `determinate` is always null —
no record counts a run's units.

A plan's `meter`: `known`, `total` (tasks not cancelled), `completed`
(agreed), `working`, `awaiting_agreement`, `stopped`, `unknown`,
`cancelled`, `revisions` (each `[doc]` and the total it left), `note`,
`segments` (one per task, serial order). No total while planning.

## Rules worth knowing

- A card is `completed` only when every unit of work (plan, task, run,
  routine run) is finished by record and every stage is; a delivered answer
  or an ended run alone completes nothing. All cancelled reads `cancelled`.
- A pending question to a person in the request's own conversation leads
  the card; units nobody holds wait on it.
- A refresh counts for a study when it names the study's project and comes
  after this run's research was accepted, wherever it was recorded.
- `queue_behind`: a listener serves one conversation at a time, so a post
  its owner has not acknowledged while the owner has a serving open with
  evidence elsewhere is queued behind that serving, and says so.
