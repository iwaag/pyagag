"""failsafe p1: a serving's end is on record, and unfinished work has a holder.

m11741 (2026-09-26) ended its only serving on a reply that said the work
was still running, and nothing was. Its conversation could not say so: a
serving's last word looked like a live progress line, and a progress post
Zulip had cut read as the task's answer. These are the reads that make
"nobody holds this" a fact rather than a judgment.
"""

from agag import trace as tracing
from agag.post import CUT_NOTE, MAX_CONTENT, PROGRESS, PostMeta, compose, is_truncated, parse_post
from agag.selfnote import owed_start

from test_trace import Realm

DEV, FRONT, AUTOLAB, FORGE = 8, 15, 11, 12
NAMES = {DEV: "Developer", FRONT: "Front", AUTOLAB: "autolab-agstudio1", FORGE: "agforge-agstudio1"}
ACK = "Message received. Please wait for the reply."


class Posts:
    def __init__(self):
        self.messages = []

    def __call__(self, channel, topic, text, sender, at):
        ident = 1000 + len(self.messages)
        self.messages.append({"id": ident, "channel": channel, "topic": topic, "sender_id": sender,
                              "sender_full_name": NAMES[sender], "sender_realm_str": "", "timestamp": at,
                              "content": text})
        return ident


def task_world(final: str, *, extra=None):
    """Front's conversation, a task autolab started for Front, one serving."""
    post = Posts()
    origin = post("front", "front-a", "Please do it.", DEV, 100)
    post("front", "front-a", ACK, FRONT, 101)
    post("work-m1", "workrun-task1-m9", "[selfnote][task] 9#1", AUTOLAB, 110)
    post("work-m1", "workrun-task1-m9", f"[selfnote][rootchat] front/front-a #{origin}", AUTOLAB, 110)
    post("work-m1", "workrun-task1-m9", "# Task 1", AUTOLAB, 110)
    post("work-m1", "workrun-task1-m9", f"[selfnote][start] #{origin} for {FRONT} Front", AUTOLAB, 111)
    ack = post("work-m1", "workrun-task1-m9", ACK, AUTOLAB, 112)
    post("work-m1", "workrun-task1-m9", "🔧 Bash: make\n\n`ag-post intent=progress`", AUTOLAB, 200)
    if extra:
        extra(post, ack)
    last = post("work-m1", "workrun-task1-m9", final.format(ack=ack), AUTOLAB, 300)
    return post.messages, origin, ack, last


def task_node(messages, origin, now=400):
    result = tracing.trace(Realm(10**9, messages), origin, now=now)
    return next(n for n in result.nodes() if n.topic.endswith("workrun-task1-m9")), result


# --- the wire -------------------------------------------------------------------------


def test_end_is_part_of_the_line_and_read_back():
    text = compose("Still going.", PostMeta(intent=PROGRESS, end=42))
    assert text.endswith("`ag-post intent=progress end=42`")
    assert parse_post(text).meta == PostMeta(intent=PROGRESS, end=42)
    assert parse_post(compose("ok", PostMeta(end=7))).meta == PostMeta(end=7), "a serving's end alone is a line"


def test_a_long_post_is_cut_before_its_line_never_through_it():
    text = compose("x" * 20000, PostMeta(intent=PROGRESS))
    assert len(text) <= MAX_CONTENT
    assert CUT_NOTE in text and parse_post(text).meta == PostMeta(intent=PROGRESS)


def test_a_post_zulip_cut_is_output_not_the_answer_to_a_start():
    messages = [
        {"id": 1, "sender_id": AUTOLAB, "content": f"[selfnote][start] #0 for {FRONT} Front"},
        {"id": 2, "sender_id": AUTOLAB, "content": ACK},
        {"id": 3, "sender_id": AUTOLAB, "content": "🔧 Bash: a\n🔧 Bash: b\n[message truncated]"},
    ]
    assert is_truncated(messages[2]["content"])
    assert owed_start(messages, AUTOLAB, is_ack=lambda c: c == ACK) is not None


