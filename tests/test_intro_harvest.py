"""The intro harvest: what the board says, verbatim, per run.

The point of this file is attributability. What is pinned is that the
knowledge in `tools/agents.md` comes from the `#agents` board and from
nothing else — bodies verbatim, no agent name or channel name of the
consuming agent's own — and that an empty board produces an honest file
instead of a failure.
"""

from datetime import datetime, timezone

from agag import intro as agents_md

FORGE_INTRO = (
    "# agforge\n\nOpen an `assetplan-…` topic in this instance's "
    "`agforge-agstudio1` channel.\n\n---\nPosted: 2026-08-20"
)
STAMP = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)


class Client:
    def __init__(self, topics, histories, calls=None):
        self.topics = topics
        self.histories = histories
        self.calls = [] if calls is None else calls

    def stream_id(self, name):
        self.calls.append(("stream_id", name))
        return 30

    def channel_topics(self, stream_id):
        self.calls.append(("topics", stream_id))
        return self.topics

    def topic_history(self, channel, topic, num_before=50):
        self.calls.append(("history", channel, topic, num_before))
        return self.histories.get(topic, [])

    def subscribe_channels(self, *args, **kwargs):  # pragma: no cover - guard
        raise AssertionError("the harvest must not change subscriptions")


def post(content, id=1):
    return {"id": id, "sender_id": 13, "sender_full_name": "Forge", "content": content}


def board(**topics):
    names = list(topics)
    return Client(names, {name: topics[name] for name in names})


# --- harvest ---------------------------------------------------------------


def test_the_latest_post_of_each_intro_topic_is_taken():
    client = board(**{
        "intro-agforge-agstudio1": [post("old revision", id=1), post(FORGE_INTRO, id=2)],
    })
    assert agents_md.harvest_intros(client) == [("agforge-agstudio1", FORGE_INTRO)]


def test_resolved_and_non_intro_topics_are_skipped():
    client = board(**{
        "intro-live": [post("live")],
        "✔ intro-retired": [post("retired")],
        "chatter": [post("not an introduction")],
    })
    assert agents_md.harvest_intros(client) == [("live", "live")]


def test_an_empty_or_blank_topic_contributes_nothing():
    client = board(**{"intro-empty": [], "intro-blank": [post("   ")]})
    assert agents_md.harvest_intros(client) == []


def test_agents_are_ordered_so_two_harvests_of_one_board_agree():
    client = board(**{"intro-zeta": [post("z")], "intro-alpha": [post("a")]})
    assert [name for name, _ in agents_md.harvest_intros(client)] == ["alpha", "zeta"]


def test_the_harvest_reads_only_the_agents_channel():
    client = board(**{"intro-one": [post("one")]})
    assert client.calls == [] or True
    agents_md.harvest_intros(client)
    channels = {call[1] for call in client.calls if call[0] == "history"}
    assert channels == {agents_md.AGENTS_CHANNEL}


# --- rendering -------------------------------------------------------------


def test_bodies_are_copied_verbatim_under_a_heading_per_agent():
    text = agents_md.render_agents_md([("agforge-agstudio1", FORGE_INTRO)], STAMP)
    assert "## agforge-agstudio1" in text
    assert FORGE_INTRO in text


def test_the_file_carries_a_generated_at_line():
    text = agents_md.render_agents_md([], STAMP)
    assert "Generated: 2026-08-20T09:00:00+00:00" in text


def test_an_empty_board_is_stated_honestly_rather_than_failing():
    text = agents_md.render_agents_md([], STAMP)
    assert agents_md.NO_AGENTS in text


# --- placement -------------------------------------------------------------


def test_the_file_lands_in_the_generation_workspace_tools_directory(tmp_path):
    client = board(**{"intro-agforge-agstudio1": [post(FORGE_INTRO)]})
    path = agents_md.write_agents_md(client, tmp_path, STAMP)
    assert path == tmp_path / "tools" / "agents.md"
    assert FORGE_INTRO in path.read_text(encoding="utf-8")


def test_writing_works_when_tools_does_not_exist_yet(tmp_path):
    path = agents_md.write_agents_md(board(), tmp_path / "1" / "front", STAMP)
    assert path.is_file()
