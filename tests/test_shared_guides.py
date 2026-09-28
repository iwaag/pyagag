"""Guide text several agents share, written once (`agent_guide` p2 step 3)."""

from __future__ import annotations

import pytest

from agag import argue
from agag.topics import SHARED_SECTIONS, GuideError, prompt_with_guide, shared_sections, shared_text


def test_every_shared_section_ships_and_has_text():
    for name in (*SHARED_SECTIONS, "entrance", "entrance_default", "argue_participant"):
        assert shared_text(name).strip()


def test_sections_come_after_the_guide_and_before_the_reply_section():
    prompt = prompt_with_guide(["placement"], "OWN GUIDE", reply=True, shared=("refs", "board"))
    own = prompt.index("OWN GUIDE")
    board = prompt.index("# The board")
    refs = prompt.index("# References the developer published")
    reply = prompt.index("# How your reply is posted")
    assert own < board < refs < reply


def test_no_shared_section_unless_named():
    assert prompt_with_guide(["placement"], "OWN GUIDE") == "placement\n\nOWN GUIDE"


def test_an_unknown_section_is_an_error_not_a_silence():
    with pytest.raises(GuideError):
        shared_sections(["borad"])


def test_a_section_named_twice_is_given_once():
    assert shared_sections(["refs", "refs"]).count("agrefs list") == 1


def test_argue_participants_get_the_references_pointer_once():
    prompt = argue.participant_prompt("autolab", "[Dev] hi", "You are autolab.")
    assert prompt.count("agrefs list") == 1
    assert prompt.index("You are autolab.") < prompt.index("agrefs list") < prompt.index("# How your reply is posted")


def test_the_participant_guide_is_the_shipped_text():
    assert argue.participant_guide() == shared_text("argue_participant")


def test_a_participant_whose_tools_differ_can_be_given_no_section():
    prompt = argue.participant_prompt("archsage", "[Dev] hi", "You are one sage.", shared=())
    assert "agrefs" not in prompt
