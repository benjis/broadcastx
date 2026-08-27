from pathlib import Path

import pytest

from broadcastx.config import ConfigError, load_config


def test_load_config_discovers_users_and_resolves_relative_output(tmp_path, monkeypatch):
    config_file = tmp_path / "broadcastx.toml"
    config_file.write_text(
        """
output_root = "archive"

[monitor]
check_interval_seconds = 60
live_interval_seconds = 15

[download]
timezone = "UTC"
datetime_format = "%Y-%m-%d_%H-%M-%S"
filename_template = "{datetime}_{broadcast_id}"

[[users]]
username = "@Alice"
timezone = "Australia/Sydney"

[[users]]
username = "BOB"
filename_template = "{broadcast_id}_{datetime}"
""".strip()
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.source == config_file
    assert config.output_root == tmp_path / "archive"
    assert [user.username for user in config.users] == ["alice", "bob"]
    assert config.download_for("ALICE").timezone == "Australia/Sydney"
    assert config.download_for("bob").timezone == "UTC"
    assert config.download_for("bob").filename_template == "{broadcast_id}_{datetime}"


def test_missing_automatic_config_uses_defaults(tmp_path):
    config = load_config(cwd=tmp_path)

    assert config.source is None
    assert config.output_root == tmp_path / "output"
    assert config.download.timezone == "UTC"
    assert config.users == ()


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="Configuration file not found"):
        load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    "body, message",
    [
        ('[[users]]\nusername = "Alice"\n[[users]]\nusername = "alice"', "Duplicate user"),
        ('[download]\ntimezone = "Sydney"', "Invalid timezone"),
        ('[download]\nfilename_template = "{title}_{broadcast_id}"', "Unknown filename field"),
        ('[download]\nfilename_template = "../{broadcast_id}"', "must produce a file name"),
    ],
)
def test_invalid_config_fails_before_runtime(tmp_path, body, message):
    path = tmp_path / "invalid.toml"
    path.write_text(body)

    with pytest.raises(ConfigError, match=message):
        load_config(path)
