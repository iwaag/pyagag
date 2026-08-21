"""The participation ledger: what a run posted, and on whose behalf.

This is the memory that survives the end of a run. Nothing here is clever —
what is pinned is that a remote conversation resolves back to the home one it
was opened for, that a half-written line costs nothing, and that a run with
no home records nothing at all.
"""

from agag import participation

HOME = participation.Conversation("work-s2-10", "workrun-task1-s2-10")
REMOTE = participation.Conversation("agforge-agstudio1", "assetplan-enemy-sprite")


def test_a_conversation_reads_and_writes_as_channel_slash_topic():
    assert str(REMOTE) == "agforge-agstudio1/assetplan-enemy-sprite"
    assert participation.parse_conversation(str(REMOTE)) == REMOTE


def test_a_topic_may_contain_a_slash_and_the_channel_may_not():
    parsed = participation.parse_conversation("work-a/task/1")
    assert parsed == participation.Conversation("work-a", "task/1")


def test_what_is_not_a_conversation_is_none():
    for text in (None, "", "   ", "no-separator", "/topic", "channel/"):
        assert participation.parse_conversation(text) is None


def test_the_home_comes_from_the_environment(tmp_path):
    assert participation.home_from_environment({}) is None
    assert participation.home_from_environment(
        {participation.HOME_VARIABLE: str(HOME)}
    ) == HOME


def test_the_ledger_path_falls_back_to_the_runs_own_directory():
    assert participation.ledger_from_environment({}) == participation.DEFAULT_LEDGER
    assert participation.ledger_from_environment(
        {participation.LEDGER_VARIABLE: "/tmp/x.jsonl"}
    ).as_posix() == "/tmp/x.jsonl"


def test_a_remote_conversation_resolves_to_the_home_it_was_opened_for(tmp_path):
    ledger = tmp_path / "nested" / "participations.jsonl"
    participation.record(ledger, remote=REMOTE, home=HOME, message_id=41)
    assert participation.home_for(ledger, *REMOTE.as_pair()) == HOME
    assert participation.home_for(ledger, "agforge-agstudio1", "assetplan-other") is None


def test_the_most_recent_entry_decides_a_reused_remote_topic(tmp_path):
    ledger = tmp_path / "participations.jsonl"
    later = participation.Conversation("work-s2-11", "workrun-task2-s2-11")
    participation.record(ledger, remote=REMOTE, home=HOME, message_id=1)
    participation.record(ledger, remote=REMOTE, home=later, message_id=2)
    assert participation.home_for(ledger, *REMOTE.as_pair()) == later


def test_the_threads_a_home_is_party_to_are_listed_once_each(tmp_path):
    ledger = tmp_path / "participations.jsonl"
    other = participation.Conversation("agforge-agstudio1", "assetrun-enemy-sprite")
    participation.record(ledger, remote=REMOTE, home=HOME, message_id=1)
    participation.record(ledger, remote=REMOTE, home=HOME, message_id=2)
    participation.record(ledger, remote=other, home=HOME, message_id=3)
    participation.record(
        ledger, remote=other,
        home=participation.Conversation("work-x", "workrun-task9-x"), message_id=4,
    )
    assert participation.remotes_for_home(ledger, *HOME.as_pair()) == [REMOTE, other]


def test_a_broken_last_line_costs_only_that_line(tmp_path):
    ledger = tmp_path / "participations.jsonl"
    participation.record(ledger, remote=REMOTE, home=HOME, message_id=1)
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write('{"remote": "half-writ')
    assert participation.home_for(ledger, *REMOTE.as_pair()) == HOME


def test_a_ledger_that_does_not_exist_is_simply_empty(tmp_path):
    assert participation.entries(tmp_path / "absent.jsonl") == []
    assert participation.remotes_for_home(tmp_path / "absent.jsonl", "a", "b") == []
