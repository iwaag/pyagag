"""`agag.argue` (`argue` p1 step 1): the conversation contract.

Pinned here: a mention is an invitation and a selector addresses one logical
speaker of an account; an invitation is outstanding until a reply under the
matching speaker header answers it, whatever else was posted meanwhile; the
human's desire is a human's post, never an agent's draft; a participant
serving answers every outstanding invitation without an ack and marks the
topic served afterwards; and the listener keeps an unanswered invitation
across other posts and a restart while leaving an answered one alone.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agag import argue
from agag.agent import AgentSpec
from agag.listen import MENTION, Listener
from agag.mirror import Mirror
from agag.mirror.testing import FakeRealm
from agag.selfnote import Conversation, is_speech

BOT, DEV, FRONT, OTHER = 42, 7, 15, 9
BOT_NAME = "Mirror Bot"


def message(ident, sender, name, content, realm=""):
    return {"id": ident, "sender_id": sender, "sender_full_name": name, "content": content,
            "sender_realm_str": realm, "subject": "argue-x", "display_recipient": "argue"}


# --- mentions, selectors, speakers -----------------------------------------------


def test_a_mention_may_carry_a_selector_and_a_fenced_one_is_not_a_mention():
    text = "@**archsage** sage:arxiv what is trending?  @**archsage** and you?\n```\n@**archsage** sage:none\n```"
    assert argue.mentions_of(text, "archsage") == ["sage:arxiv", None]
    assert argue.mentions_of(text, "Front") == []
    assert argue.mentions_of("@_**archsage** silently", "archsage") == []
    assert argue.mentions_of("@**cagent** please", "Cagent") == [None]
    assert argue.parse_selector("sage:arxiv") == "sage:arxiv"
    assert argue.parse_selector("Sage:arxiv") is None
    assert argue.parse_selector("") is None


def test_a_speaker_header_round_trips_and_the_account_itself_has_none():
    post = argue.with_speaker("sage:arxiv", "the answer")
    assert post.startswith("**[sage:arxiv]**\n")
    assert argue.speaker_of(post) == "sage:arxiv"
    assert argue.speaker_of("plain reply") is None
    assert argue.with_speaker(None, "  plain  ") == "plain"


def test_is_argue_topic_follows_the_resolve_rename():
    assert argue.is_argue_topic("argue", "argue-fish")
    assert argue.is_argue_topic("argue", "✔ argue-fish")
    assert not argue.is_argue_topic("front", "argue-fish")
    assert not argue.is_argue_topic("argue", "front-fish")


# --- outstanding invitations --------------------------------------------------------


def test_an_invitation_is_outstanding_until_a_reply_with_its_speaker_answers_it():
    rows = [
        message(1, DEV, "Dev", "I want something grand"),
        message(2, FRONT, "Front", "@**Mirror Bot** sage:arxiv trends? @**Mirror Bot** sage:realworld problems?"),
        message(3, BOT, BOT_NAME, argue.with_speaker("sage:arxiv", "papers say...")),
        message(4, DEV, "Dev", "interesting, go on"),
    ]
    left = argue.outstanding(rows, BOT, BOT_NAME)
    assert [(i.message_id, i.selector) for i in left] == [(2, "sage:realworld")]
    # A reply as the account itself does not answer a selected invitation.
    rows.append(message(5, BOT, BOT_NAME, "as myself"))
    assert [(i.message_id, i.selector) for i in argue.outstanding(rows, BOT, BOT_NAME)] == [(2, "sage:realworld")]
    rows.append(message(6, BOT, BOT_NAME, argue.with_speaker("sage:realworld", "the world says...")))
    assert argue.outstanding(rows, BOT, BOT_NAME) == []


def test_an_invitation_to_the_account_itself_is_answered_by_a_plain_reply_only():
    rows = [
        message(1, FRONT, "Front", "@**Mirror Bot** what can you observe?"),
        message(2, BOT, BOT_NAME, argue.with_speaker("sage:arxiv", "wrong hat")),
    ]
    assert [i.selector for i in argue.outstanding(rows, BOT, BOT_NAME)] == [None]
    rows.append(message(3, BOT, BOT_NAME, "I can observe files and endpoints"))
    assert argue.outstanding(rows, BOT, BOT_NAME) == []


def test_selfnotes_and_system_notices_are_neither_invitations_nor_answers():
    rows = [
        message(1, FRONT, "Front", "[selfnote][served] argue/argue-x 0 @**Mirror Bot**"),
        message(2, OTHER, "Notification Bot", "@**Mirror Bot** moved here", realm="zulipinternal"),
        message(3, FRONT, "Front", "@**Mirror Bot** please"),
        message(4, BOT, BOT_NAME, "[selfnote][served] argue/argue-x 3"),
    ]
    assert [i.message_id for i in argue.outstanding(rows, BOT, BOT_NAME)] == [3]


# --- the desire and the anchor --------------------------------------------------------


def test_the_desire_must_be_a_human_post_in_the_conversation():
    rows = [
        message(10, FRONT, "Front", "Draft: build a self-running aquarium factory?"),
        message(11, DEV, "Dev", "Yes, exactly that."),
        message(12, FRONT, "Front", "[selfnote][argue] from front/front-a"),
    ]
    humans = {DEV}
    is_human = humans.__contains__
    assert argue.validate_desire(rows, 11, self_id=FRONT, is_human=is_human) == (argue.Desire(11, DEV), None)
    _, why = argue.validate_desire(rows, 10, self_id=FRONT, is_human=is_human)
    assert "your own post" in why
    _, why = argue.validate_desire(rows, 12, self_id=FRONT, is_human=is_human)
    assert "note" in why
    _, why = argue.validate_desire(rows, 99, self_id=FRONT, is_human=is_human)
    assert "not in this conversation" in why
    rows.append(message(13, BOT, BOT_NAME, "I, a bot, adopt the draft"))
    _, why = argue.validate_desire(rows, 13, self_id=FRONT, is_human=is_human)
    assert "by a bot" in why


def test_desire_and_argue_notes_parse_and_the_earliest_wins():
    note = argue.desire_note(11, DEV)
    assert note == "[selfnote][desire] 11 by 7"
    assert argue.parse_desire(note) == argue.Desire(11, DEV)
    assert argue.parse_desire("[selfnote][desire] eleven by me") is None
    rows = [message(1, FRONT, "Front", argue.desire_note(11, DEV)), message(2, FRONT, "Front", argue.desire_note(12, DEV))]
    assert argue.recorded_desire(rows) == argue.Desire(11, DEV)
    assert argue.recorded_desire([]) is None

    anchor = argue.argue_note(Conversation("front", "front-a"))
    assert argue.parse_argue(anchor) == (True, Conversation("front", "front-a"))
    assert argue.parse_argue(argue.argue_note(None)) == (True, None)
    assert argue.parse_argue("[selfnote][argue] elsewhere") == (False, None)
    found = argue.anchor([message(5, FRONT, "Front", anchor), message(6, FRONT, "Front", argue.argue_note(None))])
    assert found == argue.Anchor(5, Conversation("front", "front-a"))
    assert argue.desire_placement(None).startswith("The human's desire has not been recorded")
    assert '"Yes"' in argue.desire_placement(argue.Desire(11, DEV), [message(11, DEV, "Dev", "Yes")])


def test_the_block_is_split_from_the_reply_and_a_bad_line_is_an_error():
    text, fields, error = argue.split_block("Thanks.\n\n```ag-argue\ndesire: 11\nComplete: false\n```\n")
    assert (text, fields, error) == ("Thanks.", {"desire": "11", "complete": "false"}, None)
    text, fields, error = argue.split_block("No block here")
    assert (text, fields, error) == ("No block here", None, None)
    text, fields, error = argue.split_block("x\n```ag-argue\nnonsense\n```")
    assert text == "x" and fields is None and "unreadable" in error


# --- opening and participating over a fake client --------------------------------------


class Client:
    """Enough of `ZulipClient` for opening an argue and taking part in one."""

    email = "mirror-bot@example"

    def __init__(self, messages=None, *, user=BOT, name=BOT_NAME):
        self.user, self.name = user, name
        self.messages = list(messages or [])
        self.sent: list[tuple[str, str, str]] = []
        self.reactions: list[int] = []
        self.subscribed: list[str] = []
        self.next_id = 500

    def whoami(self, refresh=False):
        return {"user_id": self.user, "full_name": self.name, "email": self.email}

    def ensure_subscribed(self, channel):
        self.subscribed.append(channel)
        return True

    def topic_last_id(self, channel, topic):
        ids = [m["id"] for m in self.messages if m["subject"] == topic]
        return max(ids) if ids else 0

    def topic_history(self, channel, topic, num_before=50):
        return [m for m in self.messages if m["subject"] == topic]

    def send_to_channel(self, channel, topic, content):
        ident = self.next_id
        self.next_id += 1
        self.messages.append({**message(ident, self.user, self.name, content), "subject": topic})
        self.sent.append((channel, topic, content))
        return ident

    def add_reaction(self, message_id, emoji_name="eyes"):
        self.reactions.append(message_id)


def test_open_argue_writes_the_anchor_first_and_refuses_a_stem_in_use():
    client = Client()
    topic, anchor_id, post_id = argue.open_argue(client, "fish", "Tell me your desire.", origin=Conversation("front", "front-a"))
    assert topic == "argue-fish" and post_id == anchor_id + 1
    assert client.subscribed == ["argue"]
    assert client.sent[0] == ("argue", "argue-fish", "[selfnote][argue] from front/front-a")
    assert client.sent[1] == ("argue", "argue-fish", "Tell me your desire.")
    with pytest.raises(ValueError):
        argue.open_argue(client, "argue-fish", "again", origin=None)
    with pytest.raises(ValueError):
        argue.open_argue(client, "  ", "x", origin=None)


def spec_in(tmp_path: Path) -> AgentSpec:
    return AgentSpec("mirror", tmp_path)


def test_participate_answers_each_outstanding_invitation_without_an_ack(tmp_path):
    client = Client([
        message(1, FRONT, "Front", "[selfnote][argue] from front/front-a"),
        message(2, DEV, "Dev", "I want a self-running aquarium factory."),
        message(3, FRONT, "Front", "[selfnote][desire] 2 by 7"),
        message(4, FRONT, "Front", "@**Mirror Bot** sage:arxiv trends? @**Mirror Bot** sage:realworld problems? "
                                   "@**Mirror Bot** sage:none anyone?"),
        message(5, DEV, "Dev", "(a human speaks in between)"),
    ])
    prompts = []

    def run(prompt, cwd, invitation):
        prompts.append((prompt, cwd, invitation))
        return f"answer for {invitation.selector}"

    posted = argue.participate(
        client, "argue", "argue-x", spec=spec_in(tmp_path), role_context="I am a sage.",
        selectors=("sage:arxiv", "sage:realworld"), run=run, log=lambda line: None,
    )
    assert len(posted) == 3
    bodies = [content for _, _, content in client.sent]
    assert bodies[0] == "**[sage:arxiv]**\nanswer for sage:arxiv"
    assert bodies[1] == "**[sage:realworld]**\nanswer for sage:realworld"
    assert bodies[2].startswith("**[sage:none]**\nThere is no participant 'sage:none'")
    assert bodies[3] == "[selfnote][served] argue/argue-x 4"
    assert not any("Message received" in body for body in bodies)
    assert client.reactions == [4, 4, 4]
    # Two runs, one per real speaker; the unknown one cost no run.
    assert [i.selector for _, _, i in prompts] == ["sage:arxiv", "sage:realworld"]
    prompt, cwd, _ = prompts[0]
    assert "logical participant 'sage:arxiv'" in prompt
    assert "desire on record is message 2" in prompt
    assert "I am a sage." in prompt
    assert "I want a self-running aquarium factory." in prompt
    assert (cwd / "chatlog.md").read_text(encoding="utf-8").count("[Dev]") == 2
    assert cwd.parent.parent.name == "argue-x" and cwd.name == "argue"
    # Nothing left: a second serving posts nothing at all.
    assert argue.participate(client, "argue", "argue-x", spec=spec_in(tmp_path), role_context="", run=run,
                             log=lambda line: None) == []


def test_participate_posts_a_failure_rather_than_silence(tmp_path):
    client = Client([message(1, FRONT, "Front", "@**Mirror Bot** please")])

    def run(prompt, cwd, invitation):
        raise RuntimeError("the harness is missing")

    argue.participate(client, "argue", "argue-x", spec=spec_in(tmp_path), role_context="", run=run,
                      log=lambda line: None)
    assert client.sent[0][2].startswith("failed while answering: the harness is missing")
    assert client.sent[1][2] == "[selfnote][served] argue/argue-x 1"


# --- the listener keeps invitations ------------------------------------------------------


class NoStatus:
    def record_error(self, error):
        pass

    def record_poll_ok(self, queue_id):
        pass


def wait_until(predicate, timeout=5.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class Participant:
    """A listener whose mention route answers argue invitations the way
    `participate` does: reply in place under the speaker header, then the
    served mark into the topic itself."""

    def __init__(self, realm: FakeRealm, root: Path, *, answer=True):
        self.realm = realm
        self.mirror = Mirror.open(root / "zulip.env", root / "mirror", client_factory=realm.facet,
                                  log=lambda line: None, start=True, resync_backoff=0.05)
        self.client = realm.facet()
        self.served: list[tuple[str, str]] = []
        self.answer = answer

        def mention(channel, topic):
            self.served.append((channel, topic))
            if not self.answer:
                return
            history = [m for m in realm.messages.values() if m["subject"] == topic]
            history.sort(key=lambda m: m["id"])
            pending = argue.outstanding(history, BOT, BOT_NAME)
            for invitation in pending:
                realm.post(channel, topic, argue.with_speaker(invitation.selector, "my answer"),
                           sender_id=BOT, sender_name=BOT_NAME)
            if pending:
                realm.post(channel, topic, f"[selfnote][served] {channel}/{topic} {max(i.message_id for i in pending)}",
                           sender_id=BOT, sender_name=BOT_NAME)

        self.listener = Listener(
            self.mirror, self.client, topic_filter=lambda channel, topic: channel == "mirror-bot-x",
            handler=lambda channel, topic: None, on_mention=mention,
            log=lambda line: None, status=NoStatus(), idle_seconds=0.2,
        )
        self.thread = threading.Thread(target=self.listener.run, daemon=True)

    def start(self):
        self.thread.start()
        wait_until(lambda: self.listener.recoveries >= 1, what="startup recovery")
        return self

    def stop(self):
        self.listener.stop()
        self.thread.join(3.0)
        self.mirror.stop()


def argue_realm() -> FakeRealm:
    realm = FakeRealm()
    realm.add_channel(35, "agents")
    realm.add_channel(8, "mirror-bot-x")
    realm.add_channel(90, "argue")
    return realm


def speech_in(realm, topic):
    return [m for m in sorted(realm.messages.values(), key=lambda m: m["id"]) if m["subject"] == topic and is_speech(m)]


def test_an_invitation_survives_other_posts_and_a_restart_and_an_answered_one_stays_quiet(tmp_path):
    realm = argue_realm()
    realm.post("argue", "argue-x", "[selfnote][argue] from front/front-a", sender_id=FRONT, sender_name="Front", quiet=True)
    realm.post("argue", "argue-x", "my desire", sender_id=DEV, sender_name="Dev", quiet=True)
    # Two participants named in one post; the other one answers first, then
    # the human speaks again — the last post names nobody.
    realm.post("argue", "argue-x", "@**Other Bot** you first. @**Mirror Bot** sage:arxiv and you?",
               sender_id=FRONT, sender_name="Front", quiet=True)
    realm.post("argue", "argue-x", "other's answer", sender_id=OTHER, sender_name="Other Bot", quiet=True)
    realm.post("argue", "argue-x", "thanks both", sender_id=DEV, sender_name="Dev", quiet=True)
    # A listener that was down through all of it recovers the invitation from the index.
    p = Participant(realm, tmp_path, answer=False).start()
    wait_until(lambda: ("argue", "argue-x") in p.served, what="the recovered invitation")
    p.stop()
    # Still unanswered after that restart; a second restart finds it again and answers.
    p2 = Participant(realm, tmp_path).start()
    wait_until(lambda: any(m["sender_id"] == BOT and argue.speaker_of(m["content"]) == "sage:arxiv"
                           for m in speech_in(realm, "argue-x")), what="the answer")
    time.sleep(0.4)
    assert p2.served.count(("argue", "argue-x")) == 1
    p2.stop()
    # Answered and marked: a third restart, and the owner replying without a
    # mention, serve nothing.
    realm.post("argue", "argue-x", "noted, thank you", sender_id=FRONT, sender_name="Front", quiet=True)
    p3 = Participant(realm, tmp_path).start()
    time.sleep(0.5)
    assert p3.served == []
    # A new invitation, live this time, is served once.
    realm.post("argue", "argue-x", "@**Mirror Bot** one more thing", sender_id=FRONT, sender_name="Front",
               flags=("mentioned",))
    wait_until(lambda: p3.served == [("argue", "argue-x")], what="the live invitation")
    wait_until(lambda: any(m["sender_id"] == BOT and argue.speaker_of(m["content"]) is None
                           for m in speech_in(realm, "argue-x")), what="the plain answer")
    time.sleep(0.4)
    assert p3.served == [("argue", "argue-x")]
    p3.stop()
    assert p3.client.calls == 1  # whoami; everything else came off the mirror


def test_a_resolved_argue_serves_nobody(tmp_path):
    realm = argue_realm()
    realm.post("argue", "argue-x", "@**Mirror Bot** last word?", sender_id=FRONT, sender_name="Front", quiet=True)
    realm.resolve("argue", "argue-x", quiet=True)
    p = Participant(realm, tmp_path).start()
    time.sleep(0.5)
    assert p.served == []
    assert [r for r in (MENTION,) if p.listener.queue.entries()] == []
    p.stop()


def test_a_role_context_may_depend_on_the_invitation(tmp_path):
    client = Client([message(1, FRONT, "Front", "@**Mirror Bot** sage:a one @**Mirror Bot** sage:b two")])
    prompts = []
    argue.participate(
        client, "argue", "argue-x", spec=spec_in(tmp_path),
        role_context=lambda invitation: f"CONTEXT FOR {invitation.selector}",
        run=lambda prompt, cwd, invitation: prompts.append(prompt) or "ok", log=lambda line: None,
    )
    assert "CONTEXT FOR sage:a" in prompts[0] and "CONTEXT FOR sage:b" in prompts[1]


def test_two_invitations_to_one_speaker_are_one_reply(tmp_path):
    client = Client([message(1, FRONT, "Front", "@**Mirror Bot** first?"), message(2, DEV, "Dev", "…"),
                     message(3, FRONT, "Front", "still waiting on @**Mirror Bot**")])
    runs = []
    posted = argue.participate(client, "argue", "argue-x", spec=spec_in(tmp_path), role_context="",
                               run=lambda prompt, cwd, invitation: runs.append(invitation.message_id) or "here",
                               log=lambda line: None)
    assert runs == [3] and len(posted) == 1 and client.reactions == [1, 3]
    assert client.sent[-1][2] == "[selfnote][served] argue/argue-x 3"
