"""What is asked of the fixture board, and how an answer is judged.

Each probe is one post a person makes, the role it is served by, and a pass
rule over the reply the role marks. The rules are deliberately about
**content the board holds** — names, ids, states — so the same rule judges
a run with the old guide and a run with the new one. A rule is a set of
facts that must all appear (each fact is one or more accepted spellings)
and facts that must not; `judge` reports which were met.

Front's three are p1's board probes (report6): the incident wording of
#15673, "the grow box thing", and forge's past work for ProtoPrey. The
others are p2 step 7's: archsage asked about a sage by a loose name, an
autolab planner asked about another project's state, Observer's triage
asked about a request it did not open.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .responder import Canned

__all__ = ["DRIVERS", "PROBES", "Probe", "judge"]

#: The module that serves an agent's probes, run from that agent's checkout
#: with its own venv: `.venv/bin/python -m <driver> <probe> --out <dir>`.
DRIVERS = {"agfront": "agfront.trial", "archsage": "archsage.trial", "agautolab": "agautolab.trial",
           "agobserver": "agobserver.trial"}


@dataclass(frozen=True)
class Probe:
    name: str
    agent: str
    role: str
    channel: str
    topic: str
    text: str
    must: tuple[tuple[str, ...], ...]
    must_not: tuple[str, ...] = ()
    #: Facts looked for and reported, but not part of the pass rule: what a
    #: trial wants to see measured that no guide in scope is meant to change.
    observe: tuple[tuple[str, ...], ...] = ()
    #: Rules over the run's tool calls (one line per call), for a probe whose
    #: behaviour is an act, not words: each group needs one call matching any
    #: of its substrings; no call may contain a `tools_must_not` substring.
    tools_must: tuple[tuple[str, ...], ...] = ()
    tools_must_not: tuple[str, ...] = ()
    note: str = ""
    speaker: str = "Developer"
    extra: dict = field(default_factory=dict)
    #: The responder's script (`agag.fixture.responder`): a probe that has one
    #: is served on an overlay, and again on each scripted callback. Its
    #: `must`/`must_not` then judge the **last** serving's reply, the tool
    #: rules every serving's calls, and `sends_must` the posts the served
    #: agent sent (each group: one post carrying any of its spellings).
    script: tuple[Canned, ...] = ()
    sends_must: tuple[tuple[str, ...], ...] = ()
    #: How many servings the conversation must have had: a scripted answer
    #: counts only when it came back on a serving of its own.
    servings_min: int = 1
    #: Run the listener's claim check (`agag.claims`, failsafe p7) on every
    #: serving: the probe is served on an overlay, where the records a run
    #: writes are kept, so the check has a window to read. A mismatch is
    #: written there as the listener writes it and the conversation is
    #: served again, as its start note would have it served.
    claims: bool = False


PROBES = {p.name: p for p in (
    Probe(
        name="aisvgs-sufficient", agent="agfront", role="desk", channel="front",
        topic="front-desk-fixture-aisvgs",
        text=("AISVGsの調査は十分に行われ、主要な研究者や個人の取り組み、コミュニティの最先端を確認し、"
              "車輪の再発明をする確率を下げつつ新しいアイディアを発明できる段階にあると言えますか？"),
        must=(("pj-aisvgs", "routine-study-aisvgs", "sage:aisvgs"),
              ("round 2", "round2", "ラウンド2", "第2", "strand 5", "strands 5", "5–7", "5-7"),
              ("round 3", "round3", "ラウンド3", "第3", "researchplan-aisvgs-round3")),
        must_not=("見つかりません", "記録がありません", "教えてください", "no record"),
        observe=(("study-aisvgs-round3", "自分で", "ご自身で", "by hand", "yourself", "回さない"),),
        note=("#15673's wording. Passes when the reply finds the study and its rounds on the board and says round 3 "
              "is planned and not started (p1's pass for run-0169). Observed, not required: whether it also carries "
              "the Developer's decision, recorded only in archsage's channel, that round 3 is theirs to do by hand — "
              "where such a decision belongs is p1's open finding 1, held by the Developer."),
    ),
    Probe(
        name="growbox-thing", agent="agfront", role="desk", channel="front", topic="front-desk-fixture-growbox",
        text="あのgrow boxのやつ、いまどこまで進んでる？",
        must=(("pj-growbox",),
              ("germination", "発芽"),
              ("food safety", "food-safety", "食品安全", "衛生"),
              ("control loop", "control-loop", "制御", "m20402")),
        must_not=("教えてください", "どのプロジェクト"),
        note="Passes when the reply names the study, the two accepted strands and the control-loop mission running now.",
    ),
    Probe(
        name="forge-protoprey", agent="agfront", role="desk", channel="front", topic="front-desk-fixture-forge",
        text="forgeってprotoprey向けに何を納品したんだっけ？",
        must=(("hero", "ヒーロー", "野ウサギ", "hare"),
              ("meadow", "草原", "背景"),
              ("footstep", "足音", "sound", "サウンド", "効果音")),
        must_not=("birthday", "誕生日"),
        note=("Passes when the reply lists the two deliveries and says the footstep sounds were not delivered (no "
              "sound-effect toolset; forge asked which way), and leaves out the unrelated birthday card."),
    ),
    Probe(
        name="archsage-loose-sage", agent="archsage", role="archsage", channel="archsage-agstudio1",
        topic="fixture-sprouts", text="Does the sprout-box sage know how many days mung beans take to sprout?",
        must=(("sage:growbox", "growbox"),),
        must_not=("which sage", "no sage"),
        note=("A sage asked by a loose name. Passes when archsage takes 'the sprout-box sage' to be sage:growbox and "
              "answers from its tree or says the tree does not answer; it may not ask which sage was meant."),
    ),
    Probe(
        name="planner-other-project", agent="agautolab", role="superdirector", channel="pj-protoprey",
        topic="workplan-protoprey-sprouts",
        text=("Before you plan anything: where does the growbox study stand right now? I want a ProtoPrey location "
              "built around sprouting seeds and would reuse its findings."),
        must=(("pj-growbox", "growbox"), ("control loop", "control-loop", "m20402"),
              ("germination", "food safety", "food-safety")),
        must_not=("cannot see", "can't see", "no access"),
        note="An autolab planner asked about another project's state: passes when it reads the growbox study off the board.",
    ),
    Probe(
        name="entrance-plans", agent="agautolab", role="front", channel="autolab-agstudio1",
        topic="fixture-plans", speaker="Front",
        text="Where do all of your plans stand right now? One line per project, please.",
        must=(("pj-aisvgs", "aisvgs"), ("pj-growbox", "growbox"), ("pj-protoprey", "protoprey"),
              ("m20402", "control loop", "control-loop"), ("m20455", "v0.2", "locations", "go-ahead")),
        note=("autolab's entrance (as10: an entrance that answered from one project missed another). Passes when "
              "every project with missions is named, with the running control-loop mission and the v0.2 plan "
              "waiting for the Developer."),
    ),
    Probe(
        name="receipt-owed", agent="agfront", role="desk", channel="front", topic="front-desk-20260928-0800",
        text="", speaker="agobserver-agstudio1",
        must=(),
        tools_must=(("agentchat receipt",), ("--repair",)),
        tools_must_not=("[selfnote][receipt]", "[selfnote][served]"),
        note=("The conversation is on the board: Front's answer from autolab was taken up (a later post of Front's "
              "quotes it) but no receipt was written — what failsafe p6's `exit-before-receipt` leaves — and "
              "Observer asks. Passes when the run inspects with `agentchat receipt` and repairs with `--repair` (the "
              "fixture refuses the write; the attempt is the act judged), and never writes a receipt line itself."),
    ),
    Probe(
        name="hold-release", agent="agfront", role="desk", channel="front", topic="front-desk-20260928-0900",
        text="", speaker="Omni Agent",
        must=(),
        tools_must=(("agentchat release",), ("agentchat disposition",)),
        tools_must_not=("[selfnote][hold]", "[selfnote][disposition]"),
        claims=True,
        note=("The conversation is on the board: the Developer's proxy asked for a hold, Front recorded it, and the "
              "proxy now releases it and ends the request. Passes when the run records both — `agentchat release` "
              "and `agentchat disposition`. Since failsafe p7 the writes are kept on the trial's overlay and the listener's "
              "claim check reads the reply against them (`claims` in the outcome: a correct run is `clean`). p2's "
              "live trial (#15842) failed here: one turn, no tool call, a reply saying both were recorded."),
    ),
    Probe(
        name="delegate-answer", agent="agfront", role="desk", channel="front", topic="front-desk-fixture-m20390",
        text="growbox の発芽の調査（m20390）、終わるまでにどれくらいかかって、途中で詰まったところはあったのか、autolab に聞いて教えて。",
        script=(Canned("autolab-agstudio1", "{asker} m20390 took 41 minutes of work. It stalled once, for 12 minutes: "
                       "one of the germination papers (source 7) was paywalled, so I replaced it with the authors' "
                       "open preprint. Answers #{ask}.\n\n`ag-post intent=report re={ask}`",
                       topics=("workplan-", "workrun-")),),
        must=(("41",), ("paywall", "有料", "ペイウォール", "preprint", "プレプリント")),
        tools_must=(("agentchat send",),),
        servings_min=2,
        note=("Front delegates: how m20390 went is autolab's own experience, on no post of the board (the board has "
              "only its start and its done line). The fixture's responder answers the first post Front sends to "
              "autolab (a mention, autolab's channel, or a workplan-/workrun- topic) with a canned report; the "
              "conversation is then served again as the listener serves a callback. Passes when the callback "
              "serving reports the 41 minutes and the paywalled source. (A first version asked about the running "
              "control loop; Front read it as a status poll of running work and asked nobody, as board.md's as9 "
              "sentence says.)"),
    ),
    Probe(
        name="delegate-decision", agent="agfront", role="desk", channel="front", topic="front-desk-fixture-lights",
        text=("growbox の制御ループ（m20402）の照明を1日何時間点けるか、autolab に決めてもらって結果を教えて。"
              "どちらかを選ぶよう聞かれたら、電気代を抑えるほうで答えておいて。"),
        script=(Canned("autolab-agstudio1", "{asker} For the light schedule I need a decision: 16 h a day (faster "
                       "greening, about twice the power) or 12 h a day (slower, half the power). Which one?\n\n"
                       "`ag-post intent=response_request to=15 ask=decision`", topics=("workplan-", "workrun-")),
                Canned("autolab-agstudio1", "{asker} Set: lights 12 h a day (06:00–18:00) in "
                       "`main/control/schedule.toml` at `b41d0e7`; the task goes on with it. Answers #{ask}.\n\n"
                       "`ag-post intent=report re={ask}`", topics=("workplan-", "workrun-"))),
        must=(("12 h", "12時間", "12 hours", "12h", "06:00", "b41d0e7"),),
        must_not=("16時間にしました", "16 h a day was set"),
        tools_must=(("agentchat send",),),
        sends_must=(("12 h", "12時間", "12 hours", "12h", "12-hour", "twelve"),),
        servings_min=3,
        note=("Front answers a response_request from the fixture agent. autolab's scripted first answer asks Front to "
              "choose (16 h or 12 h of light); the Developer said to take the cheaper one. Passes when a later serving "
              "sends autolab the 12-hour choice and the last one reports what autolab set."),
    ),
    Probe(
        name="triage-unopened", agent="agobserver", role="triage", channel="pj-protoprey",
        topic="workplan-protoprey-locations", speaker="agobserver-agstudio1",
        text="",
        must=(("legit",),),
        note=("Observer's triage asked about a request it did not open: autolab asked the Developer for the go-ahead "
              "and nobody has answered. Passes on `legit` (the next move is the Developer's and they were asked)."),
    ),
)}


def _found(text: str, spellings: tuple[str, ...]) -> str | None:
    folded = text.casefold()
    for spelling in spellings:
        if spelling.casefold() in folded:
            return spelling
    return None


def judge(probe: Probe, reply: str, tool_calls: list[str] | None = None, *, sends: list[str] | None = None,
          servings: int = 1) -> dict:
    """Which of the probe's facts the reply (and its tool calls) carries, and
    whether it passes. For a scripted probe `reply` is the last serving's,
    `tool_calls` every serving's, `sends` what the served agent sent, and
    `servings` how many there were."""
    met = [(spellings, _found(reply, spellings)) for spellings in probe.must]
    against = [s for s in probe.must_not if s.casefold() in reply.casefold()]
    missing = [" / ".join(spellings) for spellings, hit in met if hit is None]
    calls = "\n".join(tool_calls or ())
    for spellings in probe.tools_must:
        if not any(s in calls for s in spellings):
            missing.append("tool call: " + " / ".join(spellings))
    against += [f"tool call: {s}" for s in probe.tools_must_not if s in calls]
    for spellings in probe.sends_must:
        if not any(_found(post, spellings) for post in sends or ()):
            missing.append("sent: " + " / ".join(spellings))
    if servings < probe.servings_min:
        missing.append(f"servings: {servings} of at least {probe.servings_min}")
    observed = {" / ".join(spellings): _found(reply, spellings) for spellings in probe.observe}
    return {"probe": probe.name, "passed": not missing and not against,
            "met": [hit for _, hit in met if hit is not None], "missing": missing, "against": against,
            "observed": observed, "chars": len(reply)}
