"""A small synthetic realm shaped like a study request (failsafe p5).

Built in code, not exported: the posts carry the shapes the readers depend
on (root notes, acks, `ag-post` lines with `end=`, mission/task/state
notes, finish blocks, ✔ notices) and nothing of any real conversation.
progress_panel p1's trial B is the model: a Front Desk request, a routine
run Front opened from it, autolab's workplan and task below that, and
archsage's refresh topic.

`Realm` is a read-only client stand-in (the calls `agag.trace`,
`agag.progress` and `agag.acceptance` make) over a list of rows; `cut(id)`
is the realm as a reader found it right after that message.
"""

from __future__ import annotations

from agag.zulip import RESOLVED_TOPIC_PREFIX

DEV, OMNI, AUTOLAB, FRONT, OBSERVER, ARCHSAGE = 8, 9, 11, 15, 23, 24
NAMES = {DEV: "Developer", OMNI: "Omni Agent", AUTOLAB: "autolab-agstudio1", FRONT: "Front",
         OBSERVER: "agobserver-agstudio1", ARCHSAGE: "archsage", 0: "Notification Bot"}
ACK = "Message received. Please wait for the reply."
T0 = 1_790_500_000


class Realm:
    def __init__(self, rows: list[dict] | None = None, *, me: int = FRONT):
        self.rows = rows if rows is not None else []
        self.next_id = 1000
        self.clock = T0
        self.me = me
        self.written: list[dict] = []

    # -- building ------------------------------------------------------------------------

    def post(self, channel: str, topic: str, content: str, sender: int, *, at: int | None = None,
             realm: str = "") -> int:
        self.clock = at if at is not None else self.clock + 5
        self.next_id += 1
        self.rows.append({"id": self.next_id, "channel": channel, "topic": topic, "sender_id": sender,
                          "sender_full_name": NAMES.get(sender, str(sender)), "sender_realm_str": realm,
                          "timestamp": self.clock, "content": content})
        return self.next_id

    def resolve(self, channel: str, topic: str, by: int) -> int:
        return self.post(channel, topic, f"@_**{NAMES[by]}|{by}** has marked this topic as resolved.", 0,
                         realm="zulipinternal")

    def at(self, message_id: int) -> int:
        return next(r["timestamp"] for r in self.rows if r["id"] == message_id)

    def cut(self, upto: int) -> "Realm":
        view = Realm([r for r in self.rows if r["id"] <= upto], me=self.me)
        view.next_id, view.clock = self.next_id, self.clock
        return view

    def speaking_as(self, me: int) -> "Realm":
        view = Realm(self.rows, me=me)
        view.next_id, view.clock = self.next_id, self.clock
        return view

    # -- the client calls ---------------------------------------------------------------

    def _live(self, channel, topic):
        resolved = False
        for r in self.rows:
            if r["channel"] == channel and r["topic"] == topic and r["sender_realm_str"]:
                resolved = "marked this topic as resolved" in r["content"]
        return f"{RESOLVED_TOPIC_PREFIX}{topic}" if resolved else topic

    def _shape(self, r):
        return {"id": r["id"], "type": "stream", "display_recipient": r["channel"],
                "subject": self._live(r["channel"], r["topic"]), "sender_id": r["sender_id"],
                "sender_full_name": r["sender_full_name"], "sender_realm_str": r["sender_realm_str"],
                "timestamp": r["timestamp"], "content": r["content"]}

    def whoami(self, refresh=False):
        return {"user_id": self.me, "full_name": NAMES[self.me], "is_bot": self.me not in (DEV,)}

    def message(self, message_id, strict=False):
        return next((self._shape(r) for r in self.rows if r["id"] == int(message_id)), None)

    def topic_history(self, channel, topic, num_before=50):
        bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
        if self._live(channel, bare) != topic:
            return []
        return [self._shape(r) for r in self.rows if r["channel"] == channel and r["topic"] == bare][-num_before:]

    def public_notes(self, tag, num_before=1000):
        marker = f"[selfnote][{tag}]"
        return [self._shape(r) for r in self.rows if r["content"].startswith(marker)][-num_before:]

    def send_to_channel(self, channel, topic, content):
        bare = topic[len(RESOLVED_TOPIC_PREFIX):] if topic.startswith(RESOLVED_TOPIC_PREFIX) else topic
        ident = self.post(channel, bare, content, self.me)
        self.written.append({"id": ident, "channel": channel, "topic": bare, "content": content})
        return ident

    def resolve_topic(self, message_id, topic):
        row = next(r for r in self.rows if r["id"] == int(message_id))
        if not self._live(row["channel"], row["topic"]).startswith(RESOLVED_TOPIC_PREFIX):
            self.resolve(row["channel"], row["topic"], self.me)


