from datetime import date
from pathlib import Path

from agag import intro


def test_intro_text_is_the_committed_markdown_plus_a_stamp(tmp_path):
    source = tmp_path / "intro.md"
    source.write_text(
        "# agforge\n\nOpen an `assetplan-…` topic in `{instance}`.\n", encoding="utf-8"
    )

    assert intro.intro_text(
        source, tmp_path, "agforge-agstudio1", date(2026, 8, 20), "3939f26"
    ) == (
        "# agforge\n\nOpen an `assetplan-…` topic in `agforge-agstudio1`.\n\n---\n"
        "Posted: 2026-08-20\nRevision: `3939f26`\n"
    )


def test_a_file_without_the_placeholder_is_posted_as_it_stands(tmp_path):
    source = tmp_path / "intro.md"
    source.write_text("nothing to fill in\n", encoding="utf-8")
    text = intro.intro_text(source, tmp_path, "autolab-agstudio1", date(2026, 8, 21), "abc")
    assert text.startswith("nothing to fill in\n\n---\n")


def test_intro_topic_is_the_append_only_per_instance_topic():
    assert intro.intro_topic("autolab-agstudio1") == "intro-autolab-agstudio1"


def test_post_intro_appends_to_the_shared_board(tmp_path, monkeypatch):
    sent = []

    class Client:
        def send_to_channel(self, channel, topic, text):
            sent.append((channel, topic, text))

    source = tmp_path / "intro.md"
    source.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(intro, "revision", lambda root: "abc1234")

    text = intro.post_intro(
        Client(), instance="autolab-agstudio1", intro_path=source, root=tmp_path
    )

    assert sent == [("agents", "intro-autolab-agstudio1", text)]
    assert text.startswith("hello\n\n---\nPosted: ")
    assert text.endswith("Revision: `abc1234`\n")


def test_revision_is_honest_outside_a_repository(tmp_path):
    assert intro.revision(tmp_path) in {"unknown", intro.revision(Path.cwd())}


# --- the roster block (operation_room p2) ----------------------------------
#
# The observer these serve cannot read an `AgentSpec` or a node's ignored
# `.local/instance.toml`, so everything it routes by arrives through this
# block. p1 measured what guessing costs: 66 phantom stalled rows.


def roster(**overrides) -> intro.Roster:
    fields = {
        "instance": "agforge-agstudio1",
        "agent": "agforge",
        "bot": "agforge-agstudio1",
        "bot_id": 13,
        "channel": "agforge-agstudio1",
        "prefixes": ("assetplan-", "assetrun-"),
    }
    fields.update(overrides)
    return intro.Roster(**fields)


def test_a_roster_survives_being_written_and_read_back():
    original = roster()
    assert intro.parse_roster(intro.roster_block(original)) == original


def test_the_block_is_fenced_so_it_is_not_prose_and_not_a_mention():
    block = intro.roster_block(roster())
    assert f"```{intro.ROSTER_FENCE}" in block
    assert "@" not in block


def test_a_front_shaped_roster_states_a_channel_that_need_not_exist():
    # Front's instance is `front-agstudio1`, its Zulip name is `Front`, and no
    # channel of either name exists. The post states what the listener matches
    # on; deciding whether it exists is the reader's job.
    parsed = intro.parse_roster(
        intro.roster_block(
            roster(instance="front-agstudio1", agent="front", bot="Front", bot_id=15,
                   channel="front-agstudio1", prefixes=("front-",))
        )
    )
    assert parsed.bot == "Front"
    assert parsed.channel == "front-agstudio1"


def test_an_introduction_without_a_block_parses_as_unknown_not_as_empty():
    # None and "no prefixes" must not be the same answer: one is an agent that
    # sweeps nothing, the other is an agent nobody can route for.
    assert intro.parse_roster("# agecho\n\nAsk me anything.\n") is None
    assert intro.parse_roster(intro.roster_block(roster(prefixes=()))).prefixes == ()


def test_the_last_block_wins_so_a_quoted_example_cannot_outrank_the_real_one():
    text = (
        "Here is what one looks like:\n\n"
        + intro.roster_block(roster(instance="example-agent"))
        + "\n\nand here is mine:\n\n"
        + intro.roster_block(roster())
    )
    assert intro.parse_roster(text).instance == "agforge-agstudio1"


def test_a_block_that_cannot_say_who_it_is_about_says_nothing():
    assert intro.parse_roster(f"```{intro.ROSTER_FENCE}\nagent: agforge\n```") is None


def test_intro_text_puts_the_block_after_the_prose_and_before_the_stamp(tmp_path):
    source = tmp_path / "intro.md"
    source.write_text("# {instance}\n\nAsk me anything.\n", encoding="utf-8")
    text = intro.intro_text(
        source, tmp_path, "agforge-agstudio1", date(2026, 9, 6), "abc", roster=roster()
    )
    assert text.index("Ask me anything.") < text.index("```agag-roster") < text.index("\n---\n")
    assert intro.parse_roster(text) == roster()


def test_an_intro_posted_without_a_roster_is_unchanged(tmp_path):
    source = tmp_path / "intro.md"
    source.write_text("hello\n", encoding="utf-8")
    assert intro.intro_text(source, tmp_path, "x", date(2026, 9, 6), "abc") == (
        "hello\n\n---\nPosted: 2026-09-06\nRevision: `abc`\n"
    )
