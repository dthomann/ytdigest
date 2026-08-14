"""Loads config.yaml + .env, validates, fails loudly."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULTS = {
    "data_dir": "data",
    "timezone": "UTC",
    "digest_hour": 6,
    "rss_delay_seconds": [1, 2],
    "max_channel_consecutive_errors": 10,
    "min_duration_seconds": 180,
    "shorts_probe": False,
    "summarize_finished_livestreams": False,
    "youtube_api_quota_daily": 10000,
    "youtube_api_quota_warn_fraction": 0.9,
    "transcript_languages": ["en"],
    "transcript_delay_seconds": [2, 5],
    "max_transcript_fetches_per_run": 40,
    "enable_whisper_fallback": False,
    "whisper_max_duration_minutes": 120,
    "retry_backoff_hours": [6, 12, 24, 48, 96],
    "max_transcript_attempts": 5,
    "scheduled_retry_delay_hours": 1,
    "max_scheduled_retries": 3,
    "summary_model": "gemini-2.5-flash-lite",
    "summary_mode": "sync",
    "summary_words": [60, 100],
    "output_language": "en",
    "max_input_chars": 400000,
    "delivery_channel": "stdout",
    "telegram_message_delay_seconds": 1,
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "web_public_url": None,
}

# Secrets that must come from environment / .env, never from config.yaml.
REQUIRED_SECRETS = {
    "stdout": [],
    "file": [],
    "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_ID"],
}

SECRET_KEYS = (
    "youtube_api_key",
    "gemini_api_key",
    "groq_api_key",
    "telegram_bot_token",
    "telegram_allowed_chat_id",
)


class ConfigError(Exception):
    pass


@dataclass
class Config:
    values: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)
    config_path: Path | None = None

    def __getattr__(self, name):
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name)

    @property
    def data_dir(self) -> Path:
        base = Path(self.values["data_dir"])
        if not base.is_absolute() and self.config_path is not None:
            base = self.config_path.parent / base
        return base

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ytdigest.db"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def digests_dir(self) -> Path:
        return self.data_dir / "digests"


def _reject_secrets_in_yaml(raw: dict, config_path: Path) -> None:
    lower_keys = {k.lower() for k in raw.keys()}
    offenders = lower_keys & set(SECRET_KEYS)
    # also catch anything that merely looks like a secret (contains "key", "token", "secret")
    suspicious = {
        k
        for k in raw.keys()
        if any(term in k.lower() for term in ("api_key", "token", "secret", "password"))
    }
    offenders |= suspicious
    if offenders:
        raise ConfigError(
            f"Secrets must not be set in {config_path} — found key(s) {sorted(offenders)}. "
            "Put them in .env instead (see .env.example)."
        )


def load_config(config_path: str | Path = "config.yaml", env_path: str | Path | None = None) -> Config:
    """Load config.yaml (merged over DEFAULTS) and .env. Fails loudly on problems."""
    config_path = Path(config_path)
    if not config_path.exists():
        raise ConfigError(
            f"Config file not found: {config_path}. Copy config.example.yaml to {config_path} first."
        )

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level.")

    _reject_secrets_in_yaml(raw, config_path)

    unknown = set(raw.keys()) - set(DEFAULTS.keys())
    if unknown:
        raise ConfigError(f"Unknown config key(s) in {config_path}: {sorted(unknown)}")

    values = dict(DEFAULTS)
    values.update(raw)

    _validate(values, config_path)

    env_file = Path(env_path) if env_path else config_path.parent / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)

    secrets = {
        "YOUTUBE_API_KEY": os.environ.get("YOUTUBE_API_KEY", ""),
        "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", ""),
        "GROQ_API_KEY": os.environ.get("GROQ_API_KEY", ""),
        "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_ALLOWED_CHAT_ID": os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", ""),
        "YOUTUBE_OAUTH_CLIENT_ID": os.environ.get("YOUTUBE_OAUTH_CLIENT_ID", ""),
        "YOUTUBE_OAUTH_CLIENT_SECRET": os.environ.get("YOUTUBE_OAUTH_CLIENT_SECRET", ""),
        "WEB_AUTH_TOKEN": os.environ.get("WEB_AUTH_TOKEN", ""),
    }

    channel = values["delivery_channel"]
    missing = [k for k in REQUIRED_SECRETS.get(channel, []) if not secrets.get(k)]
    if missing:
        raise ConfigError(
            f"delivery_channel={channel!r} requires environment variable(s) {missing} "
            f"(set in {env_file})."
        )

    return Config(values=values, secrets=secrets, config_path=config_path)


def update_config_file(config_path: Path, updates: dict) -> None:
    """Merge updates into config.yaml and re-validate."""
    config_path = Path(config_path)
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level.")
    unknown = set(updates.keys()) - set(DEFAULTS.keys())
    if unknown:
        raise ConfigError(f"Cannot update unknown config key(s): {sorted(unknown)}")
    raw.update(updates)
    merged = dict(DEFAULTS)
    merged.update(raw)
    _validate(merged, config_path)
    with open(config_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _validate(values: dict, config_path: Path) -> None:
    def require_type(key, types):
        if not isinstance(values[key], types):
            raise ConfigError(f"{config_path}: {key!r} must be of type {types}, got {type(values[key])}")

    require_type("digest_hour", int)
    if not (0 <= values["digest_hour"] <= 23):
        raise ConfigError(f"{config_path}: digest_hour must be 0-23")

    require_type("min_duration_seconds", int)
    if values["min_duration_seconds"] < 0:
        raise ConfigError(f"{config_path}: min_duration_seconds must be >= 0")

    if values["delivery_channel"] not in ("telegram", "stdout", "file"):
        raise ConfigError(
            f"{config_path}: delivery_channel must be telegram|stdout|file, "
            f"got {values['delivery_channel']!r}"
        )

    if values["summary_mode"] not in ("sync", "batch"):
        raise ConfigError(f"{config_path}: summary_mode must be sync|batch")
    if values["summary_mode"] == "batch":
        raise ConfigError(
            f"{config_path}: summary_mode=batch is documented but not implemented; use sync."
        )

    if not (0 < values["youtube_api_quota_warn_fraction"] <= 1):
        raise ConfigError(f"{config_path}: youtube_api_quota_warn_fraction must be in (0, 1]")

    rss_delay = values["rss_delay_seconds"]
    if not (isinstance(rss_delay, list) and len(rss_delay) == 2 and rss_delay[0] <= rss_delay[1]):
        raise ConfigError(f"{config_path}: rss_delay_seconds must be [min, max] with min <= max")

    transcript_delay = values["transcript_delay_seconds"]
    if not (
        isinstance(transcript_delay, list)
        and len(transcript_delay) == 2
        and transcript_delay[0] <= transcript_delay[1]
    ):
        raise ConfigError(f"{config_path}: transcript_delay_seconds must be [min, max] with min <= max")

    require_type("scheduled_retry_delay_hours", (int, float))
    if values["scheduled_retry_delay_hours"] <= 0:
        raise ConfigError(f"{config_path}: scheduled_retry_delay_hours must be > 0")
    require_type("max_scheduled_retries", int)
    if values["max_scheduled_retries"] < 1:
        raise ConfigError(f"{config_path}: max_scheduled_retries must be >= 1")

    require_type("web_port", int)
    if not (1 <= values["web_port"] <= 65535):
        raise ConfigError(f"{config_path}: web_port must be 1-65535")

    if values["web_public_url"] is not None and not isinstance(values["web_public_url"], str):
        raise ConfigError(f"{config_path}: web_public_url must be a string or null")
