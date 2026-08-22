"""`agag provision`: guarded bot creation and idempotent channel setup."""

from pathlib import Path

import pytest

from agag import init
from agag.cli import main
from agag.provision import ProvisionError, provision


class FakeClient:
    def __init__(self, *, existing_user=None, existing_channel=None):
        self.existing_user = existing_user
        self.existing_channel = existing_channel
        self.calls = []

    def user_by_email(self, email):
        self.calls.append(("user_by_email", email))
        return self.existing_user

    def create_bot(self, full_name, short_name):
        self.calls.append(("create_bot", full_name, short_name))
        return {
            "user_id": 21,
            "email": "agecho-lab1-bot@zulip.example.invalid",
            "api_key": "new-bot-key",
        }

    def whoami(self):
        self.calls.append(("whoami",))
        return {"user_id": 7}

    def subscribe_channels(self, names, principals=None):
        self.calls.append(("subscribe_channels", names, principals))
        return {"subscribed": []}

    def channels(self):
        self.calls.append(("channels",))
        return [self.existing_channel] if self.existing_channel else []

    def create_channel(self, name, description, principals):
        self.calls.append(("create_channel", name, description, principals))
        return {"subscribed": []}

    def update_channel_description(self, stream_id, description):
        self.calls.append(("update_channel_description", stream_id, description))
        return {"result": "success"}


def project(tmp_path: Path) -> Path:
    plan = init.Plan(
        "agecho", "agecho-lab1", "agechoplan-", "agechorun-", dest=tmp_path
    )
    return init.generate(plan)


def admin_env(tmp_path: Path) -> Path:
    path = tmp_path / "admin.env"
    path.write_text(
        "ZULIP_URL=https://zulip.example.invalid\n"
        "ZULIP_EMAIL=provisioner@zulip.example.invalid\n"
        "ZULIP_API_KEY=admin-key\n"
        "ZULIP_CA_BUNDLE=/a/ca.pem\n",
        encoding="utf-8",
    )
    return path


def test_provision_creates_bot_env_and_channels(tmp_path):
    root = project(tmp_path)
    client = FakeClient()
    result = provision(
        root,
        admin_env=admin_env(tmp_path),
        client_factory=lambda path: client,
    )

    assert result.instance == "agecho-lab1"
    assert result.channel_created
    credential = root / ".local" / "zulip.env"
    assert credential.stat().st_mode & 0o777 == 0o600
    assert credential.read_text() == (
        "ZULIP_URL=https://zulip.example.invalid\n"
        "ZULIP_EMAIL=agecho-lab1-bot@zulip.example.invalid\n"
        "ZULIP_API_KEY=new-bot-key\n"
        "ZULIP_CA_BUNDLE=/a/ca.pem\n"
    )
    assert ("subscribe_channels", ["agents"], [21]) in client.calls
    assert (
        "create_channel",
        "agecho-lab1",
        "The conversational entrance for `agecho-lab1`: plain topics ask this instance a "
        "question, and `agechoplan-…` topics request its work.",
        [21, 7],
    ) in client.calls


def test_provision_updates_an_existing_channel_description(tmp_path):
    root = project(tmp_path)
    client = FakeClient(existing_channel={"stream_id": 33, "name": "agecho-lab1"})
    result = provision(
        root,
        admin_env=admin_env(tmp_path),
        description="Updated for {instance}",
        client_factory=lambda path: client,
    )
    assert not result.channel_created
    assert ("update_channel_description", 33, "Updated for agecho-lab1") in client.calls


def test_provision_accepts_remote_instance_and_output_path(tmp_path):
    root = project(tmp_path)
    (root / ".local" / "instance.toml").unlink()
    credential = tmp_path / "controller" / "agecho-remote.env"
    client = FakeClient()

    result = provision(
        root,
        admin_env=admin_env(tmp_path),
        instance="agecho-remote",
        out=credential,
        client_factory=lambda path: client,
    )

    assert result.instance == "agecho-remote"
    assert result.credential_path == credential.resolve()
    assert credential.stat().st_mode & 0o777 == 0o600
    assert ("user_by_email", "agecho-remote-bot@zulip.example.invalid") in client.calls
    assert (
        "create_channel",
        "agecho-remote",
        "The conversational entrance for `agecho-remote`: plain topics ask this instance a "
        "question, and `agechoplan-…` topics request its work.",
        [21, 7],
    ) in client.calls


def test_provision_refuses_an_existing_bot_before_any_write(tmp_path):
    root = project(tmp_path)
    client = FakeClient(existing_user={"user_id": 22})
    with pytest.raises(ProvisionError, match="already exists"):
        provision(root, admin_env=admin_env(tmp_path), client_factory=lambda path: client)
    assert client.calls == [("user_by_email", "agecho-lab1-bot@zulip.example.invalid")]
    assert not (root / ".local" / "zulip.env").exists()


def test_provision_cli_requires_the_admin_credential_path(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGAG_ZULIP_ADMIN_ENV", raising=False)
    assert main(["provision", str(tmp_path)]) == 2
    assert "AGAG_ZULIP_ADMIN_ENV" in capsys.readouterr().err
