"""The skeleton: one spec drives instance name, role runs and the listener."""

import json
from pathlib import Path

from agag import agent, entrance
from agag.agent_config import ResolvedAgent
from agag.harness import HarnessResult
from agag.topics import TopicContext

CONFIG = '''schema = "ag.agent-config.v2"
[models."ollama/test"]
[profiles.stub]
harness = "fake"
model = "ollama/test"
[roles.front]
profile = "stub"
allowed_tools = "Read,Bash(agentchat:*)"
[roles.worker]
profile = "stub"
allowed_tools = ["Read", "Write"]
'''


def spec(tmp_path: Path, **kwargs) -> agent.AgentSpec:
    (tmp_path / "agents.toml").write_text(CONFIG, encoding="utf-8")
    return agent.AgentSpec("agtest", tmp_path, **kwargs)


def test_paths_and_names_follow_the_root(tmp_path):
    s = spec(tmp_path, plan_prefix="testplan-", run_prefix="testrun-")
    assert s.zulip_env == tmp_path / ".local" / "zulip.env"
    assert s.instance_toml == tmp_path / ".local" / "instance.toml"
    assert s.intro_path == tmp_path / "params" / "intro.md"
    assert s.instance_env_var == "AGTEST_INSTANCE_NAME"
    assert s.log_only_env_var == "AGTEST_ZULIP_LOG_ONLY"
    assert s.sweep_prefixes == ("testrun-", "testplan-")


def test_extra_prefixes_are_swept_too(tmp_path):
    s = spec(tmp_path, plan_prefix="testplan-", extra_prefixes=("mining-",))
    assert s.sweep_prefixes == ("testplan-", "mining-")
    matches = agent.topic_filter(s)
    assert matches("pj-x", "mining-ideas") and not matches("pj-x", "other")


def test_instance_name_reads_the_local_file_then_falls_back(tmp_path, monkeypatch):
    s = spec(tmp_path)
    monkeypatch.delenv("AGTEST_INSTANCE_NAME", raising=False)
    assert s.instance_name() == "agtest"
    s.instance_toml.parent.mkdir()
    s.instance_toml.write_text('name = "agtest-host1"\n', encoding="utf-8")
    assert s.instance_name() == "agtest-host1"
    monkeypatch.setenv("AGTEST_INSTANCE_NAME", "agtest-host2")
    assert s.instance_name() == "agtest-host2"


def test_topic_filter_admits_own_channel_and_prefixes(tmp_path, monkeypatch):
    s = spec(tmp_path, plan_prefix="testplan-", run_prefix="testrun-")
    monkeypatch.setenv("AGTEST_INSTANCE_NAME", "agtest-host1")
    matches = agent.topic_filter(s)
    assert matches("agtest-host1", "a plain question")
    assert matches("general", "testplan-a-request")
    assert matches("general", "testrun-a-request")
    assert not matches("general", "a plain question")


def test_topic_filter_without_prefixes_is_the_own_channel_only(tmp_path, monkeypatch):
    s = spec(tmp_path)
    monkeypatch.setenv("AGTEST_INSTANCE_NAME", "agtest-host1")
    matches = agent.topic_filter(s)
    assert matches("agtest-host1", "anything")
    assert not matches("general", "anything")


def test_resolve_role_carries_the_grant_and_the_chat_handover(tmp_path, monkeypatch):
    s = spec(tmp_path, extra_environment=lambda env: {"TOOL_HOME": "/tools"})
    resolved = agent.resolve_spec_role(
        s, "front", check_available=False, home=("agtest-host1", "hello")
    )
    assert resolved.allowed_tools == "Read,Bash(agentchat:*)"
    assert resolved.environment["AGENTCHAT_ZULIP_ENV"] == str(s.zulip_env)
    assert resolved.environment["AGENTCHAT_HOME"] == "agtest-host1/hello"
    assert resolved.environment["TOOL_HOME"] == "/tools"
    assert "PATH" in resolved.environment
    worker = agent.resolve_spec_role(s, "worker", check_available=False)
    assert worker.allowed_tools == "Read,Write"


