import pytest

from agag.document import TITLE_LIMIT, DocumentError, compose, split


@pytest.mark.parametrize(
    "text,title,body",
    [
        ("# Ship it\n\nUse A & B.\nVerify.", "Ship it", "Use A & B.\nVerify."),
        ("\n\n## Second level\nbody", "Second level", "body"),
        ("no heading here\nrest of it", "no heading here", "rest of it"),
        ("# Only a title\n", "Only a title", ""),
        ("### Trailing hashes ###\nbody", "Trailing hashes", "body"),
    ],
)
def test_split_takes_the_first_heading_and_keeps_everything_else(text, title, body):
    assert split(text) == (title, body)


def test_split_truncates_a_long_title_and_refuses_an_empty_file():
    title, _ = split("# " + "x" * 400)
    assert len(title) == TITLE_LIMIT
    with pytest.raises(DocumentError):
        split("\n  \n")


def test_compose_inverts_split():
    document = compose("Build it", "With A & B.\nThen verify.")
    assert document == "# Build it\n\nWith A & B.\nThen verify.\n"
    assert split(document) == ("Build it", "With A & B.\nThen verify.")
    assert compose("Bare", None) == "# Bare\n"
    assert compose("Bare", "   ") == "# Bare\n"


