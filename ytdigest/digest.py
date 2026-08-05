"""Compose the daily digest and always write it to data/digests/YYYY-MM-DD.md.

Stage 1: titles only (no summaries exist yet — that lands in Stage 2).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import VideoState


@dataclass
class VideoEntry:
    video_id: str
    title: str | None
    channel_title: str | None
    published_at: str | None
    kind: str | None = None
    summary: str | None = None
    scheduled_start: str | None = None


@dataclass
class Digest:
    date: str
    new_videos: list[VideoEntry] = field(default_factory=list)
    live_announcements: list[VideoEntry] = field(default_factory=list)
    transcript_pending: int = 0
    warnings: list[str] = field(default_factory=list)


def build_digest(
    conn: sqlite3.Connection,
    new_video_ids: list[str],
    newly_announced_ids: list[str],
    warnings: list[str],
    date: str | None = None,
) -> Digest:
    date = date or datetime.now(timezone.utc).date().isoformat()
    digest = Digest(date=date, warnings=list(warnings))

    def load(video_id: str) -> VideoEntry | None:
        row = conn.execute(
            """
            SELECT v.video_id, v.title, v.published_at, v.kind, v.summary, v.scheduled_start,
                   c.title AS channel_title
            FROM videos v JOIN channels c ON c.channel_id = v.channel_id
            WHERE v.video_id = ?
            """,
            (video_id,),
        ).fetchone()
        if row is None:
            return None
        return VideoEntry(
            video_id=row["video_id"],
            title=row["title"],
            channel_title=row["channel_title"],
            published_at=row["published_at"],
            kind=row["kind"],
            summary=row["summary"],
            scheduled_start=row["scheduled_start"],
        )

    for vid in new_video_ids:
        entry = load(vid)
        if entry is None:
            continue
        if entry.kind not in (None, "unknown") and entry.summary is None:
            # only surface videos worth telling the human about; shorts are silently dropped
            row = conn.execute("SELECT state FROM videos WHERE video_id = ?", (vid,)).fetchone()
            if row and row["state"] == VideoState.NEEDS_TRANSCRIPT.value:
                digest.new_videos.append(entry)
                digest.transcript_pending += 1

    for vid in newly_announced_ids:
        entry = load(vid)
        if entry is not None:
            digest.live_announcements.append(entry)

    return digest


def render_markdown(digest: Digest) -> str:
    lines = [f"# YouTube Digest — {digest.date}", ""]
    lines.append(
        f"{len(digest.new_videos)} new videos · "
        f"{len(digest.live_announcements)} livestreams announced · "
        f"{digest.transcript_pending} transcript pending"
    )
    if digest.warnings:
        lines.append("")
        lines.append("**Warnings:**")
        for w in digest.warnings:
            lines.append(f"- {w}")

    if digest.live_announcements:
        lines.append("")
        lines.append("## Upcoming livestreams")
        for e in digest.live_announcements:
            lines.append("")
            lines.append(f"🔴 **{e.channel_title}** — starts {e.scheduled_start or 'TBD'}")
            lines.append(f"*{e.title}*")
            lines.append(f"https://youtu.be/{e.video_id}")

    if digest.new_videos:
        lines.append("")
        lines.append("## New videos")
        for e in digest.new_videos:
            lines.append("")
            lines.append(f"*{e.title}*")
            lines.append(f"{e.channel_title} · {e.published_at or ''}")
            if e.summary:
                lines.append(e.summary)
            else:
                lines.append("_(summary pending — Stage 2)_")
            lines.append(f"https://youtu.be/{e.video_id}")

    if not digest.new_videos and not digest.live_announcements:
        lines.append("")
        lines.append("No new videos today.")

    lines.append("")
    return "\n".join(lines)


def write_digest_file(digest: Digest, digests_dir: Path) -> Path:
    digests_dir.mkdir(parents=True, exist_ok=True)
    path = digests_dir / f"{digest.date}.md"
    path.write_text(render_markdown(digest), encoding="utf-8")
    return path
