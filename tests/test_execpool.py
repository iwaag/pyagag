"""Deriving an execution option's usage pool from what actually resolves.

The failure this module exists to prevent: a menu that says `pool: anthropic`
while half the covered roles spend the Antigravity account, because the pool
was a string in a tuple and nothing ever compared it to the harness.
"""

import tomllib

import pytest

from agag.agent import AgentSpec
from agag.execopt import NONE, Option
from agag.execpool import UNKNOWN, derive, diagnose, pool_of, with_derived_pools

CONFIG = """
schema = "ag.agent-config.v2"
project = "demo"

[models."anthropic/claude-sonnet-5"]
[models."antigravity/gemini-3.8-flash-medium"]
[models."ollama/qwen"]

[profiles.sonnet]
harness = "claude_code"
model = "anthropic/claude-sonnet-5"

[profiles.agy]
harness = "agy"
model = "antigravity/gemini-3.8-flash-medium"

[profiles.local]
harness = "agcode"
model = "ollama/qwen"

[roles.front]
profile = "sonnet"
requires = []
allowed_tools = "Read"

[roles.worker]
profile = "sonnet"
requires = []
allowed_tools = "Read"
"""

ROLES = ("front", "worker")


def config(text=CONFIG):
    return tomllib.loads(text)


def overlay(text='schema = "ag.agent-config.v2"\n'):
    return tomllib.loads(text)


def identity(option, _role):
    """The default private mapping: the option *is* the profile name."""
    return option


def test_pool_of_reads_the_harness_provider_table():
    assert pool_of("claude_code") == "anthropic"
    assert pool_of("agy") == "antigravity"
    # agcode's account follows its model, so it has no fixed pool and must
    # never be guessed at.
    assert pool_of("agcode") is None
    assert pool_of(None) is None


def test_the_default_is_derived_from_the_roles_own_profiles():
    found = derive(None, ROLES, config(), overlay(), identity)
    assert found.option == "default"
    assert found.pool == "anthropic"
    assert [role.profile for role in found.roles] == ["sonnet", "sonnet"]


def test_an_option_is_derived_from_the_profile_it_maps_to():
    assert derive("agy", ROLES, config(), overlay(), identity).pool == "antigravity"


def test_the_instance_overlay_moves_the_derived_default():
    """The whole reason a hand-written pool goes wrong.

    A machine's own `agents.local.toml` moves one role to another harness and
    every declared `pool: anthropic` keeps saying `anthropic` while that role
    spends somebody else's account.
    """
    moved = overlay(
        'schema = "ag.agent-config.v2"\n[roles.worker]\nprofile = "agy"\n'
    )
    found = derive(None, ROLES, config(), moved, identity)
    assert found.pool == "anthropic+antigravity"
    assert [role.harness for role in found.roles] == ["claude_code", "agy"]


def test_a_mixed_default_is_said_truthfully_rather_than_rounded():
    moved = overlay(
        'schema = "ag.agent-config.v2"\n[roles.front]\nprofile = "agy"\n'
    )
    found = derive(None, ROLES, config(), moved, identity)
    # Order follows the roles, so the entrance's pool is named first: that is
    # what a reader skimming a menu takes for "mostly this".
    assert found.pools == ("antigravity", "anthropic")
    assert found.pool == "antigravity+anthropic"


def test_a_role_whose_pool_cannot_be_said_is_kept_as_unknown():
    """`agcode` spends whatever its model does, so it has no pool.

    Dropping it would round a partly unanswerable option up to a fully
    answerable one, which is the one rounding a threshold must never survive.
    """
    moved = overlay(
        'schema = "ag.agent-config.v2"\n[roles.worker]\nprofile = "local"\n'
    )
    found = derive(None, ROLES, config(), moved, identity)
    assert found.pool == f"anthropic{'+'}{UNKNOWN}"


def test_a_role_specific_mapping_is_honoured():
    """An option may mean different profiles for different roles."""

    def mapping(option, role):
        if option is None:
            return None
        return "agy" if role == "worker" else "sonnet"

    found = derive("cheap-worker", ROLES, config(), overlay(), mapping)
    assert found.pool == "anthropic+antigravity"
    assert found.roles[1].profile == "agy"


def test_a_broken_role_is_a_diagnosis_not_a_crash():
    found = derive("nonesuch", ("front",), config(), overlay(), identity)
    assert found.pool == NONE
    assert found.errors and "nonesuch" in found.errors[0]


