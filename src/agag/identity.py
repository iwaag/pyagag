"""Which conversation a record means, decided by message ids.

`robust_workflow` p2 step 2. A topic name is how a conversation is shown: a
✔ renames it, a retirement frees it for a replacement, anybody can reuse it.
A message id is what it is — a message moves with every rename of its
topic and nobody else can take its id. The realm's notes already carry ids
(a served mark says *up to which post*, a root note may say *which post it
was written for*), but until now the readers joined them on the name written
beside the id, so a rename orphaned the record and a reused name captured
it: a finished callback was served again after its topic was renamed, and a
new request under an old name inherited the old one's marks.

The rule here is the one `agag.zulip.locate` already follows for a single
lookup, made available to every reader that holds a lookup — a mirror (free)
or a client (one call): **where the post is now decides; the name written
beside it is the fallback only when the post cannot be found.** A lookup is
any object with `message(id)` returning a Zulip dict or a mirror `Message`,
and `None` (or `False`) when it cannot say.
"""

from __future__ import annotations

from .selfnote import Conversation
from .zulip import RESOLVED_TOPIC_PREFIX, channel_name

__all__ = ["bare", "key", "served_key", "whereabouts"]


def bare(topic: str) -> str:
    """A topic name without the ✔ a resolve adds."""
    topic = str(topic or "")
    return topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic


def key(channel: str, topic: str) -> tuple[str, str]:
    """`(channel, bare topic)`: the realm's join key for "this conversation
    as it stands", ✔ or not."""
    return (str(channel), bare(topic))


def whereabouts(lookup, message_id: int) -> tuple[str, str] | None:
    """`(channel, live topic)` of a message now, or None when the lookup does
    not hold it (deleted, never seen, or the lookup got no answer)."""
    if lookup is None or not message_id:
        return None
    try:
        found = lookup.message(int(message_id))
    except Exception:  # noqa: BLE001 - a lookup that failed says nothing
        return None
    if not found:
        return None
    if isinstance(found, dict):
        channel, topic = channel_name(found), found.get("subject")
    else:
        if getattr(found, "deleted", False):
            return None
        channel, topic = getattr(found, "channel", ""), getattr(found, "topic", "")
    if not channel or not isinstance(topic, str) or not topic:
        return None
    return (str(channel), topic)


def served_key(lookup, remote: Conversation, up_to: int) -> tuple[str, str]:
    """The conversation a `[served] <remote> <up_to>` mark covers, as a join
    key: where post `up_to` is now; the name written in the mark only when
    that post cannot be found. The post is the one the mark was written for
    — in the remote conversation by construction — so it follows every
    rename that conversation goes through."""
    where = whereabouts(lookup, up_to)
    if where is not None:
        return key(*where)
    return key(remote.channel, remote.topic)
