import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ytdigest import db
from ytdigest.config import Config
from ytdigest.util import utcnow_iso

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_ENV_KEYS = (
    "YOUTUBE_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_ID",
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    "WEB_AUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def _isolated_env():
    """load_config() calls python-dotenv with override=False, which only ever *adds* to
    os.environ and never clears it — so a secret set by one test's .env leaks into every test
    that runs afterward in the same process. Snapshot and restore the secret-relevant keys
    around each test so `.env` files stay test-local."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def load_fixture_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def load_fixture_json(name: str) -> dict:
    return json.loads(load_fixture_text(name))


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        values={
            "data_dir": str(tmp_path / "data"),
            "timezone": "UTC",
            "digest_hour": 6,
            "rss_delay_seconds": [0, 0],
            "max_channel_consecutive_errors": 10,
            "min_duration_seconds": 180,
            "shorts_probe": False,
            "summarize_finished_livestreams": False,
            "youtube_api_quota_daily": 10000,
            "youtube_api_quota_warn_fraction": 0.9,
            "transcript_languages": ["en"],
            "transcript_delay_seconds": [0, 0],
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
            "telegram_message_delay_seconds": 0,
            "web_host": "127.0.0.1",
            "web_port": 8080,
            "web_public_url": None,
        },
        secrets={
            "YOUTUBE_API_KEY": "test-key",
            "GEMINI_API_KEY": "",
            "GROQ_API_KEY": "",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_ALLOWED_CHAT_ID": "",
            "YOUTUBE_OAUTH_CLIENT_ID": "",
            "YOUTUBE_OAUTH_CLIENT_SECRET": "",
            "WEB_AUTH_TOKEN": "",
        },
        config_path=None,
    )


def insert_channel(conn, channel_id: str, title: str = "Test Channel", enabled: int = 1):
    conn.execute(
        "INSERT INTO channels (channel_id, title, added_at, enabled) VALUES (?, ?, ?, ?)",
        (channel_id, title, utcnow_iso(), enabled),
    )
    conn.commit()
