"""Delivery backends: telegram | stdout | file.

Stage 1 implements stdout and file. Telegram sending is Stage 2 (per-message delivery tied to
Q&A via `deliveries.message_id`); the MarkdownV2 escaping helper is implemented now since it's a
pure function the test suite exercises independently of network access.
"""
from __future__ import annotations

from pathlib import Path

from .digest import Digest, render_markdown

MARKDOWN_V2_SPECIAL_CHARS = set("_*[]()~`>#+-=|{}.!\\")


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


def deliver_telegram(digest: Digest, bot_token: str, chat_id: str) -> list[str]:
    raise NotImplementedError(
        "Telegram delivery lands in Stage 2 (per-video messages + deliveries table). "
        "Use delivery_channel: stdout or file for now."
    )


def deliver(digest: Digest, channel: str, config) -> None:
    if channel == "stdout":
        deliver_stdout(digest)
    elif channel == "file":
        deliver_file(digest, config.digests_dir)
    elif channel == "telegram":
        deliver_telegram(
            digest,
            config.secrets["TELEGRAM_BOT_TOKEN"],
            config.secrets["TELEGRAM_ALLOWED_CHAT_ID"],
        )
    else:
        raise ValueError(f"Unknown delivery channel: {channel!r}")
