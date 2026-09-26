"""The continuation view (`explicit_reply` p1 step 4, `agag.continuation`).

The plan's scenario, as a fixture: a multi-turn delegation with a restart
and history truncation, and a user correction posted while a delegate was
running. The next run's view must say what arrived, what is still pending,
and what the agent had decided to do — without any completed work having to
be redone — and must let the newer instruction override the older summary.
"""

from __future__ import annotations

from agag import continuation as c, serving, topics
from agag.selfnote import Conversation
from endmark import plain

BOT, DEV, AUTOLAB, FORGE = 15, 8, 11, 13


def msg(id, sender, name, content, realm=""):
    return {"id": id, "sender_id": sender, "sender_full_name": name, "content": content, "sender_realm_str": realm}


# --- the block and the note ---------------------------------------------------------


def test_the_block_is_read_off_the_output_and_never_posted():
    output = ("I'll wait for both.\n\n```ag-reply\nAsked autolab and forge; I'll report when both answer.\n```\n\n"
              "```ag-continue\ngoal: ship the trailer (per #7301)\nconditions: 30 s max, no music until forge confirms\n"
              "next: when autolab answers in #pj-x › workrun-1, tell the developer;\n  when forge answers, start the music\n```")
    rest, carried, error = c.split_continuation(output)
    assert error is None and "ag-continue" not in rest and rest.endswith("both answer.\n```")
    assert carried.fields == {
        "goal": "ship the trailer (per #7301)",
        "conditions": "30 s max, no music until forge confirms",
        "next": "when autolab answers in #pj-x › workrun-1, tell the developer;\nwhen forge answers, start the music",
    }
    note = c.continuation_note(carried, written_after=7305)
    assert note.startswith("[selfnote][continuation] {") and '"after": 7305' in note
    parsed = c.parse_continuation(note, 7310)
    assert parsed.fields == carried.fields and parsed.written_after == 7305 and parsed.message_id == 7310


def test_an_unreadable_block_is_an_error_and_the_output_is_still_usable():
    rest, carried, error = c.split_continuation("say\n```ag-continue\nthis is not a field\n```")
    assert carried is None and "unreadable line" in error and rest == "say"
    assert c.split_continuation("nothing here") == ("nothing here", None, None)
    assert c.parse_continuation("[selfnote][continuation] not json") is None
    assert c.parse_continuation("[selfnote][served] a/b 1") is None


def test_the_last_block_wins_and_the_newest_own_note_is_read():
    _, carried, _ = c.split_continuation("```ag-continue\ngoal: old\n```\ntext\n```ag-continue\ngoal: new\n```")
    assert carried.get("goal") == "new"
    history = [msg(1, BOT, "Front", c.continuation_note(c.Continuation({"goal": "first"}), 0)),
               msg(2, DEV, "Dev", c.continuation_note(c.Continuation({"goal": "forged"}), 0)),
               msg(3, BOT, "Front", c.continuation_note(c.Continuation({"goal": "second"}), 2))]
    found = c.latest_continuation(history, BOT)
    assert found.get("goal") == "second" and found.written_after == 2 and found.message_id == 3


# --- the states of a request made elsewhere --------------------------------------------


def test_remote_states_are_read_from_evidence_and_the_served_mark():
    ours = msg(10, BOT, "Front", "please do the task")
    theirs = msg(12, AUTOLAB, "autolab", "@**Front** done")
    awaiting = c.Remote(Conversation("pj-x", "workrun-1"), [ours], "workrun-1")
    assert c.remote_state(awaiting, BOT) == ("awaiting", ours)
    answered = c.Remote(Conversation("pj-x", "workrun-1"), [ours, theirs], "workrun-1")
    assert c.remote_state(answered, BOT) == ("answered", theirs)
    served = c.Remote(Conversation("pj-x", "workrun-1"), [ours, theirs], "workrun-1", served_up_to=12)
    assert c.remote_state(served, BOT) == ("served", theirs)
    finished = c.Remote(Conversation("pj-x", "workrun-1"), [ours, theirs], "✔ workrun-1")
    assert c.remote_state(finished, BOT) == ("finished", theirs)
    unreadable = c.Remote(Conversation("pj-x", "workrun-1"), [], "workrun-1", unavailable="ZulipError: down")
    assert c.remote_state(unreadable, BOT) == ("unreadable", None)
    # An ack of theirs is not a reply; a selfnote of theirs is not speech.
    noise = c.Remote(Conversation("pj-x", "workrun-1"), [ours, msg(11, AUTOLAB, "autolab", "[selfnote][task] 1")], "workrun-1")
    assert c.remote_state(noise, BOT)[0] == "awaiting"


