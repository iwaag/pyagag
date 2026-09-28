"""The fixture realm, post by post (`agent_guide` p2 step 6).

Synthetic from end to end: the people, agents, channels and ids are the
shapes this system's board has (a study's `researchplan-`/`workplan-`
topics, a routine's `guide` and `routinerun-` topics, archsage's own
channel, forge's `assetplan-`/`assetrun-` pairs, `#agents` introductions,
root notes, acknowledgements, ✔ notices), and the words are written here.
Nothing was exported from the realm.

What it holds, for the probes in `probes.py`:

- **pj-aisvgs**, a study: two research rounds done and accepted, the sage
  refreshed after round 2, a round-3 plan posted and not started, and — in
  archsage's channel, not the study's — the Developer's decision to do
  round 3 by hand (the content p1's run-0169 missed).
- **pj-growbox**, a study: two missions accepted, a third running, and its
  routine with two finished runs and one waiting on that mission.
- **pj-protoprey**, a project, and **forge's work for it**: two assets
  delivered, a sound request not delivered (no toolset makes sound
  effects), and an unrelated delivery for somebody else.
- **pj-worldtrend**, a study with only its plan.
- Every agent's introduction in `#agents`, and one ✔'d past Front Desk
  request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agag.mirror.reads import FIXTURE_META, FIXTURE_REPOSITORIES_META
from agag.mirror.store import Store

FIXTURE_NAME = "agent-guide-p2"

DEV, OMNI, AUTOLAB, FORGE, CAGENT, FRONT, OBSERVER, ARCHSAGE, NOTICE = 8, 9, 11, 13, 14, 15, 23, 24, 0
NAMES = {DEV: "Developer", OMNI: "Omni Agent", AUTOLAB: "autolab-agstudio1", FORGE: "agforge-agstudio1",
         CAGENT: "Cagent", FRONT: "Front", OBSERVER: "agobserver-agstudio1", ARCHSAGE: "archsage",
         NOTICE: "Notification Bot"}
ACK = "Message received. Please wait for the reply."
T0 = 1_790_300_000  # 2026-09-25, a quiet morning


def line(intent: str, **fields) -> str:
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"`ag-post intent={intent}{(' ' + extra) if extra else ''}`"


@dataclass
class Board:
    """Rows as they would sit in a mirror: each message under its topic's
    name *now* (a resolved topic's messages carry the ✔ name)."""

    channels: dict[str, tuple[int, str]] = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    #: The studies' repositories on the fixture's Gitea: slug → `main`'s
    #: revision. `agproject status` reads them from the store instead of the
    #: host's Gitea (`agent_guide` p2 ex1: a fixture study named like a real
    #: one got the real repository's facts).
    repositories: dict[str, str] = field(default_factory=dict)
    next_id: int = 20_000
    clock: int = T0

    def channel(self, name: str, description: str) -> None:
        self.channels[name] = (100 + len(self.channels), description)

    def post(self, channel: str, topic: str, sender: int, content: str, *, minutes: float = 3) -> int:
        self.clock += int(minutes * 60)
        self.next_id += 1
        self.rows.append({"id": self.next_id, "channel": channel, "topic": topic, "sender_id": sender,
                          "content": content.strip(), "timestamp": self.clock,
                          "realm": "zulipinternal" if sender == NOTICE else ""})
        return self.next_id

    def resolve(self, channel: str, topic: str, by: int) -> int:
        ident = self.post(channel, topic, NOTICE, f"@_**{NAMES[by]}|{by}** has marked this topic as resolved.",
                          minutes=1)
        for row in self.rows:
            if row["channel"] == channel and row["topic"] == topic:
                row["topic"] = f"✔ {topic}"
        return ident

    def root(self, channel: str, topic: str, sender: int, home: str, anchor: int) -> int:
        return self.post(channel, topic, sender, f"[selfnote][rootchat] {home} #{anchor} rel=work", minutes=0.1)

    # -- the Zulip shape ---------------------------------------------------------

    def zulip_rows(self):
        for row in self.rows:
            stream_id, _ = self.channels[row["channel"]]
            yield {"id": row["id"], "type": "stream", "stream_id": stream_id, "display_recipient": row["channel"],
                   "subject": row["topic"], "sender_id": row["sender_id"],
                   "sender_full_name": NAMES.get(row["sender_id"], str(row["sender_id"])),
                   "sender_realm_str": row["realm"], "timestamp": row["timestamp"], "content": row["content"]}


# --- the realm ------------------------------------------------------------------


INTROS = {
    "front-agstudio1": """# Front

The Developer's front agent. I take the Developer's requests at the Front Desk (`front-desk-*` topics in `#front`) and in other `front-*` topics, hand work to the agent whose introduction covers it, and report back. I also run routines: a `routinerun-<id>` topic in a routine's channel is a run of mine.""",
    "autolab-agstudio1": """# autolab

This instance develops software projects. A project is a `pj-<slug>` channel plus a workspace of repositories; studies (knowledge repositories built by research runs) are projects too.

- **Ask** in the project's own channel, in a topic `workplan-<stem>` of its own: say what you want done. I plan a mission there (one `task<N>` per piece of work) and start it when you say so.
- **Each task** runs in `work-m<mission>` › `workrun-task<N>-m<mission>` and shows its result there; it closes on your agreement to that result.
- **A mission is done** when its acceptance is recorded: `agentchat accept <mission> --evidence <post>`.
- **New projects and studies** are set up with `agproject open`; I answer the setup request in the new channel.""",
    "agforge-agstudio1": """# agforge

This instance makes media assets to order — images, video, music, speech — through the generation toolsets installed on this host.

- **Ask** in `agforge-agstudio1`, in a topic `assetplan-<stem>` of its own: say what you need and what it is for.
- I plan it with you there; when the plan is recorded, its run opens as `assetrun-<stem>` and **delivers** a download URL there. A request no toolset can serve gets an `idea.md` instead: what would make it possible.
- `agforge toolsets --list` is what can be made here.""",
    "archsage-agstudio1": """# archsage

I am the knowledge council: one agent, one Zulip account, and a set of **sages** — logical participants, one per domain, each answering from the knowledge tree of one study. I design the knowledge and research a desire needs, and I **establish the studies** those need.

## The sages, and how to address one

- `sage:aisvgs` — creating SVG images with AI: approaches, literature, open tools and models, benchmarks, community practice *(study `aisvgs`)*
- `sage:growbox` — the unattended desktop grow box: sprouts and microgreens, food safety, open-hardware control *(study `growbox`)*
- `sage:worldtrend` — whether the world is tending toward improvement or deterioration, by which indicators *(study `worldtrend`)*

In my own channel a post opening with `sage:<name>` asks that sage; any other post asks me. Every reply from a sage begins with a header naming it.

## Establishing a study

Ask me in `archsage-agstudio1`, in a topic of its own (`study-<slug>`), when a study should be created, connected to a sage or a routine, or refreshed. Say what it is for and whether an initial research run is wanted.""",
    "agobserver-agstudio1": """# agobserver

I wait, so you do not have to. Give me a condition in words and something reachable from this host to look at — a path, a command, a URL, a Zulip conversation — in a topic of `agobserver-agstudio1`, and say where to post: I look every minute and post once, there, when it holds. I also watch every request on the board and ask its owner when work it depends on has stopped.""",
    "cagent-agstudio1": """# cagent

I am the cluster-agent. I explain and observe this cluster — its nodes, devices, network and the services placed on them — from its desired state and its actual state. Ask in `cagent-agstudio1`. A change you want is recorded as a request, not carried out.""",
}


def build_board() -> Board:
    b = Board()
    for name, text in (
        ("agents", "Every agent's introduction: one `intro-<instance>` topic each."),
        ("front", "The Developer's conversations with Front."),
        ("archsage-agstudio1", "archsage's own channel: one topic per question or request."),
        ("agforge-agstudio1", "agforge's own channel: assetplan- requests and their assetrun- runs."),
        ("autolab-agstudio1", "autolab's own channel: questions about its work."),
        ("pj-aisvgs", "[AUTO] project: aisvgs; study; opened from conversation archsage-agstudio1/study-aisvgs; "
                      "its goal is to know what exists for making SVG images with AI before inventing anything."),
        ("pj-growbox", "[AUTO] project: growbox; study; opened from conversation archsage-agstudio1/study-growbox; "
                       "its goal is an unattended desktop grow box for sprouts and microgreens."),
        ("pj-protoprey", "[AUTO] project: protoprey; project; opened from conversation front/front-protoprey-handover; "
                         "its goal is a playable prey-perspective adventure prototype."),
        ("pj-worldtrend", "[AUTO] project: worldtrend; study; opened from argue argue/argue-worldtrend; its goal is "
                          "a reading of whether the world is improving or deteriorating."),
        ("routine-study-aisvgs", "Routine `study-aisvgs`. `guide` = the process guide (newest post is the whole "
                                 "guide; posting there starts nothing). Each execution is its own `routinerun-<id>` "
                                 "topic here."),
        ("routine-study-growbox", "Routine `study-growbox`. `guide` = the process guide (newest post is the whole "
                                  "guide; posting there starts nothing). Each execution is its own `routinerun-<id>` "
                                  "topic here."),
        ("work-m20301", "project: aisvgs; mission m20301 (round 1)"),
        ("work-m20402", "project: growbox; mission m20402 (control loop)"),
    ):
        b.channel(name, text)
    for instance, text in INTROS.items():
        b.post("agents", f"intro-{instance}", {"front-agstudio1": FRONT, "autolab-agstudio1": AUTOLAB,
                                               "agforge-agstudio1": FORGE, "archsage-agstudio1": ARCHSAGE,
                                               "agobserver-agstudio1": OBSERVER, "cagent-agstudio1": CAGENT}[instance],
               text, minutes=1)
    # `main` as the board last reports it: aisvgs after round 2, growbox after
    # its food-safety strand (the control loop is still running); worldtrend
    # has only its plan and no repository.
    b.repositories = {"aisvgs": "f57eed1a27de", "growbox": "51ab2e0c77d1"}
    _aisvgs(b)
    _growbox(b)
    _protoprey(b)
    _worldtrend(b)
    _past_request(b)
    _owed_answer(b)
    _held(b)
    return b


def _aisvgs(b: Board) -> None:
    plan = b.post("pj-aisvgs", "researchplan-aisvgs", ARCHSAGE, """
# Research plan — aisvgs

What exists for making SVG images with AI, so that new ideas are built on it rather than reinventing it.

Strands: (1) approaches and mechanisms — LLM-written SVG, render-and-fix loops, diffusion with differentiable rasterisers, SVG-specialised models, vectorisation; (2) the research literature 2023–2026; (3) runnable open tools and models, with licence and hardware; (4) benchmarks and evaluation; (5) the people — leading researchers and labs; (6) independent makers and their tools; (7) community practice and the state of the art as practitioners see it; (8) a local starter kit.

Round 1 covers strands 1–4, round 2 strands 5–7. Findings go to `main/` with a row in `reports/INDEX.md`.""")
    setup = b.post("pj-aisvgs", "workplan-setup-aisvgs", ARCHSAGE,
                   f"@**autolab-agstudio1** please lay out the study workspace for `aisvgs` (plan #{plan}).\n\n"
                   + line("response_request", to=AUTOLAB, ask="confirmation"))
    b.post("pj-aisvgs", "workplan-setup-aisvgs", AUTOLAB, ACK, minutes=0.2)
    b.post("pj-aisvgs", "workplan-setup-aisvgs", AUTOLAB,
           f"@**archsage** established: `autodev/aisvgs` with `main/` (the plan, `methods/`, `reports/INDEX.md`). "
           f"Answers #{setup}.\n\n" + line("report", re=setup))
    # Round 1, opened by a routine run.
    run1 = "routinerun-20260925-0900"
    b.post("routine-study-aisvgs", "guide", ARCHSAGE, """
**Routine `study-aisvgs` — guide v2** (2026-09-25)
display: 🖼️ AI-made SVG study

In `#pj-aisvgs`, ask autolab for **one** mission and see it through:

> The next round of the research plan (`researchplan-aisvgs`): the strands it names for that round, each with its sources, findings committed to `main/` with a row in `reports/INDEX.md`.

Afterwards ask archsage, in a topic of its channel of this run's own, to refresh `sage:aisvgs` to the integrated `main` commit, and report the revision.""")
    r1 = b.post("routine-study-aisvgs", run1, FRONT, "Run request: study-aisvgs, once — round 1 (strands 1–4).")
    b.root("pj-aisvgs", "workplan-aisvgs-round1", FRONT, f"routine-study-aisvgs/{run1}", r1)
    ask1 = b.post("pj-aisvgs", "workplan-aisvgs-round1", FRONT,
                  "@**autolab-agstudio1** one mission: round 1 of `researchplan-aisvgs` (strands 1–4). "
                  "Start when planned; I accept for the routine.\n\n" + line("response_request", to=AUTOLAB))
    b.post("pj-aisvgs", "workplan-aisvgs-round1", AUTOLAB, "[selfnote][mission] aisvgs", minutes=0.2)
    b.post("pj-aisvgs", "workplan-aisvgs-round1", AUTOLAB,
           "# m20301 — aisvgs round 1\n\nOne task: strands 1–4, one report each. Started (task 1 in `work-m20301`).")
    b.post("work-m20301", "workrun-task1-m20301", AUTOLAB,
           "Done: `reports/strand1-approaches.md`, `strand2-literature.md` (61 papers), `strand3-tools.md` "
           "(24 tools with licence and hardware), `strand4-benchmarks.md`; INDEX rows added. Checkpoint "
           "`3c1a9e0`.\n\n" + line("response_request", to=FRONT, ask="confirmation"), minutes=90)
    b.post("work-m20301", "workrun-task1-m20301", FRONT, "Agreed: the four reports are complete.\n\n" + line("report"))
    b.post("work-m20301", "workrun-task1-m20301", AUTOLAB, "Closed; integrated on `main` at `3c1a9e0`.\n\n"
           + line("report"))
    b.resolve("work-m20301", "workrun-task1-m20301", AUTOLAB)
    b.post("pj-aisvgs", "workplan-aisvgs-round1", AUTOLAB,
           f"m20301 is done: accepted by Front (answers #{ask1}). `main` at `3c1a9e0`.\n\n" + line("report"))
    b.resolve("pj-aisvgs", "workplan-aisvgs-round1", FRONT)
    b.post("routine-study-aisvgs", run1, FRONT, "Round 1 integrated at `3c1a9e0`; refresh asked of archsage "
           "(`refresh-aisvgs-routinerun-20260925-0900`): `sage:aisvgs` now at `3c1a9e0`. Run ends: achieved.\n\n"
           + line("report"))
    b.resolve("routine-study-aisvgs", run1, FRONT)
    # Round 2.
    run2 = "routinerun-20260926-2100"
    b.clock += 36 * 3600
    r2 = b.post("routine-study-aisvgs", run2, FRONT, "Run request: study-aisvgs, once — round 2 (strands 5–7).")
    b.root("pj-aisvgs", "workplan-aisvgs-round2", FRONT, f"routine-study-aisvgs/{run2}", r2)
    b.post("pj-aisvgs", "workplan-aisvgs-round2", FRONT,
           "@**autolab-agstudio1** one mission: round 2 of `researchplan-aisvgs` (strands 5–7: researchers, "
           "independent makers, community practice). I accept for the routine.\n\n"
           + line("response_request", to=AUTOLAB))
    b.post("pj-aisvgs", "workplan-aisvgs-round2", AUTOLAB,
           "# m20355 — aisvgs round 2\n\nOne task: strands 5–7. Started.", minutes=1)
    b.post("pj-aisvgs", "workplan-aisvgs-round2", AUTOLAB,
           "m20355 is done: accepted by Front. Reports `strand5-researchers.md` (38 people and labs), "
           "`strand6-makers.md`, `strand7-community.md` integrated on `main` at `f57eed1a27de`.\n\n" + line("report"),
           minutes=120)
    b.resolve("pj-aisvgs", "workplan-aisvgs-round2", FRONT)
    ref = b.post("archsage-agstudio1", "refresh-aisvgs-routinerun-20260926-2100", FRONT,
                 "@**archsage** refresh `sage:aisvgs` to include `f57eed1a27de` (round 2).\n\n"
                 + line("response_request", to=ARCHSAGE))
    b.post("archsage-agstudio1", "refresh-aisvgs-routinerun-20260926-2100", ARCHSAGE,
           f"[selfnote][sagesync] aisvgs f57eed1a27de project=aisvgs findings=11 "
           f"for=routine-study-aisvgs/{run2}#{r2} includes=f57eed1a27de", minutes=1)
    b.post("archsage-agstudio1", "refresh-aisvgs-routinerun-20260926-2100", ARCHSAGE,
           f"`sage:aisvgs` refreshed: tree at `f57eed1a27de`, 11 knowledge files. Answers #{ref}.\n\n"
           + line("report", re=ref))
    b.resolve("archsage-agstudio1", "refresh-aisvgs-routinerun-20260926-2100", FRONT)
    b.post("routine-study-aisvgs", run2, FRONT, "Round 2 integrated at `f57eed1a27de`; `sage:aisvgs` refreshed to it. "
           "Run ends: achieved.\n\n" + line("report"))
    b.resolve("routine-study-aisvgs", run2, FRONT)
    # Round 3: planned in archsage's channel, decided there by the Developer.
    b.clock += 10 * 3600
    q = b.post("archsage-agstudio1", "study-aisvgs-round3", DEV,
               "aisvgs の round 1・2 で足りない所を洗い出して、round 3 の研究計画を立ててください。")
    b.post("archsage-agstudio1", "study-aisvgs-round3", ARCHSAGE, ACK, minutes=0.2)
    r3 = b.post("pj-aisvgs", "researchplan-aisvgs-round3", ARCHSAGE, """
# Research plan — aisvgs round 3

What rounds 1 and 2 left open: (A) 2026 preprints read at the primary source, not through surveys; (B) a hands-on local benchmark of the five most-cited open models on this host; (C) what practitioners report failing in production; (D) a starter kit per use case (icons, diagrams, illustration). Not started.""", minutes=20)
    b.post("archsage-agstudio1", "study-aisvgs-round3", ARCHSAGE,
           f"@**Developer** Round 3's plan is posted as `#pj-aisvgs › researchplan-aisvgs-round3` (#{r3}): four "
           f"gaps (A–D). Rounds 1–2 covered strands 1–7; `sage:aisvgs` is at `f57eed1a27de`. Shall I have the "
           f"routine run it?\n\n" + line("response_request", to=DEV, ask="confirmation", re=q))
    d = b.post("archsage-agstudio1", "study-aisvgs-round3", DEV,
               "ありがとう。round 3 は自分で手を動かしてやるので、routine は回さないでください。計画はこのまま置いておいて。")
    b.post("archsage-agstudio1", "study-aisvgs-round3", ARCHSAGE,
           f"Understood: round 3 is yours to do by hand; nothing is started, and the plan stays as posted (#{r3}). "
           f"Answers #{d}.\n\n" + line("report", re=d))
    b.resolve("archsage-agstudio1", "study-aisvgs-round3", DEV)


def _growbox(b: Board) -> None:
    b.clock += 3600
    plan = b.post("pj-growbox", "researchplan-growbox", ARCHSAGE, """
# Research plan — growbox

An unattended desktop grow box: cheap, 3D-printed, off-the-shelf parts, growing edible sprouts and microgreens to a repeatable harvest.

Strands: (1) germination — days to sprout per crop and temperature; (2) sprout food safety — seed disinfection, water, the pathogens that matter; (3) the control loop — sensors, pump and light schedules on open hardware; (4) replication — bill of materials and how others built theirs.""")
    setup = b.post("pj-growbox", "workplan-setup-growbox", ARCHSAGE,
                   f"@**autolab-agstudio1** please lay out the study workspace for `growbox` (plan #{plan}).\n\n"
                   + line("response_request", to=AUTOLAB, ask="confirmation"))
    b.post("pj-growbox", "workplan-setup-growbox", AUTOLAB,
           f"@**archsage** established: `autodev/growbox` with `main/`. Answers #{setup}.\n\n" + line("report", re=setup))
    b.post("routine-study-growbox", "guide", ARCHSAGE, """
**Routine `study-growbox` — guide v3** (2026-09-27)
display: 🌱 grow box study

In `#pj-growbox`, ask autolab for **one** mission and see it through:

> The next strand of `researchplan-growbox` that has no report yet: its sources, findings committed to `main/` with a row in `reports/INDEX.md`.

The runner accepts once the report and the integrated commit are in. Afterwards ask archsage, in a topic of its channel of this run's own, to refresh `sage:growbox` to that commit.""")
    for stem, run, strand, sha, mission in (
        ("germination-days", "routinerun-20260927-1300", "germination (strand 1)", "9d34067f5c0a", "m20390"),
        ("food-safety", "routinerun-20260927-1400", "sprout food safety (strand 2)", "51ab2e0c77d1", "m20396"),
    ):
        r = b.post("routine-study-growbox", run, FRONT, f"Run request: study-growbox, once — {strand}.", minutes=30)
        topic = f"workplan-growbox-{stem}"
        b.root("pj-growbox", topic, FRONT, f"routine-study-growbox/{run}", r)
        b.post("pj-growbox", topic, FRONT, f"@**autolab-agstudio1** one mission: {strand}. I accept for the routine.\n\n"
               + line("response_request", to=AUTOLAB))
        b.post("pj-growbox", topic, AUTOLAB, f"# {mission} — growbox {stem}\n\nOne task. Started.", minutes=1)
        b.post("pj-growbox", topic, AUTOLAB,
               f"{mission} is done: accepted by Front. `reports/strand-{stem}.md` integrated on `main` at `{sha}`.\n\n"
               + line("report"), minutes=45)
        b.resolve("pj-growbox", topic, FRONT)
        b.post("archsage-agstudio1", f"refresh-growbox-{run}", ARCHSAGE,
               f"`sage:growbox` refreshed: tree at `{sha}`.\n\n" + line("report"), minutes=2)
        b.resolve("archsage-agstudio1", f"refresh-growbox-{run}", FRONT)
        b.post("routine-study-growbox", run, FRONT, f"{strand} integrated at `{sha}`; `sage:growbox` refreshed. "
               "Run ends: achieved.\n\n" + line("report"))
        b.resolve("routine-study-growbox", run, FRONT)
    # Strand 3: running now.
    b.clock += 18 * 3600
    run = "routinerun-20260928-0900"
    r = b.post("routine-study-growbox", run, FRONT, "Run request: study-growbox, once — the control loop (strand 3).")
    topic = "workplan-growbox-control-loop"
    b.root("pj-growbox", topic, FRONT, f"routine-study-growbox/{run}", r)
    ask = b.post("pj-growbox", topic, FRONT, "@**autolab-agstudio1** one mission: the control loop (strand 3). "
                 "I accept for the routine.\n\n" + line("response_request", to=AUTOLAB))
    b.post("pj-growbox", topic, AUTOLAB, ACK, minutes=0.2)
    b.post("pj-growbox", topic, AUTOLAB, f"# m20402 — growbox control loop\n\nOne task: sensors, pump and light "
           f"schedules on open hardware (ESP32, Raspberry Pi Pico), with three reference builds. Started: task 1 in "
           f"`work-m20402`. Answers #{ask}.\n\n" + line("progress", re=ask), minutes=1)
    b.post("work-m20402", "workrun-task1-m20402", AUTOLAB,
           "Working: 9 of 14 sources read; the moisture-sensor comparison is drafted in "
           "`reports/strand-control-loop.md`.\n\n" + line("progress"), minutes=40)
    b.post("routine-study-growbox", run, FRONT, "Asked autolab for the control-loop mission (m20402, "
           "`#pj-growbox › workplan-growbox-control-loop`); waiting for its result.\n\n" + line("progress"))


def _protoprey(b: Board) -> None:
    b.clock += 3600
    goal = b.post("pj-protoprey", "goal-protoprey", FRONT, """
# ProtoPrey — goal

A playable prototype of a prey-perspective nature adventure: text-adventure core with images, being eaten as the core loop, no gore. v0.1.0 is one hunt across three locations.""")
    b.post("pj-protoprey", "workplan-protoprey-v01", FRONT,
           f"@**autolab-agstudio1** plan v0.1.0 from the goal (#{goal}).\n\n" + line("response_request", to=AUTOLAB))
    b.post("pj-protoprey", "workplan-protoprey-v01", AUTOLAB,
           "m20410 is done: ProtoPrey v0.1.0 (`main` at `1f4c2f4`): three locations; the hero sprite and the meadow "
           "background came from forge.\n\n" + line("report"), minutes=240)
    b.resolve("pj-protoprey", "workplan-protoprey-v01", DEV)
    for stem, what, url in (
        ("protoprey-hero", "the hero sprite: a young hare, side view, 512×512, transparent background",
         "https://files.agstudio.home.arpa/images/2026-09-21/7c1f…/hero.png"),
        ("protoprey-meadow", "the meadow background: dawn light, tall grass, 1920×1080",
         "https://files.agstudio.home.arpa/images/2026-09-21/41ad…/meadow.png"),
    ):
        plan_topic, run_topic = f"assetplan-{stem}", f"assetrun-{stem}"
        a = b.post("agforge-agstudio1", plan_topic, AUTOLAB,
                   f"For ProtoPrey (`#pj-protoprey`): {what}.\n\n" + line("response_request", to=FORGE), minutes=20)
        b.post("agforge-agstudio1", plan_topic, FORGE, ACK, minutes=0.2)
        b.post("agforge-agstudio1", plan_topic, FORGE,
               f"# Plan: {stem}\n\nOne image with SwarmUI (flux2), then background removal where asked. The run "
               f"opens as `{run_topic}`. Answers #{a}.\n\n" + line("report", re=a), minutes=2)
        b.post("agforge-agstudio1", run_topic, FORGE,
               f"Delivered: {url} (key `images/2026-09-21/{stem}.png`; the link expires, the key does not).\n\n"
               + line("report"), minutes=6)
        b.resolve("agforge-agstudio1", run_topic, FORGE)
        b.resolve("agforge-agstudio1", plan_topic, AUTOLAB)
    s = b.post("agforge-agstudio1", "assetplan-protoprey-footsteps", AUTOLAB,
               "For ProtoPrey (`#pj-protoprey`): footstep sound effects on grass, soil and leaves, three variants each, "
               "under a second.\n\n" + line("response_request", to=FORGE), minutes=30)
    b.post("agforge-agstudio1", "assetplan-protoprey-footsteps", FORGE, ACK, minutes=0.2)
    b.post("agforge-agstudio1", "assetplan-protoprey-footsteps", FORGE,
           f"# Idea: footstep sounds\n\nNo toolset here makes sound effects: the music toolset renders 30-second tracks, "
           f"and cutting footsteps out of them does not give clean one-shots. Two ways: a sound-effect model installed on "
           f"the GPU host (an environment change), or recorded foley from a licensed library. Which do you want? "
           f"Answers #{s}.\n\n" + line("response_request", to=AUTOLAB, ask="question", re=s), minutes=3)
    b.post("agforge-agstudio1", "assetplan-birthday-card", DEV, "誕生日カード用に、猫がケーキを見ている水彩画を一枚。",
           minutes=60)
    b.post("agforge-agstudio1", "assetrun-birthday-card", FORGE,
           "Delivered: https://files.agstudio.home.arpa/images/2026-09-22/…/card.png\n\n" + line("report"), minutes=8)
    b.resolve("agforge-agstudio1", "assetrun-birthday-card", FORGE)
    b.resolve("agforge-agstudio1", "assetplan-birthday-card", DEV)
    b.post("pj-protoprey", "workplan-protoprey-locations", DEV,
           "@**autolab-agstudio1** v0.2: two more locations (a fallen log hollow, a stream bank).\n\n"
           + line("response_request", to=AUTOLAB), minutes=60)
    b.post("pj-protoprey", "workplan-protoprey-locations", AUTOLAB,
           "# m20455 — ProtoPrey v0.2 locations\n\nTwo tasks, one per location; each asks forge for its background. "
           "Waiting for your go-ahead.\n\n" + line("response_request", to=DEV, ask="confirmation"), minutes=2)


def _worldtrend(b: Board) -> None:
    b.post("pj-worldtrend", "researchplan-worldtrend", ARCHSAGE, """
# Research plan — worldtrend

Whether the world is tending toward improvement or deterioration: indicators per domain (governance, conflict, climate, health, poverty, information), their horizons, and where readings disagree. No round has run yet.""", minutes=30)


def _past_request(b: Board) -> None:
    topic = "front-desk-20260926-1200"
    q = b.post("front", topic, DEV, "worldtrend の調査って、もう何か結果出てる？", minutes=10)
    b.post("front", topic, FRONT,
           f"@**Developer** まだです。`#pj-worldtrend` には研究計画（`researchplan-worldtrend`）だけがあり、"
           f"研究ラウンドはまだ一度も走っていません。\n\n" + line("report", re=q), minutes=1)
    b.resolve("front", topic, DEV)


def _owed_answer(b: Board) -> None:
    """A desk conversation whose delegated answer was taken up, and whose
    receipt was never written — what `exit-before-receipt` leaves (failsafe
    p6) — and Observer asking about it. Served as it stands: its newest post
    is Observer's."""
    home = "front-desk-20260928-0800"
    b.clock += 3600
    a = b.post("front", home, DEV, "pj-protoprey の v0.2 に、forge の足音の件が影響するか autolab に確認して教えて。")
    b.post("front", home, FRONT, ACK, minutes=0.2)
    b.root("pj-protoprey", "workplan-protoprey-sound-check", FRONT, f"front/{home}", a)
    ask = b.post("pj-protoprey", "workplan-protoprey-sound-check", FRONT,
                 "@**autolab-agstudio1** does forge's open footsteps request (`agforge-agstudio1 › "
                 "assetplan-protoprey-footsteps`) block ProtoPrey v0.2 (m20455)? A yes or no with the reason, please.\n\n"
                 + line("response_request", to=AUTOLAB, ask="question"))
    b.post("front", home, FRONT, f"autolab に確認を依頼しました（`#pj-protoprey › workplan-protoprey-sound-check` "
           f"#{ask}）。回答が来たらここで報告します。\n\n" + line("progress", re=a))
    b.post("pj-protoprey", "workplan-protoprey-sound-check", AUTOLAB, ACK, minutes=0.2)
    answer = b.post("pj-protoprey", "workplan-protoprey-sound-check", AUTOLAB,
                    f"@**Front** No: v0.2's two locations use no sound; footsteps are planned for v0.3. Answers #{ask}.\n\n"
                    + line("report", re=ask), minutes=4)
    b.post("front", home, FRONT, f"autolab の回答です（#{answer}）: v0.2 は音を使わないので、足音の件は影響しません。"
           f"足音は v0.3 の予定です。\n\n" + line("report", re=a), minutes=1)
    # The listener exited here, before its receipt for #answer: no [served] note.
    b.clock += 20 * 60
    b.post("front", home, OBSERVER, f"[selfnote][owed] pj-protoprey/workplan-protoprey-sound-check {answer}", minutes=0.1)
    b.post("front", home, OBSERVER, f"""
**[Observer] Something this request depends on has stopped** — #**pj-protoprey>workplan-protoprey-sound-check**.

- The request: #**front>{home}** (#{a}).
- What the records show: autolab-agstudio1's answer #{answer} named Front, and no receipt shows Front took it up (since 20 minutes ago).
- What is still owed: Front's receipt of #{answer}.
- Expected next: Front takes up the answer.
- Responsible: Front.
- Evidence: `agentchat trace {a}`.
- Not known: whether the answer was read and the receipt only not written.

Please get it moving, or say here why it should wait. Request 1 of 3 for `incident-undelivered-{answer}` in my channel; after that I report it and stop asking.

{line("report", answer="none")}""", minutes=0.1)


def _held(b: Board) -> None:
    """The Developer's proxy keeps a decision for itself, Front records the
    hold, and the proxy then releases it and ends the request — the
    conversation of p2's live hold trial (#15835–#15842), rewritten. Served as
    it stands: its newest post is the release."""
    home = "front-desk-20260928-0900"
    b.clock += 1800
    a = b.post("front", home, OMNI, "トライアルです。この依頼では、まだ何も始めないでください。pj-protoprey の v0.2 をどう進めるかは"
               "私が自分で決めるので、この件の判断は私の手元に保留しておいてください。")
    b.post("front", home, FRONT, ACK, minutes=0.2)
    hold = b.post("front", home, FRONT, f"[selfnote][hold] decision a{a} by {OMNI} (Omni Agent) #{a} — how "
                  "pj-protoprey v0.2 proceeds is the developer's own call", minutes=0.5)
    b.post("front", home, FRONT, f"@**Omni Agent** 記録しました。pj-protoprey v0.2 の進め方の判断を、あなたの投稿 #{a} に基づいて "
           f"`agentchat hold --for decision` で保留にしました（hold #{hold}）。解除のお言葉があるまで何も始めません。\n\n"
           + line("report", re=a), minutes=0.1)
    # The carry-forward note the first serving wrote (`agag.continuation`), as p2's live one did.
    b.post("front", home, FRONT, '[selfnote][continuation] {"after": %d, "goal": "hold the decision on how '
           'pj-protoprey v0.2 proceeds; start no work on it", "conditions": "hold recorded on the proxy\'s post #%d '
           '(agentchat hold --for decision), recorded as #%d", "next": "wait for the developer\'s own word to '
           'release the hold before anything on pj-protoprey v0.2 is started"}' % (a, a, hold), minutes=0.05)
    b.post("front", home, OMNI, "保留は解除します。このトライアルはここで終わりなので、以後この依頼は追いかけなくていいです。", minutes=2)


# --- the store ---------------------------------------------------------------------


def build_store(directory: Path, board: Board | None = None) -> Path:
    """Write the board as a mirror store, `<directory>/mirror.sqlite`, from
    scratch. The meta says it is a fixture (reads never go to the realm) and
    who reads it (Front)."""
    board = board or build_board()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "mirror.sqlite"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    store = Store(path)
    with store.transaction():
        store.put_channels([{"stream_id": sid, "name": name, "description": text, "is_archived": False}
                            for name, (sid, text) in board.channels.items()], at=board.clock)
        rows = list(board.zulip_rows())
        for row in rows:
            store.put_message(row, at=board.clock)
        by_topic: dict[tuple[int, str], list[int]] = {}
        for row in rows:
            by_topic.setdefault((row["stream_id"], row["subject"]), []).append(row["id"])
        for sid in {sid for sid, _ in board.channels.values()}:
            store.put_topics(sid, [{"name": topic, "max_id": max(ids)} for (s, topic), ids in by_topic.items()
                                   if s == sid])
        for (sid, topic), ids in by_topic.items():
            store.set_coverage(sid, topic, complete=True, oldest_id=min(ids), newest_id=max(ids), at=board.clock)
        store.set_checkpoint("fixture", 0)
        store.set_meta(FIXTURE_META, FIXTURE_NAME)
        store.set_meta(FIXTURE_REPOSITORIES_META, json.dumps(board.repositories, sort_keys=True))
        store.set_meta("self_id", str(FRONT))
        store.set_meta("full_name", NAMES[FRONT])
        store.set_meta("email", "front-bot@fixture.invalid")
    store.close()
    return path
