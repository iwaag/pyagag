"""The liveness status file: written only on success, atomically, never fatally."""

import json
import os
import stat
from pathlib import Path

import pytest

from agag.status import (
    SCHEMA,
    STATUS_FILENAME,
    STATUS_PATH_ENV,
    StatusWriter,
    default_status_path,
    read_status,
)


def writer(tmp_path, **kwargs):
    return StatusWriter(tmp_path / ".local" / STATUS_FILENAME, **kwargs)


def test_a_successful_poll_writes_the_pinned_document(tmp_path):
    status = writer(tmp_path, now=lambda: "2026-08-17T12:00:00+00:00")
    status.record_poll_ok("queue-1")
    document = json.loads((tmp_path / ".local" / STATUS_FILENAME).read_text())
    assert document == {
        "schema": SCHEMA,
        "last_poll_ok": "2026-08-17T12:00:00+00:00",
        "queue_id": "queue-1",
        "last_error": None,
    }


def test_a_failure_alone_never_writes_a_file(tmp_path):
    status = writer(tmp_path)
    status.record_error("zulip call failed")
    assert not (tmp_path / ".local" / STATUS_FILENAME).exists()


def test_a_failure_does_not_refresh_an_existing_file(tmp_path):
    stamps = iter(["2026-08-17T12:00:00+00:00", "2026-08-17T12:05:00+00:00"])
    status = writer(tmp_path, now=lambda: next(stamps))
    status.record_poll_ok("queue-1")
    status.record_error("zulip call failed")
    document = json.loads((tmp_path / ".local" / STATUS_FILENAME).read_text())
    assert document["last_poll_ok"] == "2026-08-17T12:00:00+00:00"


def test_the_previous_failure_is_reported_on_the_next_success_then_cleared(tmp_path):
    status = writer(tmp_path, now=lambda: "2026-08-17T12:00:00+00:00")
    status.record_error("queue expired")
    status.record_poll_ok("queue-2")
    assert json.loads((tmp_path / ".local" / STATUS_FILENAME).read_text())["last_error"] == "queue expired"
    status.record_poll_ok("queue-2")
    assert json.loads((tmp_path / ".local" / STATUS_FILENAME).read_text())["last_error"] is None


def test_a_write_leaves_no_temporary_files_behind(tmp_path):
    status = writer(tmp_path)
    status.record_poll_ok("queue-1")
    assert [path.name for path in (tmp_path / ".local").iterdir()] == [STATUS_FILENAME]


def test_an_unwritable_location_is_logged_once_and_never_raises(tmp_path):
    target = tmp_path / "locked"
    target.mkdir()
    status = StatusWriter(target / ".local" / STATUS_FILENAME, log=lambda message: logged.append(message))
    logged = []
    target.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        status.record_poll_ok("queue-1")
        status.record_poll_ok("queue-1")
    finally:
        target.chmod(stat.S_IRWXU)
    assert len(logged) == 1
    assert "cannot write status file" in logged[0]


def test_a_disabled_writer_does_nothing(tmp_path):
    status = StatusWriter(None)
    assert status.enabled is False
    status.record_poll_ok("queue-1")
    assert list(tmp_path.iterdir()) == []


def test_default_path_follows_the_environment_and_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.delenv(STATUS_PATH_ENV, raising=False)
    assert default_status_path(tmp_path) == tmp_path / ".local" / STATUS_FILENAME
    monkeypatch.setenv(STATUS_PATH_ENV, str(tmp_path / "elsewhere.json"))
    assert default_status_path(tmp_path) == tmp_path / "elsewhere.json"
    monkeypatch.setenv(STATUS_PATH_ENV, "")
    assert default_status_path(tmp_path) is None


def test_read_status_refuses_a_missing_corrupt_or_foreign_document(tmp_path):
    assert read_status(tmp_path / "absent.json") is None
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json")
    assert read_status(corrupt) is None
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"schema": "something.else", "last_poll_ok": "now"}))
    assert read_status(foreign) is None


def test_read_status_round_trips_what_the_writer_wrote(tmp_path):
    status = writer(tmp_path, now=lambda: "2026-08-17T12:00:00+00:00")
    status.record_poll_ok("queue-1")
    assert read_status(tmp_path / ".local" / STATUS_FILENAME)["queue_id"] == "queue-1"