# --- the plan's scenario -----------------------------------------------------------------


def scenario():
    """front-1: the developer asks for a trailer; Front delegates to autolab
    (#20) and forge (#21) and records its plan (#23, after #22). Then the
    developer posts a correction (#30) while both delegates run; autolab
    answers in its topic (#31); the listener restarts; the next serving is
    brought by autolab's answer with Front's last reply being #22."""
    home = [
        msg(1, DEV, "Dev", "(older) let's plan the trailer"),
        msg(2, BOT, "Front", "sure — what length?"),
        msg(3, DEV, "Dev", "30 seconds, and music later"),
        msg(18, DEV, "Dev", "make the trailer now"),
        msg(19, BOT, "Front", "Message received. Please wait for the reply."),
        msg(22, BOT, "Front", "@**Dev**\n\nAsked autolab (#pj-x › workrun-1) and forge (#agforge › assetplan-t); I'll report."),
        msg(23, BOT, "Front", c.continuation_note(c.Continuation({
            "goal": "a 30 s trailer (per #3)", "conditions": "music only after forge confirms",
            "next": "when autolab answers, tell the developer; when forge answers, ask about music"}), 22)),
        msg(30, DEV, "Dev", "correction: make it 20 seconds, not 30"),
    ]
    remotes = [
        c.Remote(Conversation("pj-x", "workrun-1"),
                 [msg(20, BOT, "Front", "please cut the trailer"), msg(31, AUTOLAB, "autolab", "@**Front** cut is done: v1.mp4")],
                 "workrun-1"),
        c.Remote(Conversation("agforge", "assetplan-t"), [msg(21, BOT, "Front", "music for the trailer?")], "assetplan-t"),
        c.Remote(Conversation("pj-x", "workrun-0"), [], "workrun-0", unavailable="ZulipError: timed out"),
    ]
    interrupted = serving.Serving(9, "front", "front-1", "owner", 30, state=serving.ACKED, ack_id=32, input_up_to=None)
    return home, remotes, interrupted


def test_the_view_says_what_arrived_what_is_pending_and_what_was_decided():
    home, remotes, interrupted = scenario()
    view = c.continuation_view(
        home, BOT, last_input_up_to=19, last_delivered_id=22, remotes=remotes,
        brought_by=("pj-x", "workrun-1", 31), interrupted=interrupted, omitted_before=18, omitted_count=3,
    )
    assert view.startswith("How this conversation stands") and c.BEGIN in view and view.endswith(c.END)
    # What arrived: the correction, not the older history, not our own posts.
    assert "Your last reply here is message 22; it answered everything up to message 19." in view
    assert "New here since then (1 post):" in view and "[Dev #30] correction: make it 20 seconds, not 30" in view
    assert "let's plan the trailer" not in view
    assert "brought by a post in #pj-x › workrun-1 (message 31)" in view
    # The summary, and the correction overriding it.
    assert "written after message 22" in view and "goal: a 30 s trailer (per #3)" in view
    assert "next: when autolab answers, tell the developer" in view
    assert "Posts newer than message 22 (listed above) override this" in view
    # Pending relationships, from evidence.
    assert "#pj-x › workrun-1: answered, not yet dealt with: [autolab #31] @**Front** cut is done: v1.mp4" in view
    assert "#agforge › assetplan-t: awaiting a reply to your post (message 21)." in view
    assert "#pj-x › workrun-0: could not be read (ZulipError: timed out); its state is unknown." in view
    # The interrupted serving, and what is not carried.
    assert "interrupted at stage 'acked' (acknowledged as message 32)" in view and "before repeating" in view
    assert "3 earlier messages of this conversation are not carried in the prompt (before message 18)" in view
    assert "1 thread could not be read; the state of that request is unknown, not settled." in view


