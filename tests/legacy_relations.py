"""The legacy relation records a realm export needs since failsafe p6 ex1.

Replay fixtures are realm exports from before root notes stated their
relation. On the live realm each author wrote one legacy record classifying
its old notes from their conversations' beginnings (`python -m
agag.relations legacy`); this builds the same records for a fixture, so a
replay reads it as the migrated realm does."""

from __future__ import annotations

from agag.relations import beginning_relation, legacy_note
from agag.selfnote import parse_rootchat, rootchat_relation


def migrated(messages: list[dict]) -> list[dict]:
    by_conversation: dict[tuple[str, str], list[dict]] = {}
    for m in messages:
        topic = str(m.get("topic") or "")
        bare = topic[2:] if topic.startswith("✔ ") else topic
        by_conversation.setdefault((m.get("channel"), bare), []).append(m)
    per_author: dict[int, dict] = {}
    for (channel, topic), rows in by_conversation.items():
        rows.sort(key=lambda m: int(m["id"]))
        for m in rows:
            if parse_rootchat(m.get("content")) is None or rootchat_relation(m.get("content")) is not None:
                continue
            author = int(m["sender_id"])
            kind = beginning_relation(rows[:5], author, rows)
            found = per_author.setdefault(author, {"upto": 0, "reference": [], "name": m.get("sender_full_name")})
            found["upto"] = max(found["upto"], int(m["id"]))
            if kind == "reference":
                found["reference"].append(int(m["id"]))
    lowest = min((int(m["id"]) for m in messages), default=1)
    records = []
    for index, (author, found) in enumerate(sorted(per_author.items())):
        records.append({"id": lowest - 10 + index, "channel": "relations-fixture",
                        "topic": "relations", "sender_id": author, "sender_full_name": found["name"],
                        "sender_realm_str": "", "timestamp": 1,
                        "content": legacy_note(found["upto"], found["reference"], (), "fixture migration")})
    return records + list(messages)
