import pytest

from agag.instance import instance_name


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("AGENT_INSTANCE_NAME", raising=False)


def test_reads_the_name_from_the_file(tmp_path):
    path = tmp_path / "instance.toml"
    path.write_text('name = "agforge-somewhere2"\n', encoding="utf-8")
    assert instance_name(path, fallback="agforge") == "agforge-somewhere2"


def test_falls_back_to_the_plain_agent_name_without_a_file(tmp_path):
    assert instance_name(tmp_path / "absent.toml", fallback="autolab") == "autolab"


def test_falls_back_when_the_file_has_no_name(tmp_path):
    path = tmp_path / "instance.toml"
    path.write_text("# nothing here\n", encoding="utf-8")
    assert instance_name(path, fallback="autolab") == "autolab"


def test_env_wins_over_the_file(tmp_path, monkeypatch):
    path = tmp_path / "instance.toml"
    path.write_text('name = "agforge-fromfile1"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_INSTANCE_NAME", "agforge-fromenv1")
    assert (
        instance_name(path, fallback="agforge", env_var="AGENT_INSTANCE_NAME")
        == "agforge-fromenv1"
    )


def test_the_env_var_is_opt_in(tmp_path, monkeypatch):
    path = tmp_path / "instance.toml"
    path.write_text('name = "agforge-fromfile1"\n', encoding="utf-8")
    monkeypatch.setenv("AGENT_INSTANCE_NAME", "agforge-fromenv1")
    assert instance_name(path, fallback="agforge") == "agforge-fromfile1"
