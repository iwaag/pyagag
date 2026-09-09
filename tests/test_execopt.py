"""`ag.exec-options.v1`: publishing, selecting, freezing, inheriting, refusing."""

import pytest

from agag import execopt
from agag.execopt import ExecOptions, Option, Selection
from agag.selfnote import Conversation, is_speech

BOT = "Autolab"
AGY = Option("agy", "antigravity", "everything", "Antigravity CLI, Gemini 3.8 Flash")
MENU = execopt.with_default(BOT, [AGY, Option("codex", "openai", "planning")])


def command(option, bot=BOT):
    return f"@**{bot}** use {option}"


def msg(id, content, sender_id=8, name="Developer"):
    return {"id": id, "content": content, "sender_id": sender_id, "sender_full_name": name}


# --- publishing ------------------------------------------------------------


def test_a_published_menu_round_trips_through_an_introduction():
    text = f"Some prose about me.\n\n{MENU.block()}\n\n---\nPosted: today\n"
    read = execopt.parse_options(text)
    assert read.supported is True
    assert read.bot == BOT
    assert read.names == ("default", "agy", "codex")
    assert read.get("agy") == AGY


def test_the_last_block_wins_so_a_quoted_example_cannot_outrank_the_real_one():
    quoted = ExecOptions("Somebody Else", (Option("wrong"),)).block()
    read = execopt.parse_options(f"Here is what one looks like:\n\n{quoted}\n\n{MENU.block()}")
    assert read.bot == BOT
    assert "wrong" not in read.names


def test_no_block_is_unknown_and_not_an_empty_menu():
    assert execopt.parse_options("I am an agent that predates this contract.") is None


def test_supported_no_is_a_different_answer_from_unknown():
    read = execopt.parse_options(ExecOptions(BOT, (), supported=False).block())
    assert read is not None and read.supported is False


def test_default_is_always_advertised_because_the_reset_names_it():
    assert execopt.with_default(BOT, []).names == ("default",)


# --- the command -----------------------------------------------------------


def test_a_bare_command_line_selects():
    assert execopt.parse_command(command("agy"), BOT) == "agy"


def test_a_command_addressed_to_somebody_else_is_not_ours():
    assert execopt.parse_command(command("agy", "Front"), BOT) is None


@pytest.mark.parametrize(
    "content",
    [
        "Front asked me to `@**Autolab** use agy` — is that still what you want?",
        "@**Autolab** use agy please, and then build the thing",
        "```\n@**Autolab** use agy\n```",
        "Do the work.",
        "",
    ],
)
def test_speech_that_merely_contains_the_command_is_not_a_command(content):
    assert execopt.parse_command(content, BOT) is None


def test_several_command_lines_in_one_message_end_at_the_last():
    assert execopt.parse_command(f"{command('codex')}\n\n{command('agy')}", BOT) == "agy"


# --- resolution and the freeze --------------------------------------------


def test_nothing_selected_is_a_selection_of_its_own():
    selection = execopt.resolve([msg(1, "Build it")], BOT)
    assert selection == Selection()
    assert selection.explicit is False


def test_the_newest_directive_wins():
    history = [msg(1, command("agy")), msg(2, "work"), msg(3, command("codex"))]
    assert execopt.resolve(history, BOT, known=MENU.names).option == "codex"


def test_reset_returns_to_the_defaults_rather_than_a_mode_of_its_own():
    history = [msg(1, command("agy")), msg(3, command("default"))]
    selection = execopt.resolve(history, BOT, known=MENU.names)
    assert selection.option is None
    assert selection.source == "topic" and selection.message_id == 3


def test_a_selection_is_frozen_at_the_servings_start():
    history = [msg(1, command("agy")), msg(9, command("codex"))]
    # The serving read the topic up to message 5; 9 arrived while it ran.
    assert execopt.resolve(history, BOT, known=MENU.names, up_to=5).option == "agy"
    assert execopt.resolve(history, BOT, known=MENU.names).option == "codex"


def test_an_unpublished_name_never_becomes_the_topics_setting():
    history = [msg(1, command("agy")), msg(3, command("opus"))]
    assert execopt.resolve(history, BOT, known=MENU.names).option == "agy"


# --- inheritance -----------------------------------------------------------