def test_run_role_passes_the_grant_and_writes_its_record(tmp_path, monkeypatch):
    s = spec(tmp_path)
    calls = []
    monkeypatch.setattr(
        agent, "resolve_spec_role",
        lambda spec, role, **kw: ResolvedAgent(
            role, "stub", "fake", "ollama", "ollama/test", {}, "agent", None, {}, "Read"
        ),
    )
    monkeypatch.setattr(
        agent, "run_harness",
        lambda a, prompt, **kw: calls.append((a, prompt, kw))
        or HarnessResult("answer", 0, {"role": a.role, "outcome": "done"}),
    )
    record = tmp_path / "records" / "run-0001.json"
    output, run_record, code = agent.run_role(
        s, "front", "question", cwd=tmp_path, timeout=30, record=record,
        extra_meta={"project": "demo"},
    )
    assert (output, code) == ("answer", 0)
    assert run_record["schema"] == "ag.agent-run.v1"
    assert run_record["project"] == "demo"
    assert record.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["project"] == "demo"
    assert calls[0][2]["allowed_tools"] == "Read"
    assert calls[0][2]["cwd"] == tmp_path


def test_log_only_switch_is_per_agent_or_global(tmp_path, monkeypatch):
    s = spec(tmp_path)
    monkeypatch.delenv("AGTEST_ZULIP_LOG_ONLY", raising=False)
    monkeypatch.delenv("AGAG_ZULIP_LOG_ONLY", raising=False)
    assert not agent.log_only(s)
    monkeypatch.setenv("AGTEST_ZULIP_LOG_ONLY", "1")
    assert agent.log_only(s)
    monkeypatch.delenv("AGTEST_ZULIP_LOG_ONLY")
    monkeypatch.setenv("AGAG_ZULIP_LOG_ONLY", "1")
    assert agent.log_only(s)


# --- the entrance ----------------------------------------------------------


def test_default_guide_names_the_prefixes(tmp_path):
    s = spec(tmp_path, plan_prefix="testplan-", run_prefix="testrun-")
    text = entrance.entrance_guide(s)
    assert "`testplan-…` is a plan, `testrun-…` is its run" in text
    assert "new `testplan-…` topic" in text
    assert "{" not in text


def test_default_guide_without_prefixes_still_reads(tmp_path):
    text = entrance.entrance_guide(spec(tmp_path))
    assert "lists your conversations." in text
    assert "{" not in text


def test_own_guide_wins_over_the_default(tmp_path):
    s = spec(tmp_path)
    path = s.guides / "entrance_front" / "guide.md"
    path.parent.mkdir(parents=True)
    path.write_text("Say hello.\n", encoding="utf-8")
    assert entrance.entrance_guide(s) == "Say hello."


def context(client=None, history=None) -> TopicContext:
    return TopicContext(
        client, "agtest-host1", "hello", 7, "Test",
        history=history or [{"id": 1, "sender_id": 3, "content": "hi"}],
    )


def test_serve_entrance_writes_the_chatlog_and_posts_the_answer(tmp_path, monkeypatch):
    s = spec(tmp_path)
    seen = {}

    def run_role(spec, role, prompt, **kwargs):
        seen.update(kwargs, role=role, prompt=prompt)
        return "the answer", {}, 0

    monkeypatch.setattr(entrance, "run_role", run_role)
    result = entrance.serve_entrance(s, context())
    assert result.sections == ["the answer"]
    assert seen["role"] == "front"
    assert seen["home"] == ("agtest-host1", "hello")
    assert seen["stream"] is True
    chatlog = seen["cwd"] / "chatlog.md"
    assert "hi" in chatlog.read_text(encoding="utf-8")
    assert "Your own channel is" in seen["prompt"]


def test_serve_entrance_reports_a_failed_run(tmp_path, monkeypatch):
    s = spec(tmp_path)
    monkeypatch.setattr(entrance, "run_role", lambda *a, **k: ("boom", {}, 2))
    try:
        entrance.serve_entrance(s, context())
    except entrance.EntranceError as error:
        assert "exited 2" in str(error)
    else:
        raise AssertionError("expected EntranceError")


