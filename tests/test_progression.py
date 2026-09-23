"""An owner starting its own conversation, and the answer finding its way home
(`robust_workflow` p1 step 3).

adventure_game p3 lost 24 minutes to a task start that was reported and never
posted: the only thing that could start a task was somebody else's post in its
topic. Now the owner may start one itself with `[selfnote][start]`, and the
report at the end reaches the requester through the conversation the task was
opened for, even though the requester never posted in the task.
"""

from agag import selfnote, topics
from agag.selfnote import owed_start, parse_start, start_note
from agag.zulip import parent_rootchat, rootchat_home

OWNER, FRONT, DEV = 11, 15, 8
ACK = "Message received. Please wait for the reply."


def msg(id, sender, content, name=None):
    return {"id": id, "sender_id": sender, "sender_full_name": name or str(sender), "content": content}


def is_ack(content):
    return content == ACK


def test_the_start_note_round_trips():
    line = start_note(8409, FRONT, "Front")
    assert line == "[selfnote][start] #8409 for 15 Front"
    assert parse_start(line) == (8409, 15, "Front")
    assert parse_start("[selfnote][start] garbage") is None


def test_a_start_is_owed_until_the_owner_answers_after_it():
    history = [msg(1, OWNER, "Task 4 spec"), msg(2, OWNER, start_note(9, FRONT, "Front"))]
    assert owed_start(history, OWNER, is_ack)["sender_id"] == FRONT
    history.append(msg(3, OWNER, ACK))
    history.append(msg(4, OWNER, "🔧 Bash: git status\n💬 looking"))
    assert owed_start(history, OWNER, is_ack) is not None, "an ack and progress are a run that has not answered"
    history.append(msg(5, OWNER, "@**Front** task 4 is done"))
    assert owed_start(history, OWNER, is_ack) is None


def test_the_answer_to_a_self_started_conversation_names_the_requester():
    history = [msg(1, OWNER, "Task 4 spec"), msg(2, OWNER, start_note(9, FRONT, "Front")), msg(3, OWNER, ACK)]
    requester = topics.requester_of(history, OWNER, up_to=3)
    assert requester["sender_id"] == FRONT and topics.mention_of(requester) == "@**Front**"


def test_somebody_else_speaking_still_wins_over_the_note():
    history = [msg(1, OWNER, start_note(9, FRONT, "Front")), msg(2, DEV, "one more thing", "Developer")]
    assert topics.requester_of(history, OWNER, up_to=2)["sender_id"] == DEV


def test_a_self_started_topic_is_not_empty_for_the_empty_reply_guard():
    context = topics.TopicContext(None, "work-m1", "workrun-task2-m1", OWNER, "autolab")
    context.history = [msg(1, OWNER, "Task 2 spec")]
    assert not context.humans_spoke()
    context.history.append(msg(2, OWNER, start_note(9, FRONT, "Front")))
    assert context.humans_spoke()


class Realm:
    def __init__(self, topics):
        self.topics = topics

    def topic_history(self, channel, topic, num_before=50):
        return self.topics.get((channel, topic), [])


def test_a_callback_from_a_child_topic_finds_the_requester_through_its_parent():
    """The task topic holds autolab's root note naming the workplan; Front's
    own note is in the workplan. One hop finds Front's home."""
    realm = Realm({
        ("work-m1", "workrun-task2-m1"): [
            msg(1, OWNER, "[selfnote][task] 1#2"),
            msg(2, OWNER, "[selfnote][rootchat] pj-x/workplan-a"),
            msg(3, OWNER, "@**Front** task 2 is done"),
        ],
        ("pj-x", "workplan-a"): [
            msg(0, FRONT, "[selfnote][rootchat] front/front-a #99"),
            msg(4, FRONT, "Mission for autolab"),
        ],
    })
    home = rootchat_home(realm, "work-m1", "workrun-task2-m1", FRONT)
    assert (home.channel, home.topic, home.anchor) == ("front", "front-a", 99)


def test_our_own_note_in_the_child_always_wins():
    realm = Realm({
        ("work-m1", "workrun-task2-m1"): [
            msg(2, OWNER, "[selfnote][rootchat] pj-x/workplan-a"),
            msg(5, FRONT, "[selfnote][rootchat] front/front-b"),
        ],
        ("pj-x", "workplan-a"): [msg(0, FRONT, "[selfnote][rootchat] front/front-a")],
    })
    assert rootchat_home(realm, "work-m1", "workrun-task2-m1", FRONT).topic == "front-b"


def test_the_parent_hop_is_one_hop_and_never_a_guess():
    realm = Realm({
        ("work-m1", "workrun-task2-m1"): [msg(2, OWNER, "[selfnote][rootchat] pj-x/workplan-a")],
        ("pj-x", "workplan-a"): [msg(1, OWNER, "[selfnote][rootchat] front/front-z")],  # not ours
    })
    assert parent_rootchat(realm, realm.topics[("work-m1", "workrun-task2-m1")], FRONT) is None
    assert rootchat_home(realm, "work-m1", "workrun-task2-m1", FRONT) is None
