"""`ZulipClient`'s read calls, answered from a mirror store (`agent_guide` p2).

A run's `agentchat` used to read Zulip itself, one call per topic, while the
listener that started the run already held the whole public realm in its
mirror (`agag.mirror`). `AGENTCHAT_MIRROR` names that store
(`agag.agent.chat_environment` sets it to the listener's own), and the
commands that only look — `read`, `topics`, `channels`, `intro`, `options`,
`trace`, `agproject status` — are answered from it through this class,
opened read-only. Everything else (every write, and `recheck`, whose verdict
is acted on at once) keeps the live client.

The store is the listener's live copy, updated from its event queue as posts
arrive; it is not a snapshot. Where it cannot answer, the live client does:
a channel it does not hold (a private one), an archived channel whose topic
it never read, a message it never saw, or a store whose mirror has no queue
yet (it is being built). Anything this class does not implement is the live
client's too.

**A fixture board** is a store whose meta says `fixture=<name>`
(`agag.fixture`): a synthetic realm for repeatable guide trials. There is no
live side at all — nothing a run does can reach the real realm — so a write,
or a read the store cannot answer, fails with a line saying so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from agag.zulip import ZulipError

from .store import Store

__all__ = ["FIXTURE_GITEA", "FIXTURE_META", "FIXTURE_REPOSITORIES_META", "FixtureRefused", "MirrorReads"]

#: The store meta key a fixture board carries (its name).
FIXTURE_META = "fixture"
#: The fixture's repositories (JSON: slug → `main`'s revision), which
#: `agproject status` reads instead of the host's Gitea.
FIXTURE_REPOSITORIES_META = "fixture_repositories"
#: Where the fixture's repositories say they are: a host that resolves nowhere.
FIXTURE_GITEA = "https://gitea.fixture.invalid"


class FixtureRefused(ZulipError):
    """What a fixture board cannot do: write, or read beyond itself."""


class MirrorReads:
    """Read calls from `path`'s store; the rest from `live()` (never for a fixture)."""

    def __init__(self, path: Path, live: Callable[[], object] | None = None):
        self.path = Path(path)
        self.store = Store.open_readonly(self.path)
        self.fixture = self.store.get_meta(FIXTURE_META) or ""
        self._live_factory = live
        self._live = None

    # -- the live side ------------------------------------------------------

    def live(self):
        if self.fixture:
            raise FixtureRefused(f"this board is the fixture {self.fixture!r} ({self.path}): it is read-only "
                                 "and holds only what was built into it; nothing is posted or read elsewhere")
        if self._live is None:
            if self._live_factory is None:
                raise ZulipError(f"the mirror at {self.path} cannot answer this and there is no live client")
            self._live = self._live_factory()
        return self._live

    def __getattr__(self, name):
        # Only reached for what this class does not define: writes, users,
        # subscriptions, DMs — the live client's, or refused on a fixture.
        if name.startswith("_") or name in ("store", "path", "fixture"):
            raise AttributeError(name)
        return getattr(self.live(), name)

    @property
    def usable(self) -> bool:
        """A fixture always; a live mirror once its queue exists (built)."""
        if self.fixture:
            return True
        queue, _ = self.store.checkpoint()
        return queue is not None

    def repository(self, slug: str, org: str) -> dict:
        """A study's repository as `agag.project.gitea_head` reports one,
        from a fixture's own rows; a live mirror holds none."""
        if not self.fixture:
            raise ZulipError("only a fixture board holds repositories")
        found = json.loads(self.store.get_meta(FIXTURE_REPOSITORIES_META) or "{}").get(slug)
        repository = f"{FIXTURE_GITEA}/{org}/{slug}.git"
        if not found:
            return {"repository": repository, "exists": False}
        return {"repository": repository, "exists": True, "branch": "main", "revision": str(found), "empty": False}

    # -- identity -----------------------------------------------------------

    def whoami(self, refresh: bool = False) -> dict:
        if self.fixture:
            return {"user_id": int(self.store.get_meta("self_id") or 0),
                    "full_name": self.store.get_meta("full_name") or "", "email": self.store.get_meta("email") or "",
                    "is_bot": True}
        return self.live().whoami(refresh) if refresh else self.live().whoami()

    # -- channels -------------------------------------------------------------

    def channels(self, *, include_archived: bool = False) -> list[dict]:
        return [{"stream_id": c.stream_id, "name": c.name, "description": c.description, "folder_id": c.folder_id,
                 "is_archived": c.archived}
                for c in self.store.channels(include_archived=include_archived)]

    def _channel(self, name: str):
        return self.store.channel(name)

    def stream_id(self, name: str) -> int:
        found = self._channel(name)
        if found is None:
            if self.fixture:
                raise FixtureRefused(f"no channel {name!r} on the fixture board {self.fixture!r}")
            return self.live().stream_id(name)
        return found.stream_id

    def channel_topics_detail(self, stream_id: int) -> list[dict]:
        rows = sorted(self.store.topic_rows(int(stream_id)), key=lambda r: r.max_id, reverse=True)
        return [{"name": r.name, "max_id": r.max_id} for r in rows]

    def channel_topics(self, stream_id: int) -> list[str]:
        """Topic names, newest first, resolved ones included — as Zulip lists them."""
        return [row["name"] for row in self.channel_topics_detail(stream_id)]

    def channel_subscribers(self, stream_id: int) -> list[int]:
        if self.fixture:
            return []
        return self.live().channel_subscribers(stream_id)

    # -- one topic -------------------------------------------------------------

    def _answerable(self, channel: str, topic: str):
        """`(stream_id, True)` when the store can answer for this topic name,
        `(None, False)` when only the live side can."""
        found = self._channel(channel)
        if found is None:
            return None, False
        if not found.archived:
            return found.stream_id, True
        coverage = self.store.coverage(found.stream_id, topic)
        return found.stream_id, bool(coverage and coverage.complete)

    def _rows(self, channel: str, topic: str, *, since_id: int = 0):
        stream_id, ok = self._answerable(channel, topic)
        if not ok:
            return None
        return [m.as_zulip() for m in self.store.messages(stream_id, topic, since_id=since_id)]

    def topic_history(self, channel: str, topic: str, num_before: int = 50, **_) -> list[dict]:
        rows = self._rows(channel, topic)
        if rows is None:
            return self._fallback("topic_history", channel, topic, num_before=num_before)
        return rows[-int(num_before):] if num_before else []

    def topic_since(self, channel: str, topic: str, after_id: int, num_after: int = 100) -> list[dict]:
        rows = self._rows(channel, topic, since_id=int(after_id))
        if rows is None:
            return self._fallback("topic_since", channel, topic, after_id, num_after=num_after)
        return rows[:int(num_after)]

    def topic_beginning(self, channel: str, topic: str, num_after: int = 5) -> list[dict]:
        rows = self._rows(channel, topic)
        if rows is None:
            return self._fallback("topic_beginning", channel, topic, num_after=num_after)
        return rows[:int(num_after)]

    def topic_last_id(self, channel: str, topic: str) -> int:
        rows = self._rows(channel, topic)
        if rows is None:
            return self._fallback("topic_last_id", channel, topic)
        return int(rows[-1]["id"]) if rows else 0

    def _fallback(self, method: str, channel: str, topic: str, *args, **kwargs):
        if self.fixture:
            # A fixture holds its whole realm: what it does not hold is empty.
            return 0 if method == "topic_last_id" else []
        return getattr(self.live(), method)(channel, topic, *args, **kwargs)

    # -- messages and notes ------------------------------------------------------

    def message(self, message_id: int, *, strict: bool = False) -> dict | None:
        found = self.store.message(int(message_id))
        if found is not None:
            return found.as_zulip()
        if self.fixture:
            return None
        return self.live().message(int(message_id), strict=strict)

    def roster_owner(self, channel: str, topic: str) -> str:
        """The agent whose published roster serves this topic, read off the
        store's `#agents` board (as `agag.trace.MirrorReader` does); "" when
        none or more than one does."""
        from agag.intro import AGENTS_CHANNEL, INTRO_TOPIC_PREFIX, parse_roster, roster_owner

        rosters = []
        found = self._channel(AGENTS_CHANNEL)
        if found is not None:
            for name in self.channel_topics(found.stream_id):
                if not name.startswith(INTRO_TOPIC_PREFIX):
                    continue
                history = self.topic_history(AGENTS_CHANNEL, name, 1)
                roster = parse_roster(str(history[-1].get("content") or "")) if history else None
                if roster is not None:
                    rosters.append(roster)
        return roster_owner(rosters, channel, topic)

    def own_notes(self, tag: str, num_before: int = 1000) -> list[dict]:
        """This account's own `[selfnote][<tag>]` messages, oldest first."""
        me = int(self.whoami()["user_id"])
        found: list[dict] = []
        seen: set[int] = set()
        for row in self.store.notes(tag=tag, sender_id=me):
            if row.message_id not in seen:
                seen.add(row.message_id)
                message = self.store.message(row.message_id)
                if message is not None:
                    found.append(message.as_zulip())
        return found[-int(num_before):]

    def own_rootchat_notes(self, num_before: int = 1000) -> list[dict]:
        return self.own_notes("rootchat", num_before)

    def own_moved_notes(self, num_before: int = 1000) -> list[dict]:
        return self.own_notes("rootchat-moved", num_before)

    def own_served_notes(self, num_before: int = 1000) -> list[dict]:
        return self.own_notes("served", num_before)

    def public_notes(self, tag: str, num_before: int = 1000) -> list[dict]:
        found: list[dict] = []
        seen: set[int] = set()
        for row in self.store.notes(tag=tag):
            if row.message_id in seen:
                continue
            seen.add(row.message_id)
            message = self.store.message(row.message_id)
            if message is not None:
                found.append(message.as_zulip())
        return found[-int(num_before):]