def test_without_a_record_the_view_derives_the_boundary_from_the_last_reply():
    home, remotes, _ = scenario()
    view = c.continuation_view(home, BOT, remotes=remotes[:2])
    assert "Your last reply here is your newest post below." in view
    assert "[Dev #30] correction" in view and "(older)" not in view
    assert "Not carried here" not in view
    assert "interrupted" not in view


def test_a_served_answer_and_a_finished_thread_read_as_such():
    home, remotes, _ = scenario()
    remotes[0].served_up_to = 31
    remotes[1].live_name = "✔ assetplan-t"
    view = c.continuation_view(home, BOT, last_input_up_to=30, last_delivered_id=40, remotes=remotes[:2])
    assert "answered and already dealt with (up to message 31)" in view
    assert "#agforge › assetplan-t: finished (✔)." in view
    assert "Nothing new has been said here since then." in view


def test_a_new_conversation_says_so_plainly():
    view = c.continuation_view([msg(1, DEV, "Dev", "hi")], BOT)
    assert "You have not replied in this conversation yet." in view
    assert "[Dev #1] hi" in view
    assert "You have recorded no goal or next action for this conversation yet." in view
    assert "You have made no request in another conversation for this one." in view


def test_more_new_posts_than_the_limit_are_counted_not_dropped_silently():
    home = [msg(i, DEV, "Dev", f"post {i}") for i in range(1, 12)]
    view = c.continuation_view(home, BOT)
    assert "New here since then (11 posts):" in view and "… 3 earlier of them are not quoted here" in view
    assert "[Dev #11] post 11" in view and "[Dev #3] post 3" not in view


# --- through the skeleton -----------------------------------------------------------------


class Client:
    email = "bot@example.invalid"

    def __init__(self, history):
        self.history = history
        self.sent = []
        self.next_id = 100

    def whoami(self):
        return {"user_id": BOT, "full_name": "Front"}

    def topic_history(self, channel, topic, num_before):
        return list(self.history)

    def send_to_channel(self, channel, topic, content):
        self.next_id += 1
        self.history.append(msg(self.next_id, BOT, "Front", content))
        self.sent.append((topic, content))
        return self.next_id


def test_serve_topic_keeps_the_block_out_of_the_post_and_writes_the_note_after_the_reply():
    client = Client([msg(1, DEV, "Dev", "make it")])
    output = "```ag-reply\nOn it.\n```\n```ag-continue\ngoal: make it\nnext: report when done\n```"
    record = topics.serve_topic(client, "front", "front-1", lambda ctx: topics.TopicResult(output=output),
                                ack_text="ack", journal=serving.NullJournal(), log=lambda t: None)
    posted = [content for _, content in client.sent]
    assert plain(posted[1]) == "@**Dev**\n\nOn it." and "ag-continue" not in posted[1]
    assert posted[2].startswith("[selfnote][continuation] ") and record.delivered_id == 102
    found = c.latest_continuation(client.history, BOT)
    assert found.fields == {"goal": "make it", "next": "report when done"} and found.written_after == 101
    # The next serving reads it back into its view.
    view = c.continuation_view(client.history, BOT, last_input_up_to=101, last_delivered_id=102)
    assert "goal: make it" in view and "written after message 101" in view


def test_an_unreadable_block_becomes_a_notice_and_no_note():
    client = Client([msg(1, DEV, "Dev", "make it")])
    output = "```ag-reply\nOn it.\n```\n```ag-continue\nnot a field line\n```"
    topics.serve_topic(client, "front", "front-1", lambda ctx: topics.TopicResult(output=output),
                       ack_text="ack", journal=serving.NullJournal(), log=lambda t: None)
    posted = [content for _, content in client.sent]
    assert "(your ag-continue block was not readable" in posted[1] and len(posted) == 2


def test_the_guide_is_appended_only_when_asked_for():
    prompt = topics.prompt_with_guide(["p"], "g", reply=True, continuation=True)
    assert prompt.endswith(c.CONTINUATION_GUIDE) and "How your reply is posted" in prompt
    assert c.CONTINUATION_GUIDE not in topics.prompt_with_guide(["p"], "g", reply=True)