# --- execution and holder ------------------------------------------------------------


def test_a_serving_that_ended_saying_work_goes_on_is_held_by_nobody():
    messages, origin, ack, last = task_world("@**Front** still running; I'll report.\n\n"
                                             "`ag-post intent=progress end={ack}`")
    task, result = task_node(messages, origin, now=300 + tracing.THRESHOLDS["unheld"])
    assert (task.execution, task.holder, task.ended_by) == ("ended", "none", last)
    assert [c.kind for c in tracing.stall_candidates(result, now=300 + tracing.THRESHOLDS["unheld"])] == ["unheld"]
    assert "NOBODY holds the next move" in "\n".join(tracing.trace_lines(result))


def test_the_grace_passes_before_a_candidate():
    messages, origin, *_ = task_world("@**Front** still running.\n\n`ag-post intent=progress end={ack}`")
    _, result = task_node(messages, origin, now=310)
    assert tracing.stall_candidates(result, now=310) == []


def test_a_live_serving_is_its_owners_whatever_it_says():
    messages, origin, *_ = task_world("🔧 Bash: pytest\n\n`ag-post intent=progress`")
    task, result = task_node(messages, origin, now=2000)
    assert (task.execution, task.holder) == ("open", "owner")
    assert "unheld" not in [c.kind for c in tracing.stall_candidates(result, now=2000)]


def test_a_report_ends_the_serving_and_hands_the_move_back():
    messages, origin, *_ = task_world("@**Front** done.\n\n`ag-post intent=report end={ack}`")
    task, _ = task_node(messages, origin)
    assert (task.execution, task.holder) == ("ended", "requester")


def test_an_answer_without_the_mark_still_ends_a_serving():
    """Owners that post nothing between their ack and their reply."""
    messages, origin, *_ = task_world("@**Front** done.")
    task, _ = task_node(messages, origin)
    assert task.execution == "ended" and task.holder == "requester"


def test_work_delegated_elsewhere_is_held_by_the_delegate():
    def delegate(post, ack):
        post("agforge-agstudio1", "assetplan-x", "[selfnote][rootchat] work-m1/workrun-task1-m9 #1004",
             AUTOLAB, 250)
        post("agforge-agstudio1", "assetplan-x", "@**agforge-agstudio1** an icon please", AUTOLAB, 250)
        post("agforge-agstudio1", "assetplan-x", ACK, FORGE, 251)

    messages, origin, *_ = task_world("@**Front** waiting on forge.\n\n`ag-post intent=progress end={ack}`",
                                      extra=delegate)
    task, result = task_node(messages, origin, now=5000)
    assert task.holder == "delegate"
    assert "unheld" not in [c.kind for c in tracing.stall_candidates(result, now=5000)]


def test_a_wait_that_goes_round_in_a_circle_is_held_by_nobody():
    """The task waits for Front, which took the answer up and then ended
    its own serving saying it waits for the task."""
    post = Posts()
    origin = post("front", "front-a", "Please run it.", DEV, 100)
    post("front", "front-a", ACK, FRONT, 100)
    post("routine-x", "routinerun-1", f"[selfnote][rootchat] front/front-a #{origin}", FRONT, 101)
    run_ack = post("routine-x", "routinerun-1", ACK, FRONT, 101)
    post("work-m1", "workrun-task1-m9", "[selfnote][task] 9#1", AUTOLAB, 110)
    post("work-m1", "workrun-task1-m9", f"[selfnote][rootchat] routine-x/routinerun-1 #{run_ack}", FRONT, 110)
    post("work-m1", "workrun-task1-m9", "@**autolab-agstudio1** start", FRONT, 111)
    ack = post("work-m1", "workrun-task1-m9", ACK, AUTOLAB, 112)
    answer = post("work-m1", "workrun-task1-m9", f"@**Front** Done; please accept.\n\n"
                                                 f"`ag-post intent=response_request to={FRONT} end={ack}`",
                  AUTOLAB, 200)
    ack2 = post("routine-x", "routinerun-1", ACK, FRONT, 201)
    post("routine-x", "routinerun-1", f"Waiting for the task.\n\n`ag-post intent=progress end={ack2}`", FRONT, 202)
    post("routine-x", "routinerun-1", f"[selfnote][served] work-m1/workrun-task1-m9 {answer}", FRONT, 202)
    result = tracing.trace(Realm(10**9, post.messages), origin, now=900)
    run = next(n for n in result.nodes() if n.topic == "routinerun-1")
    task = next(n for n in result.nodes() if n.topic == "workrun-task1-m9")
    assert task.taken_up and task.holder == "requester"
    assert run.holder == "none"
    assert [(c.kind, c.topic) for c in tracing.stall_candidates(result, now=900)] == [("unheld", "routinerun-1")]


