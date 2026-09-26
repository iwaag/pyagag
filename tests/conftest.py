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