def line(intent: str, **fields) -> str:
    extra = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"`ag-post intent={intent}{(' ' + extra) if extra else ''}`"


class Study:
    """One study request through Front, the way trial B's went.

    `desk` → (Front's desk serving) → `run` → (the run's serving) → the
    workplan in `pj-<project>` → mission → task 1 in `work-m<mission>`.
    Each step is a method; ids are kept as attributes."""

    def __init__(self, realm: Realm, project: str, stamp: str, *, beside: bool = False):
        self.realm, self.project, self.stamp = realm, project, stamp
        self.desk_topic = f"front-desk-{stamp}-{project}"
        self.run_channel, self.run_topic = f"routine-study-{project}", f"routinerun-{stamp}"
        self.plan_channel, self.plan_topic = f"pj-{project}", f"workplan-{project}-{stamp}"
        self.beside = beside  # the workplan is opened by the desk, beside the run (trial B worldtrend)

    def ask(self, text: str = "Please run the study routine once, small.") -> int:
        self.origin = self.realm.post("front", self.desk_topic, text, OMNI)
        self.desk_ack = self.realm.post("front", self.desk_topic, ACK, FRONT)
        return self.origin

    def open_run(self) -> int:
        r = self.realm
        home = f"front/{self.desk_topic} #{self.origin}"
        if self.beside:
            r.post(self.plan_channel, self.plan_topic, f"[selfnote][rootchat] {home}", FRONT)
            self.request = r.post(self.plan_channel, self.plan_topic,
                                  "@**autolab-agstudio1** one bounded mission, please.\n\n"
                                  + line("response_request", to=AUTOLAB, ask="confirmation"), FRONT)
        r.post(self.run_channel, self.run_topic, f"[selfnote][rootchat] {home}", FRONT)
        self.run_open = r.post(self.run_channel, self.run_topic, "Run request: the study routine, once.", FRONT)
        r.post("front", self.desk_topic, "@**Omni Agent** opened the run.\n\n" + line("report", end=self.desk_ack),
               FRONT)
        self.run_ack = r.post(self.run_channel, self.run_topic, ACK, FRONT)
        return self.run_open

    def run_delegates(self) -> int:
        r = self.realm
        r.post(self.plan_channel, self.plan_topic,
               f"[selfnote][rootchat] {self.run_channel}/{self.run_topic} #{self.run_open}", FRONT)
        self.request = r.post(self.plan_channel, self.plan_topic,
                              "@**autolab-agstudio1** one bounded mission, please.\n\n"
                              + line("response_request", to=AUTOLAB, ask="confirmation"), FRONT)
        self.run_waits()
        return self.request

    def run_waits(self, text: str = "Asked autolab for the mission; waiting for its plan.") -> int:
        self.run_entry = self.realm.post(self.run_channel, self.run_topic,
                                         f"{text}\n\n" + line("progress", end=self.run_ack), FRONT)
        return self.run_entry

    def autolab_plans(self) -> int:
        r = self.realm
        self.plan_ack = r.post(self.plan_channel, self.plan_topic, ACK, AUTOLAB)
        self.mission = r.post(self.plan_channel, self.plan_topic, f"[selfnote][mission] {self.project}", AUTOLAB)
        doc = r.post(self.plan_channel, self.plan_topic, "# Plan: one task", AUTOLAB)
        r.post(self.plan_channel, self.plan_topic, f"[selfnote][doc] {doc}", AUTOLAB)
        self.task_channel, self.task_topic = f"work-m{self.mission}", f"workrun-task1-m{self.mission}"
        r.post(self.task_channel, self.task_topic, f"[selfnote][task] {self.mission}#1", AUTOLAB)
        r.post(self.task_channel, self.task_topic,
               f"[selfnote][rootchat] {self.plan_channel}/{self.plan_topic} #{self.mission}", AUTOLAB)
        tdoc = r.post(self.task_channel, self.task_topic, "# Task 1", AUTOLAB)
        r.post(self.task_channel, self.task_topic, f"[selfnote][doc] {tdoc}", AUTOLAB)
        self.plan_shown = r.post(self.plan_channel, self.plan_topic, "@**Front** the plan is ready.\n\n" + line(
            "response_request", to=FRONT, ask="confirmation", end=self.plan_ack), AUTOLAB)
        return self.mission

    def front_starts(self) -> int:
        """Front's "please start task 1" in the workplan (#13702's shape)."""
        self.start_request = self.realm.post(self.plan_channel, self.plan_topic,
                                             "Confirmed. Please start task 1.\n\n" + line("progress",
                                                                                         re=self.plan_shown), FRONT)
        return self.start_request

    def autolab_starts(self) -> int:
        r = self.realm
        self.start_ack = r.post(self.plan_channel, self.plan_topic, ACK, AUTOLAB)
        r.post(self.plan_channel, self.plan_topic, "[selfnote][state] started", AUTOLAB)
        r.post(self.task_channel, self.task_topic, "Task 1 starts now.\n\n" + line("progress"), AUTOLAB)
        r.post(self.task_channel, self.task_topic, f"[selfnote][start] #{self.start_request} for {FRONT} Front",
               AUTOLAB)
        r.post(self.plan_channel, self.plan_topic, "@**Front** task 1 starts now.\n\n"
               + line("report", end=self.start_ack), AUTOLAB)
        self.task_ack = r.post(self.task_channel, self.task_topic, ACK, AUTOLAB)
        return self.task_ack

    def task_works(self) -> int:
        return self.realm.post(self.task_channel, self.task_topic, "🔧 Bash: git status\n\n" + line("progress"),
                               AUTOLAB)

    def task_shows(self) -> int:
        r = self.realm
        r.post(self.task_channel, self.task_topic, "[selfnote][change] checkpoint main=abc123:def456", AUTOLAB)
        self.shown = r.post(self.task_channel, self.task_topic, "@**Front** Done: the report is committed.\n\n"
                            + line("response_request", to=FRONT, ask="confirmation", end=self.task_ack), AUTOLAB)
        return self.shown

    def task_closes(self, agreement_from: int = FRONT) -> int:
        r = self.realm
        home = self.desk_topic if self.beside else self.run_topic
        where = "front" if self.beside else self.run_channel
        anchor = self.origin if self.beside else self.run_open
        r.post(self.task_channel, self.task_topic, f"[selfnote][rootchat] {where}/{home} #{anchor}", FRONT)
        self.agreed = r.post(self.task_channel, self.task_topic, "I agree the result is complete.\n\n"
                             + line("report"), agreement_from)
        close_ack = r.post(self.task_channel, self.task_topic, ACK, AUTOLAB)
        r.post(self.task_channel, self.task_topic,
               f"[selfnote][change] accepted main=abc123 #{self.agreed} +shown={self.shown}", AUTOLAB)
        r.post(self.task_channel, self.task_topic, "[selfnote][state] completed", AUTOLAB)
        self.closed = r.post(self.task_channel, self.task_topic,
                             "@**Front** closed; integrated on main.\n\n" + line("report", end=close_ack), AUTOLAB)
        r.resolve(self.task_channel, self.task_topic, AUTOLAB)
        return self.closed