def test_an_unavailable_harness_still_has_a_pool(monkeypatch):
    """Availability is a runtime fact, never a fault in the contract.

    A CLI that is not installed makes *that option* fail when it runs. It
    must not empty the pool, and it must not appear as a wrong declaration —
    one uninstalled binary taking a whole menu down is the failure the plan
    named.
    """
    from agag import execpool

    real = execpool.resolve_role

    def flaky(config_, overlay_, role, *, profile_override=None, check_available=True):
        resolved = real(config_, overlay_, role,
                        profile_override=profile_override, check_available=False)
        if check_available and resolved.harness == "agy":
            raise execpool.AgentConfigError("E_UNAVAILABLE", "agy is not installed")
        return resolved

    monkeypatch.setattr(execpool, "resolve_role", flaky)
    found = derive("agy", ROLES, config(), overlay(), identity)
    assert found.pool == "antigravity"
    assert not found.errors
    assert len(found.unavailable) == 2 and "E_UNAVAILABLE" in found.unavailable[0]
    # And it is not reported as a wrong declaration.
    assert diagnose([Option("agy", "antigravity")], [found]) == ()


def test_with_derived_pools_replaces_the_declaration_and_keeps_the_prose():
    declared = (
        Option("default", "anthropic", "everything", "my defaults"),
        Option("agy", "openai", "everything", "the wrong pool, on purpose"),
    )
    derived, findings = with_derived_pools(
        declared, ROLES, config(), overlay(), identity
    )
    assert [option.pool for option in derived] == ["anthropic", "antigravity"]
    assert [option.covers for option in derived] == ["everything", "everything"]
    assert [option.summary for option in derived] == [
        declared[0].summary, declared[1].summary
    ]
    lines = diagnose(declared, findings)
    assert len(lines) == 1
    assert "'agy' declares pool 'openai' but resolves to 'antigravity'" in lines[0]
    # The message says which role, profile and harness produced the answer.
    assert "front -> agy/agy (antigravity)" in lines[0]


def test_a_correct_declaration_produces_no_diagnostic():
    declared = (Option("default", "anthropic", "everything"),)
    derived, findings = with_derived_pools(
        declared, ROLES, config(), overlay(), identity
    )
    assert diagnose(declared, findings) == ()
    assert derived[0].pool == "anthropic"


# --- the spec's own view --------------------------------------------------


def write_spec(tmp_path, config_text=CONFIG, overlay_text='schema = "ag.agent-config.v2"\n'):
    (tmp_path / "agents.toml").write_text(config_text, encoding="utf-8")
    local = tmp_path / ".local"
    local.mkdir(exist_ok=True)
    (local / "agents.local.toml").write_text(overlay_text, encoding="utf-8")
    return AgentSpec(
        "demo", tmp_path,
        exec_options=(
            Option("default", "anthropic", "everything", "my defaults"),
            Option("agy", "antigravity", "everything", "the CLI"),
        ),
        exec_roles=ROLES,
    )


def test_published_options_carry_derived_pools(tmp_path):
    spec = write_spec(tmp_path)
    published = spec.published_options("Demo")
    assert published.get("default").pool == "anthropic"
    assert published.get("agy").pool == "antigravity"
    assert spec.pool_diagnostics() == ()


def test_a_published_menu_cannot_advertise_a_pool_the_overlay_moved(tmp_path):
    spec = write_spec(
        tmp_path,
        overlay_text='schema = "ag.agent-config.v2"\n[roles.worker]\nprofile = "agy"\n',
    )
    published = spec.published_options("Demo")
    # The declaration still says `anthropic`; what is *posted* does not.
    assert published.get("default").pool == "anthropic+antigravity"
    assert spec.exec_options[0].pool == "anthropic"
    lines = spec.pool_diagnostics()
    # The message names the *effective* profile, which is the actionable
    # half: "worker resolves through agy" says what to change.
    assert len(lines) == 1 and "worker -> agy/agy (antigravity)" in lines[0]


def test_an_agent_that_names_no_roles_publishes_what_it_declares(tmp_path):
    """Empty `exec_roles` is the honest answer for an agent that has not said
    which roles its options cover — the declarations stand, unverified."""
    spec = write_spec(tmp_path)
    spec = AgentSpec("demo", tmp_path, exec_options=spec.exec_options)
    assert spec.published_options("Demo").get("agy").pool == "antigravity"
    assert spec.pool_diagnostics() == ()


def test_an_unreadable_config_leaves_the_declarations_alone_and_says_so(tmp_path):
    """Degrading is fine; degrading silently is not.

    A configuration this instance cannot read is why the pools came back as
    declared, and reporting "no mismatch" for it would be the same silent
    pass the derivation exists to end.
    """
    spec = write_spec(tmp_path, config_text="this is not toml {{{")
    published = spec.published_options("Demo")
    assert published.get("default").pool == "anthropic"
    lines = spec.pool_diagnostics()
    assert len(lines) == 1 and "could not be derived" in lines[0]


@pytest.mark.parametrize("role_list", [("front",), ("front", "worker")])
def test_default_is_always_first_and_always_priced(tmp_path, role_list):
    spec = write_spec(tmp_path)
    spec = AgentSpec("demo", tmp_path, exec_options=spec.exec_options, exec_roles=role_list)
    published = spec.published_options("Demo")
    assert published.names[0] == "default"
    assert published.get("default").pool not in ("", NONE)
