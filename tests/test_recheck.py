"""failsafe p4 step 4: re-checking one stopped unit of work before acting on
a stop report. Only the owner's posts in that conversation count."""

from __future__ import annotations

from agag.agent import SWEEP_ACK
from agag.trace import recheck, recheck_lines

OWNER, REQ, FRONT = 11, 15, 99
CH, TOPIC = "work-m1", "workrun-task1-m1"


def msg(i, sender, content, ts=None):
    return {"id": i, "sender_id": sender, "sender_full_name": {OWNER: "autolab", REQ: "Front", FRONT: "Omni"}[sender],
            "display_recipient": CH, "subject": TOPIC, "content": content, "timestamp": ts or 1000 + i}


class Client:
    def __init__(self, messages):
        self.messages = messages

    def message(self, message_id, strict=False):
        return next((m for m in self.messages if m["id"] == message_id), None)

    def topic_history(self, channel, topic, num_before=50):
        return [m for m in self.messages if (m["display_recipient"], m["subject"]) == (channel, topic)]


STOPPED = [
    msg(1, OWNER, "[selfnote][task] 1#1"),
    msg(2, OWNER, "# the task"),
    msg(3, REQ, "go"),
    msg(4, OWNER, SWEEP_ACK),
    msg(5, OWNER, "🔧 Bash: make\n\n`ag-post intent=progress`"),
]


def test_nothing_since_the_stopped_serving_is_stopped():
    result = recheck(Client(STOPPED), 1, 4)
    assert result.verdict == "stopped" and result.ack == 4
    assert "resume it" in recheck_lines(result)[-1]


def test_front_s_own_activity_elsewhere_is_not_the_work_moving():
    elsewhere = dict(msg(6, REQ, SWEEP_ACK), display_recipient="front", subject="front-x")
    assert recheck(Client([*STOPPED, elsewhere]), 1, 4).verdict == "stopped"


def test_a_resume_posted_and_not_yet_acknowledged_is_asked():
    result = recheck(Client([*STOPPED, msg(6, REQ, "please resume")]), 1, 4)
    assert result.verdict == "asked" and result.pending == [6]


def test_a_new_serving_is_resuming_until_it_works_then_resumed():
    starting = [*STOPPED, msg(6, REQ, "please resume"), msg(7, OWNER, SWEEP_ACK)]
    assert recheck(Client(starting), 1, 4).verdict == "resuming"
    working = [*starting, msg(8, OWNER, "🔧 Bash: make\n\n`ag-post intent=progress`")]
    result = recheck(Client(working), 1, 4)
    assert result.verdict == "resumed" and result.work == 8
    assert "do not post" in recheck_lines(result)[-1]


def test_a_closed_record_is_finished():
    done = [*STOPPED, msg(6, OWNER, "[selfnote][state] completed")]
    assert recheck(Client(done), 1, 4).verdict == "finished"


def test_an_unreadable_anchor_concludes_nothing():
    assert recheck(Client(STOPPED), 404, 4).verdict == "unreadable"
