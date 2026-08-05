"""Dataclasses mirroring the DB rows, plus the video state enum."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VideoState(str, Enum):
    DISCOVERED = "discovered"
    SKIPPED_SHORT = "skipped_short"
    LIVE_UPCOMING = "live_upcoming"
    LIVE_NOW = "live_now"
    LIVE_FINISHED = "live_finished"
    NEEDS_TRANSCRIPT = "needs_transcript"
    HAS_TRANSCRIPT = "has_transcript"
    SUMMARIZED = "summarized"
    DELIVERED = "delivered"
    FAILED_PERMANENT = "failed_permanent"


TERMINAL_STATES = {
    VideoState.SKIPPED_SHORT,
    VideoState.LIVE_FINISHED,  # terminal by default; see summarize_finished_livestreams
    VideoState.DELIVERED,
    VideoState.FAILED_PERMANENT,
}


class VideoKind(str, Enum):
    SHORT = "short"
    LIVE = "live"
    NORMAL = "normal"
    UNKNOWN = "unknown"


class LiveBroadcast(str, Enum):
    NONE = "none"
    UPCOMING = "upcoming"
    LIVE = "live"


@dataclass
class Channel:
    channel_id: str
    title: str | None = None
    handle: str | None = None
    enabled: int = 1
    added_at: str | None = None
    last_polled_at: str | None = None
    consecutive_errors: int = 0
    last_error: str | None = None


@dataclass
class Video:
    video_id: str
    channel_id: str
    title: str | None = None
    published_at: str | None = None
    duration_seconds: int | None = None
    live_broadcast: str | None = None
    scheduled_start: str | None = None
    actual_end: str | None = None
    kind: str | None = None
    state: str = VideoState.DISCOVERED.value
    announced_at: str | None = None
    transcript_source: str | None = None
    transcript_lang: str | None = None
    transcript_auto: int | None = None
    transcript_chars: int | None = None
    summary: str | None = None
    summary_model: str | None = None
    attempts: int = 0
    next_retry_at: str | None = None
    last_error: str | None = None
    discovered_at: str | None = None
    updated_at: str | None = None


@dataclass
class Run:
    id: int | None
    started_at: str
    finished_at: str | None = None
    discovered: int = 0
    summarized: int = 0
    failed: int = 0
    api_units: int = 0
    status: str | None = None
    notes: str | None = None


@dataclass
class Delivery:
    message_id: str
    video_id: str
    run_id: int
    sent_at: str
