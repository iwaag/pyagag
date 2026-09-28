"""Shared test setup: no rate-limit pause outlives the test that caused it."""

import pytest

from agag.zulip import Budget


@pytest.fixture(autouse=True)
def _fresh_budgets():
    Budget.forget_all()
    yield
    Budget.forget_all()


@pytest.fixture(autouse=True)
def _no_host_refs_config(tmp_path_factory, monkeypatch):
    """The developer's own `~/.config/agag/refs.toml` never reaches a test."""
    monkeypatch.setenv("AGREFS_HOST_CONFIG", str(tmp_path_factory.mktemp("hostcfg") / "refs.toml"))
    monkeypatch.delenv("AGREFS_CATALOG", raising=False)


@pytest.fixture(autouse=True)
def _no_host_people_config(tmp_path_factory, monkeypatch):
    """The host's `~/.config/agag/people.toml` never reaches a test: a test
    that wants a proxy writes its own."""
    monkeypatch.setenv("AGAG_PEOPLE_CONFIG", str(tmp_path_factory.mktemp("people") / "people.toml"))


@pytest.fixture(autouse=True)
def _no_host_claims_config(tmp_path_factory, monkeypatch):
    """The host's `~/.config/agag/claims.toml` never reaches a test: no test
    calls the host's local model (failsafe p7)."""
    monkeypatch.setenv("AGAG_CLAIMS_CONFIG", str(tmp_path_factory.mktemp("claims") / "claims.toml"))
