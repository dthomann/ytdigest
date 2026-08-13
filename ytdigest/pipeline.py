"""Full pipeline orchestration — shared by CLI and web UI."""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import classify, deliver as deliver_mod, digest as digest_mod, discover, metadata
from . import summarize as summarize_mod, transcript as transcript_mod
from .config import Config
from .models import VideoState
from .run_lock import RunInProgressError, run_lock
from .run_report import report_run_issues
from .util import utcnow_iso

logger = logging.getLogger("ytdigest.pipeline")

METADATA_REFRESH_STATES = (
    VideoState.DISCOVERED.value,
    VideoState.LIVE_UPCOMING.value,
    VideoState.LIVE_NOW.value,
)


@dataclass
class RunResult:
    run_id: int
    status: str
    discovered: int = 0
    summarized: int = 0
    failed: int = 0
    api_units: int = 0
    notes: list[str] = field(default_factory=list)


def lock_path(config: Config) -> Path:
    return config.data_dir / ".run.lock"


def _video_ids_needing_metadata(conn) -> list[str]:
    placeholders = ",".join("?" for _ in METADATA_REFRESH_STATES)
    rows = conn.execute(
        f"SELECT video_id FROM videos WHERE state IN ({placeholders})",
        METADATA_REFRESH_STATES,
    ).fetchall()
    return [r["video_id"] for r in rows]


def discover_metadata_classify(conn, config):
    discover_result = discover.discover_all(conn, config, dry_run=False)
    metadata_ids = _video_ids_needing_metadata(conn)

    quota_error = None
    api_units = 0
    api_key = config.secrets.get("YOUTUBE_API_KEY")
    if metadata_ids:
        if not api_key:
            quota_error = "YOUTUBE_API_KEY not set — cannot fetch metadata"
        else:
            try:
                items, missing, api_units = metadata.fetch_all_metadata(
                    metadata_ids,
                    api_key,
                    quota_daily=config.values["youtube_api_quota_daily"],
                    quota_warn_fraction=config.values["youtube_api_quota_warn_fraction"],
                )
                metadata.apply_metadata(conn, items, missing)
            except metadata.QuotaExceededError as exc:
                quota_error = str(exc)

    classify.classify_all(conn, config)
    upcoming_ids = [
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM videos WHERE state = ?",
            (VideoState.LIVE_UPCOMING.value,),
        ).fetchall()
    ]
    return discover_result, upcoming_ids, quota_error, api_units


def _run_transcript_phase(conn, config, limit=None) -> tuple[list[str], list[str]]:
    notes = []
    result = transcript_mod.run_transcript_phase(conn, config, limit=limit)
    if result.aborted:
        notes.append(f"transcript phase aborted: {result.abort_reason}")
    for err in result.errors:
        notes.append(f"transcript: {err}")
    logger.info(
        "transcript phase: attempted=%d succeeded=%d failed_permanent=%d retrying=%d aborted=%s",
        result.attempted,
        len(result.succeeded_ids),
        len(result.failed_permanent_ids),
        result.retrying,
        result.aborted,
    )
    return notes, result.failed_permanent_ids


def _run_summarize_phase(conn, config) -> tuple[list[str], list[str]]:
    notes = []
    api_key = config.secrets.get("GEMINI_API_KEY")
    if not api_key:
        notes.append("GEMINI_API_KEY not set — cannot summarize")
        return notes, []
    result = summarize_mod.run_summarize_phase(conn, config, config.transcripts_dir, api_key)
    logger.info(
        "summarize phase: attempted=%d succeeded=%d failed=%d",
        result.attempted,
        len(result.succeeded_ids),
        len(result.failed_ids),
    )
    return notes, result.succeeded_ids


def record_run_videos(conn, run_id: int, digest: digest_mod.Digest) -> None:
    rows = []
    for entry in digest.new_videos:
        rows.append((run_id, entry.video_id, "new_videos"))
    for entry in digest.live_announcements:
        rows.append((run_id, entry.video_id, "live_announcements"))
    for entry in digest.failed_transcripts:
        rows.append((run_id, entry.video_id, "failed_transcripts"))
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO run_videos (run_id, video_id, section) VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()


