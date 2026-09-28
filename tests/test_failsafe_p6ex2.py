"""failsafe p6 ex2: the Developer's full proxy is recognized by every check.

p6 ex1's trial L3: Front refused the Omni Agent's confirmation it had taken
twice before, and the credential was switched to the Developer's to get past
it. The relationship is now one statement (`agag.people`, a host
`people.toml`) read by the checks that compare a speaker with a decision
holder: mission acceptance (reserved or not), hold release, a question put
to the Developer — while records keep naming who actually spoke, and an
ordinary agent (the worker, or any other) still decides nothing it does not
hold. Without the statement every check is plain id equality, as before.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from agag import holds as holding
from agag import people
from agag import trace as tracing
from agag.acceptance import AcceptanceRefused, accept_mission, reservation_note
from agag.dispositions import record as record_disposition
from agag.outstanding import ANSWERED, PENDING, WITHDRAWN, read_requests
from agag.post import PostMeta, compose
from agag.topics import format_chatlog

from study_realm import ARCHSAGE, AUTOLAB, DEV, FRONT, OMNI, Realm
from test_failsafe_p6 import Mission

PEOPLE = """\
[[proxy]]
user = 9
name = "Omni Agent"
for = 8
for_name = "Developer"
"""


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    path = tmp_path / "people.toml"
    path.write_text(PEOPLE, encoding="utf-8")
    monkeypatch.setenv(people.CONFIG_VARIABLE, str(path))
    return path


def closed(*, reserve: bool = False) -> tuple[Realm, Mission]:
    """The Developer's own request, delegated by Front, its task shown,
    agreed by Front and closed by autolab."""
    realm = Realm()
    m = Mission(realm, asker=DEV)
    if reserve:
        realm.post("front", m.desk, reservation_note(DEV, "Developer", m.origin), FRONT)
    m.front_agrees()
    m.autolab_closes(bound=True)
    return realm, m


def acceptance_record(realm) -> str:
    return next(r["content"] for r in realm.rows if r["content"].startswith("[selfnote][acceptance]"))


# --- the statement ----------------------------------------------------------------------------


def test_the_statement_names_the_proxy_both_ways_and_nothing_else(proxy):
    assert people.acts_for(OMNI, DEV) and people.acts_for(DEV, OMNI) and people.acts_for(DEV, DEV)
    assert not people.acts_for(FRONT, DEV) and not people.acts_for(OMNI, FRONT) and not people.acts_for(0, DEV)
    assert people.for_suffix(OMNI, DEV, "Developer") == " for 8 (Developer)"
    assert people.for_suffix(DEV, DEV, "Developer") == "" and people.for_suffix(FRONT, DEV) == ""


def test_without_the_statement_every_check_is_plain_identity():
    assert people.proxies() == {} and not people.acts_for(OMNI, DEV)


def test_the_chatlog_says_whose_authority_the_proxy_speaks_with(proxy):
    rows = [{"id": 1, "sender_id": OMNI, "sender_full_name": "Omni Agent", "content": "Go ahead."},
            {"id": 2, "sender_id": DEV, "sender_full_name": "Developer", "content": "Yes."}]
    log = format_chatlog(rows, FRONT)
    assert "[Omni Agent — with Developer's full authority] Go ahead." in log
    assert "[Developer] Yes." in log, "the principal and the reply routing keep their own names"


# --- acceptance of a Developer-owned decision ---------------------------------------------------


def test_the_proxy_accepts_the_developer_s_mission_and_the_record_names_who_spoke(proxy):
    realm, m = closed()
    words = realm.post("front", m.desk, "Accepted — the result is what I asked for.", OMNI)
    done = accept_mission(realm.speaking_as(FRONT), m.mission, evidence=words, resolve=False)
    assert (done.by_id, done.by_name, done.on_behalf) == (OMNI, "Omni Agent", " for 8 (Developer)")
    note = acceptance_record(realm)
    assert f"#{words} by 9 (Omni Agent) for 8 (Developer) after=#" in note
    result = tracing.trace(realm, m.origin, now=realm.clock + 60)
    assert node_state(result, m.plan_topic) == "done"


def node_state(result, topic_part):
    return next(n for n in result.nodes() if topic_part in n.topic).state


def test_without_the_statement_the_proxy_does_not_hold_the_developer_s_decision():
    realm, m = closed()
    words = realm.post("front", m.desk, "Accepted.", OMNI)
    with pytest.raises(AcceptanceRefused, match="does not hold"):
        accept_mission(realm.speaking_as(FRONT), m.mission, evidence=words)


def test_a_reserved_approval_is_the_developer_s_and_so_the_proxy_s_but_no_agent_s(proxy):
    realm, m = closed(reserve=True)
    front = realm.post(m.channel, m.task_topic, "Front agrees the mission is complete.", FRONT)
    with pytest.raises(AcceptanceRefused, match="reserved"):
        accept_mission(realm.speaking_as(FRONT), m.mission, evidence=front)
    other = realm.post("front", m.desk, "It is complete.", ARCHSAGE)
    with pytest.raises(AcceptanceRefused, match="reserved"):
        accept_mission(realm.speaking_as(FRONT), m.mission, evidence=other)
    words = realm.post("front", m.desk, "I accept it.", OMNI)
    done = accept_mission(realm.speaking_as(FRONT), m.mission, evidence=words, resolve=False)
    assert done.by_id == OMNI and "for 8 (Developer)" in acceptance_record(realm)


def test_the_worker_never_accepts_its_own_work_even_with_a_proxy_on_record(proxy):
    realm, m = closed(reserve=True)
    with pytest.raises(AcceptanceRefused, match="own agent"):
        accept_mission(realm.speaking_as(FRONT), m.mission, evidence=m.closeout)


def test_the_proxy_may_record_in_person_and_an_ordinary_agent_may_not(proxy):
    realm, m = closed(reserve=True)
    with pytest.raises(AcceptanceRefused, match="--evidence"):
        accept_mission(realm.speaking_as(FRONT), m.mission, evidence=None)
    done = accept_mission(realm.speaking_as(OMNI), m.mission, evidence=None, resolve=False)
    assert (done.evidence, done.by_id, done.on_behalf) == (0, OMNI, " for 8 (Developer)")


# --- holds and their release ------------------------------------------------------------------


def _hold(realm, m, purpose, unit, evidence, why="theirs to decide"):
    args = SimpleNamespace(message_id=m.origin, purpose=purpose, unit=unit, evidence=evidence, why=why.split(),
                           json=False)
    return holding.holds_command(realm, args, io.StringIO())


def test_the_proxy_releases_the_developer_s_hold_and_the_record_names_both(proxy):
    realm, m = closed()
    ask = realm.post("front", m.desk, "Keep this with me until I say otherwise.", DEV)
    assert _hold(realm, m, "indefinite", m.origin, ask) == 0
    (hold,) = holding.holds_of(tracing.trace(realm, m.origin, now=realm.clock + 60))
    other = realm.post("front", m.desk, "Let it go.", ARCHSAGE)
    with pytest.raises(holding.HoldRefused, match="full authority"):
        holding.release(realm, hold.id, other)
    words = realm.post("front", m.desk, "You can let it go now.", OMNI)
    written, _ = holding.release(realm, hold.id, words, "done with it")
    assert written
    note = next(r["content"] for r in realm.rows if r["id"] == written)
    assert note.startswith(f"[selfnote][hold-release] #{hold.id} by 9 (Omni Agent) for 8 (Developer) #{words}")
    (hold,) = holding.holds_of(tracing.trace(realm, m.origin, now=realm.clock + 60))
    assert hold.state == "released" and "released by Omni Agent for Developer" in hold.ended_by["what"]
    assert holding.release(realm, hold.id, words)[0] == 0, "a repeat writes nothing"


def test_the_developer_releases_a_hold_the_proxy_placed_and_an_operator_in_person_may_too(proxy):
    realm, m = closed()
    ask = realm.post("front", m.desk, "Hold the decision for me.", OMNI)
    _hold(realm, m, "decision", m.origin, ask)
    (hold,) = holding.holds_of(tracing.trace(realm, m.origin, now=realm.clock + 60))
    assert hold.by == OMNI
    written, _ = holding.release(realm, hold.id, 0, "from the terminal", in_person=(DEV, "Developer"))
    note = next(r["content"] for r in realm.rows if r["id"] == written)
    assert " by 8 (Developer) for 9 (Omni Agent) #0" in note
    realm2, m2 = closed()
    ask2 = realm2.post("front", m2.desk, "Hold it.", DEV)
    _hold(realm2, m2, "decision", m2.origin, ask2)
    (hold2,) = holding.holds_of(tracing.trace(realm2, m2.origin, now=realm2.clock + 60))
    with pytest.raises(holding.HoldRefused, match="only they"):
        holding.release(realm2, hold2.id, 0, in_person=FRONT)
    assert holding.release(realm2, hold2.id, 0, in_person=OMNI)[0]


def test_the_proxy_cancels_the_developer_s_request_on_its_own_words(proxy):
    realm, m = closed()
    words = realm.post("front", m.desk, "Cancel this request; the trial is over.", OMNI)
    written, same, _, _ = record_disposition(realm.speaking_as(FRONT), m.origin, "cancelled", evidence=words,
                                             why="the proxy's decision")
    assert written and same is None
    note = next(r["content"] for r in realm.rows if r["id"] == written)
    assert f" by 9 (Omni Agent) #{words}" in note, "the actual speaker, never the Developer as if they spoke"


# --- a question put to the Developer ------------------------------------------------------------


def _post(mid, sender, name, text, meta=None):
    return {"id": mid, "sender_id": sender, "sender_full_name": name, "content": compose(text, meta)}


def _ask(mid, to=DEV):
    return _post(mid, FRONT, "Front", "Shall I send it to autolab?",
                 PostMeta(intent="response_request", to=to, ask="question"))


def test_the_proxy_answers_a_question_put_to_the_developer(proxy):
    rows = [_post(1, DEV, "Developer", "Build it."), _ask(2), _post(3, OMNI, "Omni Agent", "Yes, send it.")]
    request = read_requests(rows).by_id()[2]
    assert (request.state, request.settled_by) == (ANSWERED, 3)
    other = [_post(1, DEV, "Developer", "Build it."), _ask(2), _post(3, ARCHSAGE, "archsage", "Yes.")]
    assert read_requests(other).by_id()[2].state == PENDING, "an ordinary agent answers nothing for the Developer"


def test_without_the_statement_the_question_stays_pending():
    rows = [_post(1, DEV, "Developer", "Build it."), _ask(2), _post(3, OMNI, "Omni Agent", "Yes, send it.")]
    assert read_requests(rows).by_id()[2].state == PENDING


def test_the_proxy_withdraws_what_the_developer_asked(proxy):
    ask = _post(1, DEV, "Developer", "Which one, Front?", PostMeta(intent="response_request", to=FRONT,
                                                                    ask="question"))
    rows = [ask, _post(2, OMNI, "Omni Agent", "Never mind that question.", PostMeta(intent="report", re=(1,)))]
    assert read_requests(rows).by_id()[1].state == WITHDRAWN
