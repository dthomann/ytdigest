"""Telegram long-polling bot for reply-based Q&A."""
from __future__ import annotations

import logging
import signal
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import db, pipeline, qa
from .deliver import TELEGRAM_API
from .models import VideoState
from .run_lock import RunInProgressError
from .util import utcnow_iso

logger = logging.getLogger("ytdigest")

HELP_TEXT = """ytdigest bot commands:
/status — pipeline counts and last run
/run — start the full pipeline (discover → summarize → deliver)
/last — most recently delivered video
/channels — enabled channels
/retry <video_id> — reset a failed_permanent video for retry
/ask <video_id> <question> — ask about any video with a transcript

You can also reply to a digest video message with your question.
"""

_run_state_lock = threading.Lock()
_run_in_progress = False


class BotError(Exception):
    pass


def _chat_id(update: dict) -> str | None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    return str(chat_id) if chat_id is not None else None


def _message_text(update: dict) -> str | None:
    message = update.get("message") or update.get("edited_message")
    if not message:
        return None
    text = message.get("text")
    return text if isinstance(text, str) else None


def is_allowed_chat(update: dict, allowed_chat_id: str) -> bool:
    chat_id = _chat_id(update)
    return chat_id is not None and chat_id == str(allowed_chat_id)


def send_bot_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_to_message_id: int | None = None,
    post_fn=None,
) -> None:
    post_fn = post_fn or requests.post
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    resp = post_fn(
        TELEGRAM_API.format(token=bot_token, method="sendMessage"),
        json=payload,
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        raise BotError(f"sendMessage failed: {data}")


def get_updates(bot_token: str, offset: int = 0, timeout: int = 30, get_fn=None) -> list[dict]:
    get_fn = get_fn or requests.get
    resp = get_fn(
        TELEGRAM_API.format(token=bot_token, method="getUpdates"),
        params={"offset": offset, "timeout": timeout},
        timeout=timeout + 10,
    )
    data = resp.json()
    if not data.get("ok"):
        raise BotError(f"getUpdates failed: {data}")
    return data.get("result") or []


def format_status(conn: sqlite3.Connection) -> str:
    lines = ["Videos by state:"]
    for row in conn.execute("SELECT state, COUNT(*) AS n FROM videos GROUP BY state ORDER BY n DESC"):
        lines.append(f"  {row['state']}: {row['n']}")

    total = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    enabled = conn.execute("SELECT COUNT(*) AS n FROM channels WHERE enabled = 1").fetchone()["n"]
    lines.append(f"\nChannels: {enabled}/{total} enabled")

    last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if last_run:
        lines.append(
            f"\nLast run: #{last_run['id']} {last_run['started_at']} "
            f"status={last_run['status']} discovered={last_run['discovered']} "
            f"summarized={last_run['summarized']} api_units={last_run['api_units']}"
        )
    else:
        lines.append("\nLast run: none yet")

    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ? AND next_retry_at IS NOT NULL",
        (VideoState.NEEDS_TRANSCRIPT.value,),
    ).fetchone()["n"]
    lines.append(f"Pending retries: {pending}")
    return "\n".join(lines)