def test_a_snapshot_carries_the_option_and_where_it_came_from():
    parent = Conversation("pj-agdev", "workplan-x")
    note = execopt.exec_note("agy", parent, 5731)
    assert execopt.parse_exec_note(note) == ("agy", parent, 5731)
    selection = execopt.resolve([msg(4, note)], BOT, known=MENU.names)
    assert (selection.option, selection.source) == ("agy", "inherited")
    assert selection.inherited_from == parent
    assert selection.message_id == 5731


def test_a_snapshot_buys_nobody_a_run():
    assert is_speech({"content": execopt.exec_note("agy", Conversation("c", "t"), 1)}) is False


def test_a_child_overrides_its_snapshot_by_posting_a_command():
    history = [
        msg(4, execopt.exec_note("agy", Conversation("pj", "workplan-x"), 5731)),
        msg(7, command("codex")),
    ]
    assert execopt.resolve(history, BOT, known=MENU.names).option == "codex"


def test_a_later_parent_change_does_not_reach_a_child_already_snapshotted():
    # The child's own history is all a child ever reads; the parent's newer
    # command is not in it.
    child = [msg(4, execopt.exec_note("agy", Conversation("pj", "workplan-x"), 5731))]
    assert execopt.resolve(child, BOT, known=MENU.names).option == "agy"


# --- configuration-only ----------------------------------------------------


def test_a_message_of_nothing_but_commands_is_configuration_only():
    history = [msg(1, "Build it"), msg(2, "on it", sender_id=11), msg(3, command("agy"))]
    assert execopt.configuration_only(history, 11, BOT) == ["agy"]


def test_a_command_beside_unanswered_work_is_not_configuration_only():
    history = [msg(3, command("agy")), msg(4, "and then build it")]
    assert execopt.configuration_only(history, 11, BOT) is None


def test_work_already_answered_does_not_make_a_later_command_into_work():
    history = [msg(1, "Build it"), msg(2, "done", sender_id=11), msg(3, command("default"))]
    assert execopt.configuration_only(history, 11, BOT) == ["default"]


def test_snapshots_and_notices_are_not_pending_speech():
    history = [
        msg(2, "done", sender_id=11),
        msg(3, execopt.exec_note("agy", Conversation("c", "t"), 1)),
        {"id": 4, "content": "Developer has marked this topic as resolved",
         "sender_id": 99, "sender_realm_str": "zulipinternal"},
    ]
    assert execopt.pending_speech(history, 11) == []


# --- the record ------------------------------------------------------------


def test_the_record_says_what_was_asked_for_and_where_it_came_from():
    selection = execopt.resolve([msg(3, command("agy"))], BOT, known=MENU.names)
    assert execopt.run_meta(selection) == {
        "exec_source": "topic", "exec_option": "agy", "exec_message_id": 3,
    }


def test_a_run_nobody_selected_anything_for_still_records_that():
    assert execopt.run_meta(None) == {"exec_source": "default"}


# --- what gets posted ------------------------------------------------------


def test_a_refusal_names_the_menu_and_what_the_topic_still_runs_on():
    text = execopt.refusal("opus", MENU, current=Selection("agy", "topic", 3))
    assert "`opus`" in text and "`agy`" in text and "`codex`" in text
    assert "still runs on `agy`" in text


def test_a_refusal_with_nothing_selected_says_so_rather_than_naming_a_profile():
    assert "my configured defaults" in execopt.refusal("opus", MENU)


def test_a_confirmation_describes_the_option_it_accepted():
    text = execopt.confirmation("agy", MENU)
    assert "antigravity" in text and "Antigravity CLI" in text


def test_a_reset_is_confirmed_as_a_reset():
    assert "reset" in execopt.confirmation(None, MENU)


def test_a_restart_re_derives_the_same_selection_from_the_topic_alone():
    # There is no selection file: the command post *is* the store, so a
    # listener that lost everything reads the same answer back.
    history = [msg(1, "Build it"), msg(3, command("agy")), msg(4, "and again")]
    first = execopt.resolve(history, BOT, known=MENU.names)
    assert execopt.resolve(list(history), BOT, known=MENU.names) == first
    assert first.option == "agy"


def test_an_agent_may_publish_what_its_own_default_costs():
    # A default consumes a pool like anything else, and a threshold that
    # cannot name the pool it is judged against cannot be judged.
    mine = Option("default", "anthropic", "everything", "Claude Sonnet 5")
    menu = execopt.with_default(BOT, [AGY, mine])
    assert menu.names == ("default", "agy")
    assert menu.get("default").pool == "anthropic"
