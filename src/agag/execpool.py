"""The usage pool an execution option actually consumes, derived not declared.

`ag.exec-options.v1` says an option advertises a **pool** — the provider whose
account the harness spends — because that is what makes a condition like
"until agy's usage exceeds 70 %" answerable at all: an option and a budget
observation that are not of the same pool do not compare.

Until `refactor` p3 ex1 every agent wrote its pools down by hand. The names
were filtered against the configured profiles, so a *name* could not be
advertised without a profile behind it — but the pool beside the name was a
string in a tuple, and nothing checked it against the harness that would
actually run. The default's was the worst of them: `pool: anthropic` was
literally true of four agents and true *by coincidence*, because each one's
`[roles.*].profile` happened to point at `claude_code`. Move one role to
`agy` in an instance overlay and every published default keeps saying
`anthropic` while half the work spends the Antigravity account.

So this module derives the pool from the same resolution the run will use:

    option -> profile (the agent's private mapping, per role)
           -> harness (`agents.toml` + the instance overlay)
           -> pool    (`agent_config.HARNESS_PROVIDER`)

for **every role the option covers**, and reports what it found. Four rules,
each of them a thing the plan asked for:

- **The overlay counts.** `resolve_role` reads it, so a role whose profile the
  machine's own `agents.local.toml` moved is derived where it was moved to.
  That is the whole reason a declaration can be wrong while the code is right.

- **Mixed is a truthful answer, not an error.** An agent whose default sends
  planning to `claude_code` and task work to `agy` really does spend two
  accounts, and `pool: anthropic+antigravity` says so. Forcing one name would
  make the menu lie in the one case where the lie costs the most.

- **Unavailable is not invalid.** A harness whose binary or secret is missing
  right now is a *runtime* failure of that one option (`E_UNAVAILABLE`, and
  the contract keeps it distinct). The pool is still knowable from the
  configuration, so derivation runs with `check_available=False` and
  availability is reported separately. One CLI that is not installed must not
  take the menu — or any unrelated conversation — down with it.

- **A wrong declaration is worth saying out loud.** `diagnose` compares what
  an agent declares against what resolves and names the role, the profile and
  the harness that disagree, so the message says what to change rather than
  that something is wrong.

The pool vocabulary stays provider-level, which is this environment's
convention: one account per provider. If a realm ever runs two accounts
behind one provider, the distinction belongs in the observation and in the
execution, not in a registry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from .agent_config import AgentConfigError, HARNESS_PROVIDER, resolve_role
from .execopt import DEFAULT_OPTION, NONE, Option

#: What a role whose pool cannot be said contributes to a mixed declaration.
#: Written down rather than dropped: an option covering a role nobody can
#: price is not the same as one covering only priced roles, and `agfront.budget`
#: already prints `pool unknown` for the same reason.
UNKNOWN = "unknown"
#: How several pools are joined into one field. `+` rather than a comma
#: because a comma reads as a list of alternatives and this is a sum: the
#: option spends *both* accounts.
JOIN = "+"

__all__ = [
    "JOIN", "UNKNOWN", "OptionPools", "RolePool",
    "derive", "diagnose", "pool_of", "with_derived_pools",
]


def pool_of(harness: str | None) -> str | None:
    """The account a harness spends, or None when it cannot be said.

    `HARNESS_PROVIDER` is the authority, and `agcode` is deliberately absent:
    its account follows its model, so it has no fixed pool. None is never
    guessed — a threshold matched to the wrong window is worse than one that
    says it cannot be judged.
    """
    return HARNESS_PROVIDER.get(str(harness or ""))


@dataclass(frozen=True)
class RolePool:
    """What one role resolves to under one option."""

    role: str
    profile: str | None = None
    harness: str | None = None
    pool: str | None = None
    #: The harness is known but not runnable right now (`E_UNAVAILABLE`).
    #: A runtime fact about this option, never a fault in the contract.
    unavailable: str | None = None
    #: The configuration itself could not answer for this role.
    error: str | None = None

    @property
    def token(self) -> str:
        return self.pool or UNKNOWN


@dataclass(frozen=True)
class OptionPools:
    """One published option, and what each role it covers actually spends."""

    option: str
    roles: tuple[RolePool, ...] = ()

    @property
    def pools(self) -> tuple[str, ...]:
        """The distinct pools, in the order the roles were asked about.

        Order follows the roles rather than the alphabet so the first name is
        the one that answers the entrance, which is what a reader skimming a
        menu takes for "mostly this".
        """
        seen: list[str] = []
        for role in self.roles:
            if role.token not in seen:
                seen.append(role.token)
        return tuple(seen)

    @property
    def pool(self) -> str:
        """The `pool:` field: one name, several joined by `+`, or `-`.

        `-` only when nothing at all could be resolved — an unreadable or
        broken configuration. A single unknown role among known ones is
        `anthropic+unknown`, because dropping it would round a partly
        unanswerable option up to a fully answerable one.
        """
        found = self.pools
        if not found or all(role.error for role in self.roles):
            return NONE
        return JOIN.join(found)

    @property
    def unavailable(self) -> tuple[str, ...]:
        """One line per role whose harness is configured but not runnable."""
        return tuple(
            f"{role.role} ({role.profile}/{role.harness}): {role.unavailable}"
            for role in self.roles if role.unavailable
        )

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(
            f"{role.role}: {role.error}" for role in self.roles if role.error
        )


def derive(
    option: str | None,
    roles: Sequence[str],
    config: Mapping,
    overlay: Mapping,
    profile_for: Callable[[str | None, str], str | None],
) -> OptionPools:
    """What `option` really spends, role by role.

    `profile_for(option, role)` is the agent's own private mapping — the
    public name to an `agents.toml` profile — and None from it means "no
    override", which is how `default` is derived: the role resolves through
    the overlay and the committed configuration exactly as an unselected
    serving would.

    Resolution is deliberately `check_available=False`. The pool is a fact
    about the configuration, and asking whether a CLI happens to be installed
    would turn a missing binary into an unpublishable option.
    """
    name = option or DEFAULT_OPTION
    found: list[RolePool] = []
    for role in roles:
        override = profile_for(option, role)
        try:
            resolved = resolve_role(
                dict(config), dict(overlay), role,
                profile_override=override, check_available=False,
            )
        except AgentConfigError as error:
            found.append(RolePool(role, override, error=str(error)))
            continue
        except Exception as error:  # a malformed config is a diagnosis, not a crash
            found.append(RolePool(role, override, error=f"{type(error).__name__}: {error}"))
            continue
        unavailable: str | None = None
        try:
            resolve_role(
                dict(config), dict(overlay), role,
                profile_override=override, check_available=True,
            )
        except AgentConfigError as error:
            unavailable = str(error)
        found.append(RolePool(
            role, resolved.profile, resolved.harness,
            pool_of(resolved.harness), unavailable,
        ))
    return OptionPools(name, tuple(found))


def with_derived_pools(
    options: Iterable[Option],
    roles: Sequence[str],
    config: Mapping,
    overlay: Mapping,
    profile_for: Callable[[str | None, str], str | None],
) -> tuple[tuple[Option, ...], tuple[OptionPools, ...]]:
    """`options` with every `pool` replaced by the derived one.

    The published block is what a requester acts on, so it carries the
    derived value and never the declaration: a menu that cannot lie is worth
    more than a menu that is checked. The findings come back beside it so the
    caller can say *why* a pool reads the way it does.
    """
    listed = list(options)
    findings = tuple(
        derive(None if option.name == DEFAULT_OPTION else option.name,
               roles, config, overlay, profile_for)
        for option in listed
    )
    derived = tuple(
        Option(option.name, finding.pool, option.covers, option.summary)
        for option, finding in zip(listed, findings)
    )
    return derived, findings


def diagnose(
    options: Iterable[Option],
    findings: Iterable[OptionPools],
) -> tuple[str, ...]:
    """Every declared pool that disagrees with what resolves, said usefully.

    The message names the option, the two pools, and the role/profile/harness
    behind each derived one, because "the pool is wrong" is not actionable
    and "supercoder resolves through agy, whose pool is antigravity" is.

    Availability failures are **not** here: an option whose CLI is missing
    has a correct contract and a runtime problem, and mixing the two is how
    one uninstalled binary ends up reading as a broken menu.
    """
    lines: list[str] = []
    for option, finding in zip(options, findings):
        if option.pool == finding.pool:
            continue
        where = ", ".join(
            f"{role.role} -> {role.profile or 'configured default'}"
            f"/{role.harness or 'unresolved'} ({role.token})"
            for role in finding.roles
        )
        lines.append(
            f"option {option.name!r} declares pool {option.pool!r} but resolves "
            f"to {finding.pool!r}: {where}"
        )
    return tuple(lines)