def format_last_delivery(conn: sqlite3.Connection, timezone_name: str) -> str:
    row = conn.execute(
        """
        SELECT d.sent_at, v.video_id, v.title, v.summary, c.title AS channel_title
        FROM deliveries d
        JOIN videos v ON v.video_id = d.video_id
        JOIN channels c ON c.channel_id = v.channel_id
        ORDER BY d.sent_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return "No deliveries recorded yet."

    sent_at = row["sent_at"]
    try:
        dt = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(timezone_name))
        sent_display = local.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        sent_display = sent_at

    title = row["title"] or row["video_id"]
    channel = row["channel_title"] or "unknown channel"
    link = f"https://youtu.be/{row['video_id']}"
    summary = row["summary"] or "(no summary)"
    return f"Last delivered ({sent_display}):\n{title}\n{channel}\n{summary}\n{link}"


def format_channels(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT channel_id, title FROM channels WHERE enabled = 1 ORDER BY title COLLATE NOCASE"
    ).fetchall()
    if not rows:
        return "No enabled channels."
    lines = [f"Enabled channels ({len(rows)}):"]
    for row in rows:
        label = row["title"] or row["channel_id"]
        lines.append(f"  {label} ({row['channel_id']})")
    return "\n".join(lines)


def format_run_result(result: pipeline.RunResult) -> str:
    lines = [
        f"Run #{result.run_id} finished status={result.status}",
        f"discovered={result.discovered} summarized={result.summarized} "
        f"failed={result.failed} api_units={result.api_units}",
    ]
    if result.notes:
        preview = "; ".join(result.notes[:5])
        if len(result.notes) > 5:
            preview += f" (+{len(result.notes) - 5} more)"
        lines.append(preview)
    return "\n".join(lines)


def start_pipeline_run(config, bot_token: str, chat_id: str, post_fn=None) -> str:
    global _run_in_progress
    with _run_state_lock:
        if _run_in_progress:
            return "A run is already in progress."
        _run_in_progress = True

    def _run() -> None:
        global _run_in_progress
        try:
            conn = db.connect(config.db_path)
            try:
                result = pipeline.run_pipeline(conn, config)
                send_bot_message(
                    bot_token,
                    chat_id,
                    format_run_result(result),
                    post_fn=post_fn,
                )
            except RunInProgressError:
                send_bot_message(
                    bot_token,
                    chat_id,
                    "A run is already in progress.",
                    post_fn=post_fn,
                )
            except Exception:
                logger.exception("bot-triggered run failed")
                send_bot_message(
                    bot_token,
                    chat_id,
                    "Pipeline run failed. Check logs for details.",
                    post_fn=post_fn,
                )
            finally:
                conn.close()
        finally:
            with _run_state_lock:
                _run_in_progress = False

    threading.Thread(target=_run, daemon=True).start()
    return "Pipeline run started…"


def cmd_retry(conn: sqlite3.Connection, video_id: str) -> str:
    row = conn.execute("SELECT state FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if row is None:
        return f"Unknown video_id {video_id!r}."
    if row["state"] != VideoState.FAILED_PERMANENT.value:
        return f"Video {video_id!r} is in state {row['state']!r}, not failed_permanent."
    now = utcnow_iso()
    conn.execute(
        """
        UPDATE videos
        SET state = ?, attempts = 0, next_retry_at = NULL, last_error = NULL, updated_at = ?
        WHERE video_id = ?
        """,
        (VideoState.NEEDS_TRANSCRIPT.value, now, video_id),
    )
    conn.commit()
    return f"Reset {video_id} for retry."


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return "", parts
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def handle_update(
    update: dict,
    config,
    conn: sqlite3.Connection,
    bot_token: str,
    allowed_chat_id: str,
    api_key: str,
    post_fn=None,
) -> None:
    if not is_allowed_chat(update, allowed_chat_id):
        logger.debug("ignoring update from unauthorized chat")
        return

    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = str(message["chat"]["id"])
    text = message.get("text")
    if not text or not isinstance(text, str):
        return

    reply_to = message.get("reply_to_message")
    command, args = parse_command(text)

    try:
        if command in ("/start", "/help"):
            send_bot_message(bot_token, chat_id, HELP_TEXT, post_fn=post_fn)
            return

        if command == "/status":
            send_bot_message(bot_token, chat_id, format_status(conn), post_fn=post_fn)
            return

        if command == "/run":
            send_bot_message(
                bot_token,
                chat_id,
                start_pipeline_run(config, bot_token, chat_id, post_fn=post_fn),
                post_fn=post_fn,
            )
            return

        if command == "/last":
            send_bot_message(
                bot_token,
                chat_id,
                format_last_delivery(conn, config.values["timezone"]),
                post_fn=post_fn,
            )
            return

        if command == "/channels":
            send_bot_message(bot_token, chat_id, format_channels(conn), post_fn=post_fn)
            return

        if command == "/retry":
            if not args:
                send_bot_message(bot_token, chat_id, "Usage: /retry <video_id>", post_fn=post_fn)
                return
            send_bot_message(bot_token, chat_id, cmd_retry(conn, args[0]), post_fn=post_fn)
            return

        if command == "/ask":
            if len(args) < 2:
                send_bot_message(
                    bot_token,
                    chat_id,
                    "Usage: /ask <video_id> <question>",
                    post_fn=post_fn,
                )
                return
            video_id, question = args[0], " ".join(args[1:])
            answer = qa.answer_question(conn, config, video_id, question, api_key, post_fn=post_fn)
            send_bot_message(
                bot_token,
                chat_id,
                answer,
                reply_to_message_id=message.get("message_id"),
                post_fn=post_fn,
            )
            return

        if command.startswith("/"):
            send_bot_message(
                bot_token,
                chat_id,
                f"Unknown command {command}. Send /help for available commands.",
                post_fn=post_fn,
            )
            return

        if reply_to:
            replied_id = reply_to.get("message_id")
            video_id = qa.lookup_video_by_message_id(conn, replied_id) if replied_id is not None else None
            if not video_id:
                send_bot_message(
                    bot_token,
                    chat_id,
                    "Reply to a specific digest video message to ask about that video.",
                    reply_to_message_id=message.get("message_id"),
                    post_fn=post_fn,
                )
                return
            answer = qa.answer_question(conn, config, video_id, text, api_key, post_fn=post_fn)
            send_bot_message(
                bot_token,
                chat_id,
                answer,
                reply_to_message_id=message.get("message_id"),
                post_fn=post_fn,
            )
            return

    except qa.QAError as exc:
        send_bot_message(
            bot_token,
            chat_id,
            str(exc),
            reply_to_message_id=message.get("message_id"),
            post_fn=post_fn,
        )
    except Exception:
        logger.exception("bot handler failed")
        send_bot_message(
            bot_token,
            chat_id,
            "Sorry, something went wrong processing that request.",
            reply_to_message_id=message.get("message_id"),
            post_fn=post_fn,
        )


def run_bot(
    config,
    conn: sqlite3.Connection,
    get_fn=None,
    post_fn=None,
    stop_event: threading.Event | None = None,
) -> None:
    """Long-poll Telegram until stop_event is set (or forever)."""
    bot_token = config.secrets.get("TELEGRAM_BOT_TOKEN")
    allowed_chat_id = config.secrets.get("TELEGRAM_ALLOWED_CHAT_ID")
    api_key = config.secrets.get("GEMINI_API_KEY")
    if not bot_token or not allowed_chat_id:
        raise BotError("TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_ID are required for the bot")
    if not api_key:
        raise BotError("GEMINI_API_KEY is required for Q&A")

    stop = stop_event or threading.Event()

    def _on_signal(signum, _frame):
        logger.info("received signal %s, stopping bot", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    offset = 0
    logger.info("bot listening (chat_id=%s)", allowed_chat_id)
    while not stop.is_set():
        try:
            updates = get_updates(bot_token, offset=offset, get_fn=get_fn)
        except Exception:
            logger.exception("getUpdates failed; retrying in 5s")
            stop.wait(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            handle_update(update, config, conn, bot_token, allowed_chat_id, api_key, post_fn=post_fn)

    logger.info("bot stopped")
