"""The liveness status file every agag listener leaves behind (agent_intent p1 step 4).

One small file per workspace, `<workspace>/.local/agag-status.json`, rewritten
after every **successful** Zulip event poll:

```json
{"schema": "agag.status.v1", "last_poll_ok": "2026-08-17T12:00:00+00:00",
 "queue_id": "1234:5678", "last_error": null}
```

Three rules, each of which is the whole point:

- **Written only on success.** A poll that timed out, failed, or never
  returned leaves the previous file untouched, so the file cannot claim a
  liveness it does not have. Staleness is then the *absence* of a fresh write,
  which no bug in this module can fake.
- **Written atomically.** Temp file in the same directory, then `os.replace`,
  so a reader never sees a half-written document and a crash mid-write cannot
  destroy the last honest one.
- **Never fatal.** A listener that cannot write its status keeps listening;
  the write failure is logged once and recorded as `last_error` on the next
  successful attempt. An observability file that can take down the thing it
  observes is worse than no file.

Timestamps are the *node's* UTC clock. That is deliberate: the consumer
(`nodeutils`) computes the age on the same machine, where clock skew cancels,
rather than comparing a remote clock's claim against Nautobot's.

Default location: `$AGAG_STATUS_PATH` when set, otherwise
`<cwd>/.local/agag-status.json` — a listener started in its workspace becomes
observable with no configuration, which is what makes this part of the agag
protocol rather than a per-agent feature. Setting `AGAG_STATUS_PATH` to the
empty string turns the file off.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "agag.status.v1"
STATUS_FILENAME = "agag-status.json"
STATUS_PATH_ENV = "AGAG_STATUS_PATH"


def default_status_path(cwd: Path | None = None) -> Path | None:
    """Where this process should write its status, or None when disabled."""
    configured = os.environ.get(STATUS_PATH_ENV)
    if configured is not None:
        return Path(configured) if configured.strip() else None
    return (cwd or Path.cwd()) / ".local" / STATUS_FILENAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StatusWriter:
    """Writes one workspace's status file. Construct once per listener."""

    def __init__(self, path: Path | None, *, log=None, now=_now) -> None:
        self.path = Path(path) if path is not None else None
        self._log = log
        self._now = now
        self._last_error: str | None = None
        self._warned = False

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def record_error(self, error: str) -> None:
        """Remember a failure to report on the next successful poll.

        Deliberately does not write: a file rewritten on failure would make an
        agent that is failing every call look like an agent that is polling.
        """
        self._last_error = str(error)

    def record_poll_ok(self, queue_id: str | None) -> None:
        """Rewrite the status file after a poll that actually returned."""
        if self.path is None:
            return
        document = {
            "schema": SCHEMA,
            "last_poll_ok": self._now(),
            "queue_id": queue_id,
            "last_error": self._last_error,
        }
        try:
            self._write(document)
        except OSError as error:
            # Never fatal, and never repeated noise: one line per listener.
            if self._log is not None and not self._warned:
                self._warned = True
                self._log(f"cannot write status file {self.path}: {error!r}")
            return
        self._last_error = None

    def _write(self, document: dict) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.path.parent,
            prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
        )
        try:
            with handle:
                json.dump(document, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise


def read_status(path: Path) -> dict | None:
    """Read a status file, or None when it is absent or unreadable as JSON.

    An unreadable file is treated exactly like a missing one: a consumer must
    never turn a corrupt status into a liveness claim.
    """
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return None
    return document
