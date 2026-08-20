from datetime import date
from pathlib import Path

from agag import intro


def test_intro_text_is_the_committed_markdown_plus_a_stamp(tmp_path):
    source = tmp_path / "intro.md"
    source.write_text("# agforge\n\nOpen an `assetplan-…` topic.\n", encoding="utf-8")

    assert intro.intro_text(source, tmp_path, date(2026, 8, 20), "3939f26") == (
        "# agforge\n\nOpen an `assetplan-…` topic.\n\n---\n"
        "Posted: 2026-08-20\nRevision: `3939f26`\n"
    )


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