def test_quiet_unfinished_work_whose_holder_cannot_be_read_is_a_judged_candidate():
    messages, origin, *_ = task_world("@**Front** here is a report.\n\n`ag-post intent=report end={ack}`")
    quiet_at = 300 + tracing.THRESHOLDS["quiet"]
    _, result = task_node(messages, origin, now=quiet_at)
    found = [c for c in tracing.stall_candidates(result, now=quiet_at) if c.kind == "quiet"]
    assert found and found[0].judgment
    _, result = task_node(messages, origin, now=quiet_at - 60)
    assert not [c for c in tracing.stall_candidates(result, now=quiet_at - 60) if c.kind == "quiet"]


def test_a_person_being_asked_is_not_a_quiet_stall():
    messages, origin, *_ = task_world("@**Front** here is a report.\n\n`ag-post intent=report end={ack}`")
    messages.append({"id": 5000, "channel": "front", "topic": "front-a", "sender_id": FRONT,
                     "sender_full_name": "Front", "sender_realm_str": "", "timestamp": 320,
                     "content": "@**Developer** accept it?\n\n`ag-post intent=response_request to=8 ask=confirmation`"})
    quiet_at = 320 + tracing.THRESHOLDS["quiet"]
    _, result = task_node(messages, origin, now=quiet_at)
    assert not [c for c in tracing.stall_candidates(result, now=quiet_at) if c.kind == "quiet"]


def test_a_watch_asked_for_in_the_conversation_itself_holds_it():
    """The supercoder guide's ComfyUI wait: the notifier acks with a
    reaction and answers by mentioning the owner when the job ends."""
    def watch(post, ack):
        post("work-m1", "workrun-task1-m9", "@**Comfy Notifier** watch 5f3a render of scene 2", AUTOLAB, 250)

    messages, origin, *_ = task_world("@**Front** rendering; I continue when it ends.\n\n"
                                      "`ag-post intent=progress end={ack}`", extra=watch)
    task, result = task_node(messages, origin, now=5000)
    assert task.waiting_on == ["Comfy Notifier"] and task.holder == "delegate"
    assert "unheld" not in [c.kind for c in tracing.stall_candidates(result, now=5000)]


def test_once_a_person_is_asked_about_the_stall_it_is_theirs():
    messages, origin, *_ = task_world("@**Front** still running.\n\n`ag-post intent=progress end={ack}`")
    at = 300 + tracing.THRESHOLDS["unheld"]
    messages.append({"id": 5000, "channel": "front", "topic": "front-a", "sender_id": FRONT,
                     "sender_full_name": "Front", "sender_realm_str": "", "timestamp": at - 10,
                     "content": "@**Developer** the task stopped; resume it?\n\n"
                                "`ag-post intent=response_request to=8 ask=question`"})
    _, result = task_node(messages, origin, now=at)
    assert "unheld" not in [c.kind for c in tracing.stall_candidates(result, now=at)]