def test_roster_for_states_the_listener_s_own_routing(tmp_path):
    """`roster_for` must agree with `topic_filter`, or the observer routes by
    a vocabulary the listener does not use."""
    from agag.agent import AgentSpec, roster_for, topic_filter

    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "instance.toml").write_text('name = "agecho-agstudio1"\n')
    spec = AgentSpec(
        "agecho", tmp_path, plan_prefix="agechoplan-", run_prefix="agechorun-",
        extra_prefixes=("bmining-",),
    )

    class Client:
        def whoami(self):
            return {"full_name": "agecho-agstudio1", "user_id": 16}

    found = roster_for(spec, Client())
    assert found.instance == "agecho-agstudio1"
    assert found.bot_id == 16
    matches = topic_filter(spec)
    assert matches(found.channel, "anything at all")
    for prefix in found.prefixes:
        assert matches("some-other-channel", f"{prefix}thing")
    assert not matches("some-other-channel", "unrelated-topic")


# --- execution options on the spec (ag.exec-options.v1) --------------------

from agag import execopt  # noqa: E402
from agag.execopt import Option, Selection  # noqa: E402


def test_an_agent_that_lists_no_options_publishes_nothing(tmp_path):
    # Unknown, deliberately: an introduction with no block says nothing, and
    # a reader must not read that as "no".
    assert spec(tmp_path).published_options("Agtest") is None


def test_the_published_menu_always_starts_with_default(tmp_path):
    s = spec(tmp_path, exec_options=(Option("agy", "antigravity", "everything"),))
    published = s.published_options("Agtest")
    assert published.names == ("default", "agy")
    assert published.command_line() == "@**Agtest** use <option>"


def test_the_option_is_the_profile_name_unless_the_agent_says_otherwise(tmp_path):
    s = spec(tmp_path, exec_options=(Option("agy"),))
    assert s.profile_for(Selection("agy", "topic", 3), "worker") == "agy"
    assert s.profile_for(Selection(), "worker") is None
    assert s.profile_for(Selection("default"), "worker") is None


def test_an_agent_may_map_an_option_differently_per_role(tmp_path):
    s = spec(
        tmp_path,
        exec_options=(Option("agy"),),
        exec_profile=lambda option, role: None if role == "summarizer" else f"{option}-x",
    )
    assert s.profile_for(Selection("agy", "topic", 3), "worker") == "agy-x"
    assert s.profile_for(Selection("agy", "topic", 3), "summarizer") is None


def test_a_run_records_what_was_asked_for_beside_what_ran(tmp_path, monkeypatch):
    s = spec(tmp_path, exec_options=(Option("agy"),))
    asked = []
    monkeypatch.setattr(
        agent, "resolve_spec_role",
        lambda spec, role, **kw: asked.append(kw.get("profile_override"))
        or ResolvedAgent(role, "agy", "agy", "antigravity", "antigravity/g", {},
                         "agy", None, {}, "Read"),
    )
    monkeypatch.setattr(
        agent, "run_harness",
        lambda a, prompt, **kw: HarnessResult(
            "answer", 0, {"role": a.role, "profile": a.profile, "harness": a.harness,
                          "model": a.model},
        ),
    )
    _, run_record, _ = agent.run_role(
        s, "front", "question", cwd=tmp_path, timeout=30,
        selection=Selection("agy", "topic", 5731),
    )
    assert asked == ["agy"]
    # what was asked for …
    assert run_record["exec_option"] == "agy"
    assert run_record["exec_source"] == "topic"
    assert run_record["exec_message_id"] == 5731
    # … and what actually ran.
    assert (run_record["profile"], run_record["harness"]) == ("agy", "agy")


def test_a_run_with_no_selection_still_says_which_default_it_was(tmp_path, monkeypatch):
    s = spec(tmp_path, exec_options=(Option("agy"),))
    monkeypatch.setattr(
        agent, "resolve_spec_role",
        lambda spec, role, **kw: ResolvedAgent(
            role, "stub", "fake", "ollama", "ollama/test", {}, "agent", None, {}, "Read"
        ),
    )
    monkeypatch.setattr(
        agent, "run_harness",
        lambda a, prompt, **kw: HarnessResult("answer", 0, {"profile": a.profile}),
    )
    _, run_record, _ = agent.run_role(
        s, "front", "question", cwd=tmp_path, timeout=30, selection=Selection(),
    )
    assert run_record["exec_source"] == "default"
    assert "exec_option" not in run_record
