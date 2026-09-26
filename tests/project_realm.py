"""A small in-memory realm for `agproject` / `agroutine` tests: channels with
members and folders, topics with messages, users, the `#agents` board —
shared by every client made from it, each speaking as its own user."""

from __future__ import annotations

from agag.zulip import RESOLVED_TOPIC_PREFIX

DEV, FRONT, AUTOLAB, ARCHSAGE, READER, PROVISIONER = 8, 15, 11, 24, 22, 17


def roster(instance, bot, bot_id, prefixes):
    return (f"# {instance}\n\n```agag-roster\nschema: ag.agent-roster.v1\ninstance: {instance}\nagent: {instance}\n"
            f"bot: {bot}\nbot_id: {bot_id}\nchannel: {instance}\nprefixes: {', '.join(prefixes)}\n```\n")


class Realm:
    def __init__(self):
        self.users = [
            {"user_id": DEV, "full_name": "Developer", "role": 100, "is_bot": False, "is_active": True},
            {"user_id": FRONT, "full_name": "Front", "role": 400, "is_bot": True, "is_active": True},
            {"user_id": AUTOLAB, "full_name": "autolab-agstudio1", "role": 400, "is_bot": True, "is_active": True},
            {"user_id": ARCHSAGE, "full_name": "archsage", "role": 400, "is_bot": True, "is_active": True},
            {"user_id": READER, "full_name": "Opsroom Observer", "role": 400, "is_bot": True, "is_active": True},
            {"user_id": PROVISIONER, "full_name": "Provisioner", "role": 200, "is_bot": False, "is_active": True},
        ]
        self.channels: dict[str, dict] = {}
        self.folders: dict[str, int] = {"routine": 13}
        self.messages: list[dict] = []
        self.next_id = 1000
        self.fail_after: dict[str, int] = {}
        self.truncate_over: int | None = None
        self.add_channel("agents", [])
        self.post(AUTOLAB, "agents", "intro-autolab-agstudio1",
                  roster("autolab-agstudio1", "autolab-agstudio1", AUTOLAB, ["workrun-", "workplan-", "bmining-"]))
        self.post(FRONT, "agents", "intro-front-agstudio1",
                  roster("front-agstudio1", "Front", FRONT, ["front-", "routinerun-", "argue-"]))

    def add_channel(self, name, subscribers, *, description="", folder_id=None, archived=False):
        self.channels[name] = {"name": name, "stream_id": 100 + len(self.channels), "description": description,
                               "folder_id": folder_id, "subscribers": set(subscribers), "is_archived": archived}
        return self.channels[name]

    def post(self, sender, channel, topic, content):
        self.next_id += 1
        name = next(u["full_name"] for u in self.users if u["user_id"] == sender)
        self.messages.append({"id": self.next_id, "sender_id": sender, "sender_full_name": name,
                              "display_recipient": channel, "subject": topic, "content": content})
        if self.truncate_over and len(content) > self.truncate_over:
            self.messages[-1]["content"] = content[: self.truncate_over] + "\n[message truncated]"
        return self.next_id

    def resolve(self, channel, topic):
        for message in self.messages:
            if message["display_recipient"] == channel and message["subject"] == topic:
                message["subject"] = RESOLVED_TOPIC_PREFIX + topic

    def client(self, user_id):
        return Client(self, user_id)

    def topic(self, channel, topic):
        return [m for m in self.messages if m["display_recipient"] == channel and m["subject"] == topic]


class Client:
    base_url = "https://zulip.example"

    def __init__(self, realm: Realm, user_id: int):
        self.realm, self.user_id = realm, user_id
        self.calls: list[tuple] = []

    def _tick(self, what):
        left = self.realm.fail_after.get(what)
        if left is not None:
            if left <= 0:
                raise OSError(f"injected failure at {what}")
            self.realm.fail_after[what] = left - 1

    def whoami(self, refresh=False):
        user = next(u for u in self.realm.users if u["user_id"] == self.user_id)
        return {"user_id": self.user_id, "full_name": user["full_name"]}

    def users(self):
        return list(self.realm.users)

    def realm_owners(self):
        return [u["user_id"] for u in self.realm.users if u["role"] == 100 and not u["is_bot"]]

    def channels(self, include_archived=False):
        rows = [dict(c, subscribers=None) for c in self.realm.channels.values()]
        return [r for r in rows if include_archived or not r["is_archived"]]

    def stream_id(self, name):
        return self.realm.channels[name]["stream_id"]

    def _by_id(self, stream_id):
        return next(c for c in self.realm.channels.values() if c["stream_id"] == stream_id)

    def channel_subscribers(self, stream_id):
        return sorted(self._by_id(stream_id)["subscribers"])

    def channel_topics(self, stream_id):
        name = self._by_id(stream_id)["name"]
        return list(dict.fromkeys(m["subject"] for m in reversed(self.realm.messages) if m["display_recipient"] == name))

    def topic_history(self, channel, topic, num_before=50):
        return [dict(m) for m in self.realm.topic(channel, topic)][-num_before:]

    def topic_last_id(self, channel, topic):
        history = self.topic_history(channel, topic)
        return history[-1]["id"] if history else 0

    def message(self, message_id, strict=False):
        return next((dict(m) for m in self.realm.messages if m["id"] == message_id), None)

    def send_to_channel(self, channel, topic, content):
        self._tick("send")
        if channel not in self.realm.channels:
            raise OSError(f"no channel {channel}")
        self.realm.channels[channel]["subscribers"].add(self.user_id)
        self.calls.append(("send", channel, topic))
        return self.realm.post(self.user_id, channel, topic, content)

    def ensure_subscribed(self, channel):
        members = self.realm.channels[channel]["subscribers"]
        changed = self.user_id not in members
        members.add(self.user_id)
        return changed

    def channel_folder_by_name(self, name):
        return {"id": self.realm.folders[name], "name": name} if name in self.realm.folders else None

    def create_channel_folder(self, name, description=""):
        self.realm.folders[name] = 50 + len(self.realm.folders)
        self.calls.append(("folder", name))
        return self.realm.folders[name]

    def create_channel(self, name, description, principals, announce=False, folder_id=None):
        self._tick("create")
        self.calls.append(("create", name, tuple(principals), folder_id))
        self.realm.add_channel(name, principals, description=description, folder_id=folder_id)
        return {"result": "success"}

    def subscribe_channels(self, names, principals=None):
        for name in names:
            self.realm.channels[name]["subscribers"].update(principals or [self.user_id])
        self.calls.append(("subscribe", tuple(names), tuple(principals or ())))
        return {}

    def set_channel_folder(self, stream_id, folder_id):
        self._by_id(stream_id)["folder_id"] = folder_id
        return {}

    def update_channel_description(self, stream_id, description):
        self._by_id(stream_id)["description"] = description
        return {}
