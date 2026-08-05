"""Compose the daily digest and always write it to data/digests/YYYY-MM-DD.md."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class VideoEntry:
    video_id: str
    title: str | None
    channel_title: str | None
    published_at: str | None
    kind: str | None = None
    summary: str | None = None
    scheduled_start: str | None = None
    last_error: str | None = None
    duration_seconds: int | None = None


@dataclass
class Digest:
    date: str
    new_videos: list[VideoEntry] = field(default_factory=list)
    live_announcements: list[VideoEntry] = field(default_factory=list)
    failed_transcripts: list[VideoEntry] = field(default_factory=list)
    transcript_pending: int = 0
    warnings: list[str] = field(default_factory=list)


def _load_entry(conn: sqlite3.Connection, video_id: str) -> VideoEntry | None:
    row = conn.execute(
        """
        SELECT v.video_id, v.title, v.published_at, v.kind, v.summary, v.scheduled_start,
               v.last_error, v.duration_seconds, c.title AS channel_title
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
        last_error=row["last_error"],
        duration_seconds=row["duration_seconds"],
    )


def build_digest(
    conn: sqlite3.Connection,
    summarized_ids: list[str],
    upcoming_ids: list[str],
    failed_transcript_ids: list[str] | None = None,
    warnings: list[str] | None = None,
    transcript_pending: int = 0,
    date: str | None = None,
) -> Digest:
    date = date or datetime.now(timezone.utc).date().isoformat()
    digest = Digest(date=date, warnings=list(warnings or []), transcript_pending=transcript_pending)

    for vid in summarized_ids:
        entry = _load_entry(conn, vid)
        if entry is not None:
            digest.new_videos.append(entry)

    for vid in upcoming_ids:
        entry = _load_entry(conn, vid)
        if entry is not None:
            digest.live_announcements.append(entry)

    for vid in failed_transcript_ids or []:
        entry = _load_entry(conn, vid)
        if entry is not None:
            digest.failed_transcripts.append(entry)

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
            lines.append(e.summary or "_(summary unavailable)_")
            lines.append(f"https://youtu.be/{e.video_id}")

    if digest.failed_transcripts:
        lines.append("")
        lines.append("## Couldn't get a transcript")
        for e in digest.failed_transcripts:
            lines.append(f"- *{e.title}* ({e.channel_title}) — {e.last_error or 'unknown reason'} — https://youtu.be/{e.video_id}")

    if not digest.new_videos and not digest.live_announcements and not digest.failed_transcripts:
        lines.append("")
        lines.append("No new videos today.")

    lines.append("")
    return "\n".join(lines)


def write_digest_file(digest: Digest, digests_dir: Path) -> Path:
    digests_dir.mkdir(parents=True, exist_ok=True)
    path = digests_dir / f"{digest.date}.md"
    path.write_text(render_markdown(digest), encoding="utf-8")
    return path
