"""Jittered sleep, retry decorator, text cleaning helpers."""
from __future__ import annotations

import functools
import logging
import random
import time
from datetime import datetime, timezone

logger = logging.getLogger("ytdigest")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def jittered_sleep(low: float, high: float, dry_run: bool = False) -> float:
    """Sleep a random duration in [low, high] seconds. No-op under --dry-run."""
    duration = random.uniform(low, high)
    if not dry_run:
        time.sleep(duration)
    return duration


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def retry(times: int = 3, exceptions: tuple = (Exception,), backoff_base: float = 1.5):
    """Retry decorator with exponential backoff. Logs each retry."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, times + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == times:
                        break
                    delay = backoff_base**attempt
                    logger.warning(
                        "retry %s/%s for %s after %s: %s", attempt, times, fn.__name__, exc, delay
                    )
                    time.sleep(delay)
            raise last_exc

        return wrapper

    return decorator


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())
