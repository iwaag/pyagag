"""The claims reader's measurement (`agag.fixture.reader`, agent_guide p3 step 4)."""

from __future__ import annotations

from agag.claims import Claim, ReaderError
from agag.fixture import reader as R


class Echo:
    """Reads the acts named on a line `acts: a,b`; fails on 'broken'."""

    def __init__(self):
        self.texts: list[str] = []

    def read(self, text):
        self.texts.append(text)
        if "broken" in text:
            raise ReaderError("no answer")
        line = next((l for l in text.splitlines() if l.startswith("acts:")), "acts:")
        return [Claim(act) for act in line[5:].split(",") if act]


def test_the_cases_are_data_in_the_package_and_name_only_known_acts():
    from agag.claims import ACTS

    found = R.cases()
    assert len(found) == 13
    assert len({case.name for case in found}) == 13
    assert all(case.want <= set(ACTS) for case in found)
    assert sum(1 for case in found if not case.want) >= 5


def test_the_reader_is_given_the_words_the_listener_gives_it():
    echo = Echo()
    case = R.Case("x", frozenset({"send"}), "@**Developer**\n\nacts:send\n\n`ag-post intent=report end=1`")
    R.measure(echo, 1, only=[case])
    assert echo.texts == ["acts:send"]


def test_a_set_equal_reading_is_right_and_a_failed_one_is_an_error_never_right():
    echo = Echo()
    only = [R.Case("two", frozenset({"release", "disposition"}), "acts:disposition,release,release"),
            R.Case("none", frozenset(), "nothing done"),
            R.Case("miss", frozenset({"hold"}), "acts:send"),
            R.Case("broken", frozenset(), "broken")]
    ticks = iter(range(100))
    result = R.measure(echo, 2, only=only, clock=lambda: next(ticks))
    rights = {row["case"]: [r["right"] for r in row["readings"]] for row in result["rows"]}
    assert rights == {"two": [True, True], "none": [True, True], "miss": [False, False], "broken": [False, False]}
    assert (result["right"], result["readings"], result["errors"]) == (4, 8, 2)
    assert result["seconds_max"] == 1
