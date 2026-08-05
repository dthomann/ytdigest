import pytest

from ytdigest.config import ConfigError, load_config


def write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return p


def test_missing_config_file_fails_loudly(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_secrets_in_yaml_rejected(tmp_path):
    write(tmp_path, "config.yaml", "youtube_api_key: abc123\n")
    with pytest.raises(ConfigError, match="Secrets must not be set"):
        load_config(tmp_path / "config.yaml")


def test_unknown_key_rejected(tmp_path):
    write(tmp_path, "config.yaml", "totally_bogus_key: 1\n")
    with pytest.raises(ConfigError, match="Unknown config key"):
        load_config(tmp_path / "config.yaml")


def test_defaults_applied(tmp_path):
    write(tmp_path, "config.yaml", "delivery_channel: stdout\n")
    config = load_config(tmp_path / "config.yaml")
    assert config.values["min_duration_seconds"] == 180
    assert config.values["digest_hour"] == 6


def test_telegram_channel_requires_secrets(tmp_path):
    write(tmp_path, "config.yaml", "delivery_channel: telegram\n")
    with pytest.raises(ConfigError, match="requires environment variable"):
        load_config(tmp_path / "config.yaml", env_path=tmp_path / "nonexistent.env")


def test_batch_summary_mode_not_implemented(tmp_path):
    write(tmp_path, "config.yaml", "summary_mode: batch\n")
    with pytest.raises(ConfigError, match="not implemented"):
        load_config(tmp_path / "config.yaml")


def test_invalid_digest_hour(tmp_path):
    write(tmp_path, "config.yaml", "digest_hour: 25\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "config.yaml")
