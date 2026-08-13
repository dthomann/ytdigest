import pytest

from ytdigest.deliver import (
    TelegramError,
    deliver_telegram,
    escape_markdown_v2,
    format_header_message,
    format_live_announcement_message,
    format_video_message,
    send_alert,
    send_telegram_message,
)
from ytdigest.digest import Digest, VideoEntry

TRICKY_TITLE = "_ * [ ] ( ) ~ > # + - = | { } . !"


def test_markdown_v2_escaping_covers_all_special_chars():
    escaped = escape_markdown_v2(TRICKY_TITLE)
    for ch in "_*[]()~>#+-=|{}.!":
        assert f"\\{ch}" in escaped


def test_markdown_v2_escaping_round_trip_unescapes_cleanly():
    escaped = escape_markdown_v2(TRICKY_TITLE)
    # naive unescape: drop every backslash that precedes a special char
    unescaped = []
    i = 0
    while i < len(escaped):
        if escaped[i] == "\\" and i + 1 < len(escaped):
            unescaped.append(escaped[i + 1])
            i += 2
        else:
            unescaped.append(escaped[i])
            i += 1
    assert "".join(unescaped) == TRICKY_TITLE


def test_plain_text_untouched():
    assert escape_markdown_v2("Hello world 123") == "Hello world 123"


class FakeTelegramResponse:
    def __init__(self, ok=True, message_id=1):
        self._ok = ok
        self._message_id = message_id

    def json(self):
        if self._ok:
            return {"ok": True, "result": {"message_id": self._message_id}}
        return {"ok": False, "description": "boom"}


def test_format_video_message_contains_escaped_tricky_title():
    entry = VideoEntry(
        video_id="abc-123_XYZ",
        title=TRICKY_TITLE,
        channel_title="Some Channel",
        published_at="2026-08-05T09:00:00+00:00",
        summary="A plain summary.",
        duration_seconds=492,
    )
    text = format_video_message(entry, "Europe/Zurich")
    assert "0:08" in text  # 492s -> 0h08m -> "0:08" (colon isn't a MarkdownV2 special char)
    for ch in "_*[]()~>#+-=|{}.!":
        assert f"\\{ch}" in text
    assert "https://youtu\\.be/abc\\-123\\_XYZ" in text


def test_format_header_truncates_long_warning_list():
    digest = Digest(
        date="2026-08-13",
        warnings=[f"channel {i} failed" for i in range(12)],
    )
    text = format_header_message(digest, "UTC")
    assert "channel 0 failed" in text
    assert "channel 7 failed" in text
    assert "channel 8 failed" not in text
    assert "and 4 more" in text


def test_format_live_announcement_never_includes_summary():
    entry = VideoEntry(
        video_id="v1", title="Q&A", channel_title="Chan",
        published_at=None, scheduled_start="2026-08-06T18:00:00+00:00",
        summary="should never appear",
    )
    text = format_live_announcement_message(entry, "Europe/Zurich")
    assert "should never appear" not in text
    assert "Upcoming livestream" in text


def test_send_telegram_message_returns_message_id():
    post = lambda url, json=None, timeout=None: FakeTelegramResponse(message_id=42)
    msg_id = send_telegram_message("token", "chat", "hello", post_fn=post)
    assert msg_id == "42"


def test_send_telegram_message_raises_on_failure():
    post = lambda url, json=None, timeout=None: FakeTelegramResponse(ok=False)
    with pytest.raises(TelegramError):
        send_telegram_message("token", "chat", "hello", post_fn=post)


def test_deliver_telegram_sends_header_then_videos_and_records_deliveries(conn, config):
    from .conftest import insert_channel

    insert_channel(conn, "UC1", title="Chan One")
    for vid in ("v1", "v2", "v3"):
        conn.execute(
            "INSERT INTO videos (video_id, channel_id, state, discovered_at, updated_at) "
            "VALUES (?, 'UC1', 'summarized', 'now', 'now')",
            (vid,),
        )
    conn.commit()
    digest = Digest(
        date="2026-08-05",
        new_videos=[
            VideoEntry(video_id="v1", title="A", channel_title="Chan One", published_at=None, summary="s1"),
            VideoEntry(video_id="v2", title="B", channel_title="Chan One", published_at=None, summary="s2"),
        ],
        live_announcements=[
            VideoEntry(video_id="v3", title="Live", channel_title="Chan One", published_at=None,
                       scheduled_start="2026-08-06T18:00:00+00:00"),
        ],
    )
    sent_texts = []
    counter = {"n": 0}

    def post(url, json=None, timeout=None):
        counter["n"] += 1
        sent_texts.append(json["text"])
        return FakeTelegramResponse(message_id=counter["n"])

    conn.execute("INSERT INTO runs (started_at, status) VALUES ('now', 'ok')")
    run_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.commit()

    sent = deliver_telegram(digest, "token", "chat", config, conn, run_id, post_fn=post)

    assert len(sent) == 2
    assert counter["n"] == 4  # header + 1 live announcement + 2 videos
    deliveries = conn.execute("SELECT * FROM deliveries").fetchall()
    assert len(deliveries) == 2
    assert {d["video_id"] for d in deliveries} == {"v1", "v2"}


def test_send_alert_returns_false_without_credentials(config):
    config.secrets["TELEGRAM_BOT_TOKEN"] = ""
    assert send_alert(config, "something broke") is False


def test_send_alert_sends_when_credentials_present(config):
    config.secrets["TELEGRAM_BOT_TOKEN"] = "tok"
    config.secrets["TELEGRAM_ALLOWED_CHAT_ID"] = "chat"
    post = lambda url, json=None, timeout=None: FakeTelegramResponse(message_id=1)
    assert send_alert(config, "something broke", post_fn=post) is True
