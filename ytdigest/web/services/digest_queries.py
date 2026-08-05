"""Digest query helpers for the web UI."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ... import db
from ...digest import VideoEntry


@dataclass
class RunSummary:
    id: int
    started_at: str
    finished_at: str | None
    discovered: int
    summarized: int
    failed: int
    status: str | None
    notes: str | None


def list_runs(conn: sqlite3.Connection, *, limit: int = 10, offset: int = 0) -> list[RunSummary]:
    rows = conn.execute(
        """
        SELECT id, started_at, finished_at, discovered, summarized, failed, status, notes
        FROM runs
        WHERE finished_at IS NOT NULL
        ORDER BY id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    return [
        RunSummary(
            id=r["id"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            discovered=r["discovered"] or 0,
            summarized=r["summarized"] or 0,
            failed=r["failed"] or 0,
            status=r["status"],
            notes=r["notes"],
        )
        for r in rows
    ]


def get_run(conn: sqlite3.Connection, run_id: int) -> RunSummary | None:
    r = conn.execute(
        """
        SELECT id, started_at, finished_at, discovered, summarized, failed, status, notes
        FROM runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if r is None:
        return None
    return RunSummary(
        id=r["id"],
        started_at=r["started_at"],
        finished_at=r["finished_at"],
        discovered=r["discovered"] or 0,
        summarized=r["summarized"] or 0,
        failed=r["failed"] or 0,
        status=r["status"],
        notes=r["notes"],
    )


def get_latest_run(conn: sqlite3.Connection) -> RunSummary | None:
    runs = list_runs(conn, limit=1, offset=0)
    return runs[0] if runs else None


def get_run_videos(conn: sqlite3.Connection, run_id: int, section: str | None = None) -> list[VideoEntry]:
    if section:
        rows = conn.execute(
            """
            SELECT v.video_id, v.title, v.published_at, v.kind, v.summary,
                   v.scheduled_start, v.last_error, v.duration_seconds, c.title AS channel_title
            FROM run_videos rv
            JOIN videos v ON v.video_id = rv.video_id
            JOIN channels c ON c.channel_id = v.channel_id
            WHERE rv.run_id = ? AND rv.section = ?
            ORDER BY v.published_at DESC
            """,
            (run_id, section),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT v.video_id, v.title, v.published_at, v.kind, v.summary,
                   v.scheduled_start, v.last_error, v.duration_seconds, c.title AS channel_title
            FROM run_videos rv
            JOIN videos v ON v.video_id = rv.video_id
            JOIN channels c ON c.channel_id = v.channel_id
            WHERE rv.run_id = ?
            ORDER BY rv.section, v.published_at DESC
            """,
            (run_id,),
        ).fetchall()
    return [
        VideoEntry(
            video_id=r["video_id"],
            title=r["title"],
            channel_title=r["channel_title"],
            published_at=r["published_at"],
            kind=r["kind"],
            summary=r["summary"],
            scheduled_start=r["scheduled_start"],
            last_error=r["last_error"],
            duration_seconds=r["duration_seconds"],
        )
        for r in rows
    ]


def get_run_sections(conn: sqlite3.Connection, run_id: int) -> dict[str, list[VideoEntry]]:
    return {
        "new_videos": get_run_videos(conn, run_id, "new_videos"),
        "live_announcements": get_run_videos(conn, run_id, "live_announcements"),
        "failed_transcripts": get_run_videos(conn, run_id, "failed_transcripts"),
    }
