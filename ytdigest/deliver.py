"""Delivery backends: telegram | stdout | file.

Telegram delivers one message per video (the message_id is what makes Stage 3 reply-based Q&A
work, and it sidesteps the 4096-character message limit) plus a header and livestream
announcements. MarkdownV2 is used throughout with careful escaping of dynamic content only —
literal formatting characters we add ourselves (`*bold*`, bullets, em dashes) are left alone.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .digest import Digest, VideoEntry, render_markdown
from .util import jittered_sleep

logger = logging.getLogger("ytdigest")

MARKDOWN_V2_SPECIAL_CHARS = set("_*[]()~`>#+-=|{}.!\\")

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def escape_markdown_v2(text: str) -> str:
    """Escape all MarkdownV2 special characters per the Telegram Bot API spec."""
    out = []
    for ch in text:
        if ch in MARKDOWN_V2_SPECIAL_CHARS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


def deliver_stdout(digest: Digest) -> None:
    print(render_markdown(digest))


def deliver_file(digest: Digest, digests_dir: Path) -> Path:
    from .digest import write_digest_file

    return write_digest_file(digest, digests_dir)


# --------------------------------------------------------------------------------------
# Telegram formatting
# --------------------------------------------------------------------------------------


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?:??"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return f"{hours}:{minutes:02d}"


def _format_local_time(iso_ts: str | None, timezone_name: str) -> str:
    if not iso_ts:
        return "unknown time"
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(timezone_name))
        return local.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_ts


def format_header_message(digest: Digest, timezone_name: str) -> str:
    title = f"*YouTube Digest — {escape_markdown_v2(digest.date)}*"
    counts = escape_markdown_v2(
        f"{len(digest.new_videos)} new videos · {len(digest.live_announcements)} livestreams "
        f"announced · {digest.transcript_pending} transcript pending"
    )
    lines = [title, "", counts]
    if digest.warnings:
        lines.append("")
        for w in digest.warnings:
            lines.append(f"⚠️ {escape_markdown_v2(w)}")
    return "\n".join(lines)


def format_video_message(entry: VideoEntry, timezone_name: str) -> str:
    title = escape_markdown_v2(entry.title or "Untitled")
    channel = escape_markdown_v2(entry.channel_title or "")
    duration = escape_markdown_v2(_format_duration(entry.duration_seconds))
    published = escape_markdown_v2(_format_local_time(entry.published_at, timezone_name))
    summary = escape_markdown_v2(entry.summary or "(summary unavailable)")
    link = escape_markdown_v2(f"https://youtu.be/{entry.video_id}")
    return f"*{title}*\n{channel} · {duration} · {published}\n{summary}\n{link}"


def format_live_announcement_message(entry: VideoEntry, timezone_name: str) -> str:
    channel = escape_markdown_v2(entry.channel_title or "")
    title = escape_markdown_v2(entry.title or "Untitled")
    starts = escape_markdown_v2(_format_local_time(entry.scheduled_start, timezone_name))
    link = escape_markdown_v2(f"https://youtu.be/{entry.video_id}")
    return f"🔴 Upcoming livestream — {channel}\n*{title}* — starts {starts}\n{link}"


# --------------------------------------------------------------------------------------
# Telegram transport
# --------------------------------------------------------------------------------------


class TelegramError(Exception):
    pass


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    disable_web_page_preview: bool = True,
    post_fn=None,
) -> str:
    """Send one MarkdownV2 message. Returns the Telegram message_id as a string."""
    post_fn = post_fn or requests.post
    resp = post_fn(
        TELEGRAM_API.format(token=bot_token, method="sendMessage"),
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": disable_web_page_preview,
        },
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(f"sendMessage failed: {data}")
    return str(data["result"]["message_id"])


def deliver_telegram(
    digest: Digest,
    bot_token: str,
    chat_id: str,
    config,
    conn: sqlite3.Connection,
    run_id: int | None,
    post_fn=None,
) -> list[tuple[str, str]]:
    """Send the header, livestream announcements, then one message per video.

    Returns a list of (message_id, video_id) for every video message successfully sent, and
    records each into the `deliveries` table. Raises TelegramError on the first failure — the
    caller decides how to treat a partial send (the digest file was already written regardless).
    """
    timezone_name = config.values["timezone"]
    delay = config.values["telegram_message_delay_seconds"]
    sent: list[tuple[str, str]] = []

    send_telegram_message(bot_token, chat_id, format_header_message(digest, timezone_name), post_fn=post_fn)

    for entry in digest.live_announcements:
        jittered_sleep(delay, delay)
        send_telegram_message(
            bot_token, chat_id, format_live_announcement_message(entry, timezone_name), post_fn=post_fn
        )

    for entry in digest.new_videos:
        jittered_sleep(delay, delay)
        message_id = send_telegram_message(
            bot_token, chat_id, format_video_message(entry, timezone_name), post_fn=post_fn
        )
        conn.execute(
            "INSERT INTO deliveries (message_id, video_id, run_id, sent_at) VALUES (?, ?, ?, ?)",
            (message_id, entry.video_id, run_id, datetime.now().astimezone().isoformat()),
        )
        conn.commit()
        sent.append((message_id, entry.video_id))

    return sent


def send_alert(config, message: str, post_fn=None) -> bool:
    """Best-effort failure alert. Never raises — a broken alert must not crash the run."""
    bot_token = config.secrets.get("TELEGRAM_BOT_TOKEN")
    chat_id = config.secrets.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not bot_token or not chat_id:
        logger.warning("cannot send failure alert (no Telegram credentials): %s", message)
        return False
    try:
        send_telegram_message(bot_token, chat_id, f"⚠️ ytdigest: {escape_markdown_v2(message)}", post_fn=post_fn)
        return True
    except Exception:
        logger.exception("failed to send failure alert")
        return False


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------


def deliver(digest: Digest, channel: str, config, conn: sqlite3.Connection = None, run_id: int = None) -> None:
    if channel == "stdout":
        deliver_stdout(digest)
    elif channel == "file":
        deliver_file(digest, config.digests_dir)
    elif channel == "telegram":
        deliver_telegram(
            digest,
            config.secrets["TELEGRAM_BOT_TOKEN"],
            config.secrets["TELEGRAM_ALLOWED_CHAT_ID"],
            config,
            conn,
            run_id,
        )
    else:
        raise ValueError(f"Unknown delivery channel: {channel!r}")
