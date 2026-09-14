"""Shared test setup: no rate-limit pause outlives the test that caused it."""

import pytest

from agag.zulip import Budget


@pytest.fixture(autouse=True)
def _fresh_budgets():
    Budget.forget_all()
    yield
    Budget.forget_all()