def build_and_deliver_digest(conn, config, channel, upcoming_ids, failed_transcript_ids, notes, run_id):
    summarized_ids = [
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM videos WHERE state = ? ORDER BY discovered_at",
            (VideoState.SUMMARIZED.value,),
        ).fetchall()
    ]
    transcript_pending = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE state = ?", (VideoState.NEEDS_TRANSCRIPT.value,)
    ).fetchone()["n"]

    d = digest_mod.build_digest(
        conn,
        summarized_ids,
        upcoming_ids,
        failed_transcript_ids,
        notes,
        transcript_pending=transcript_pending,
    )
    digest_mod.write_digest_file(d, config.digests_dir)
    record_run_videos(conn, run_id, d)

    delivery_error = None
    try:
        if channel == "telegram":
            deliver_mod.deliver_telegram(
                d,
                config.secrets["TELEGRAM_BOT_TOKEN"],
                config.secrets["TELEGRAM_ALLOWED_CHAT_ID"],
                config,
                conn,
                run_id,
            )
        else:
            deliver_mod.deliver(d, channel, config)
    except Exception as exc:
        logger.exception("delivery failed")
        delivery_error = str(exc)

    if channel == "telegram":
        delivered_ids = {
            r["video_id"]
            for r in conn.execute(
                "SELECT video_id FROM deliveries WHERE run_id = ?", (run_id,)
            ).fetchall()
        }
    elif delivery_error is None:
        delivered_ids = set(summarized_ids)
    else:
        delivered_ids = set()

    if delivered_ids:
        now = utcnow_iso()
        conn.executemany(
            "UPDATE videos SET state = ?, updated_at = ? WHERE video_id = ?",
            [(VideoState.DELIVERED.value, now, vid) for vid in delivered_ids],
        )
        conn.commit()

    return d, delivery_error


def run_pipeline(
    conn: sqlite3.Connection,
    config: Config,
    *,
    limit: int | None = None,
    channel: str | None = None,
    use_lock: bool = True,
) -> RunResult:
    channel = channel or config.values["delivery_channel"]
    lock = run_lock(lock_path(config)) if use_lock else _null_context()

    with lock:
        run_started = utcnow_iso()
        cur = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'ok')", (run_started,))
        run_id = cur.lastrowid
        conn.commit()

        status = "ok"
        notes: list[str] = []
        api_units = 0
        failed_transcript_ids: list[str] = []
        digest = digest_mod.Digest(date=run_started[:10])

        try:
            discover_result, upcoming_ids, quota_error, api_units = discover_metadata_classify(
                conn, config
            )
            if discover_result.channels_failed:
                status = "partial"
                notes.append(
                    f"{discover_result.channels_failed}/{discover_result.channels_polled} "
                    "channels failed RSS poll after retry"
                )
            if discover_result.dead_channel_warnings:
                notes.extend(discover_result.dead_channel_warnings)
            if quota_error:
                notes.append(quota_error)
                status = "partial"

            transcript_notes, failed_transcript_ids = _run_transcript_phase(conn, config, limit=limit)
            if transcript_notes:
                notes.extend(transcript_notes)
                status = "partial"

            summarize_notes, _ = _run_summarize_phase(conn, config)
            if summarize_notes:
                notes.extend(summarize_notes)
                status = "partial"

            digest, delivery_error = build_and_deliver_digest(
                conn, config, channel, upcoming_ids, failed_transcript_ids, notes, run_id
            )
            if delivery_error:
                notes.append(f"delivery failed: {delivery_error}")
                status = "error"

            conn.execute(
                """
                UPDATE runs SET finished_at = ?, discovered = ?, summarized = ?, failed = ?,
                                api_units = ?, status = ?, notes = ?
                WHERE id = ?
                """,
                (
                    utcnow_iso(),
                    discover_result.new_videos,
                    len(digest.new_videos),
                    len(failed_transcript_ids),
                    api_units,
                    status,
                    "; ".join(notes) if notes else None,
                    run_id,
                ),
            )
            conn.commit()

            if status in ("error", "partial"):
                report_run_issues(
                    config,
                    run_id=run_id,
                    status=status,
                    notes=notes,
                )
        except Exception as exc:
            logger.exception("run failed")
            conn.execute(
                "UPDATE runs SET finished_at = ?, status = 'error', notes = ? WHERE id = ?",
                (utcnow_iso(), str(exc), run_id),
            )
            conn.commit()
            report_run_issues(
                config,
                run_id=run_id,
                status="error",
                notes=[f"crashed: {exc}"],
            )
            raise

        return RunResult(
            run_id=run_id,
            status=status,
            discovered=discover_result.new_videos,
            summarized=len(digest.new_videos),
            failed=len(failed_transcript_ids),
            api_units=api_units,
            notes=notes,
        )


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
