"""Run issue reporting — always visible on stdout/stderr; Telegram when configured."""
from __future__ import annotations

import logging
import sys

from .config import Config

logger = logging.getLogger("ytdigest")


def report_run_issues(
    config: Config,
    *,
    run_id: int,
    status: str,
    notes: list[str],
    post_fn=None,
) -> None:
    """Log and print run problems; send a Telegram alert when credentials are available."""
    if status == "ok" and not notes:
        return

    header = f"run #{run_id} status={status}"
    if notes:
        shown = notes[:8]
        body = "\n".join(f"• {n}" for n in shown)
        extra = len(notes) - len(shown)
        if extra:
            body += f"\n• … and {extra} more (see /status)"
        message = f"{header}\n{body}"
    else:
        message = header

    logger.warning("run issues: %s", message.replace("\n", " | "))
    print(message, file=sys.stderr)

    bot_token = config.secrets.get("TELEGRAM_BOT_TOKEN")
    chat_id = config.secrets.get("TELEGRAM_ALLOWED_CHAT_ID")
    if not bot_token or not chat_id:
        return

    # Re-use deliver helper; import here to avoid circular imports at module load.
    from .deliver import send_alert

    send_alert(config, message, post_fn=post_fn)
