"""ytdigest CLI.

Stage 1: init-db, add-channel, import-channels, seed, run, discover, status.
Stage 2: fetch-transcripts, summarize, deliver, retry, export.
Stage 3 (not yet implemented): ask, bot.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import classify, db, deliver as deliver_mod, digest as digest_mod, discover, metadata
from . import summarize as summarize_mod, transcript as transcript_mod
from .config import Config, ConfigError, load_config
from .models import VideoState
from .util import utcnow_iso

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.resolve_channels import resolve_file, resolve_one  # noqa: E402

logger = logging.getLogger("ytdigest")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load_config(args) -> Config:
    try:
        return load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)


def _connect(config: Config):
    return db.init_db(config.db_path)


def cmd_init_db(args) -> None:
    config = _load_config(args)
    _connect(config)
    print(f"Initialized database at {config.db_path}")


def cmd_add_channel(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    try:
        resolved = resolve_one(args.channel, api_key=config.secrets.get("YOUTUBE_API_KEY") or None)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO channels (channel_id, title, handle, added_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(channel_id) DO UPDATE SET enabled = 1
        """,
        (resolved.channel_id, resolved.title, resolved.handle, now),
    )
    conn.commit()
    print(f"Added channel {resolved.channel_id} ({resolved.title or 'unknown title'})")


def cmd_import_channels(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    try:
        resolved = resolve_file(args.file, api_key=config.secrets.get("YOUTUBE_API_KEY") or None)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    now = utcnow_iso()
    added = 0
    for r in resolved:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO channels (channel_id, title, handle, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (r.channel_id, r.title, r.handle, now),
        )
        added += cur.rowcount
    conn.commit()
    print(f"Imported {added} new channel(s) ({len(resolved)} total in file)")


METADATA_REFRESH_STATES = (
    VideoState.DISCOVERED.value,
    VideoState.LIVE_UPCOMING.value,
    VideoState.LIVE_NOW.value,
)


def _video_ids_needing_metadata(conn) -> list[str]:
    placeholders = ",".join("?" for _ in METADATA_REFRESH_STATES)
    rows = conn.execute(
        f"SELECT video_id FROM videos WHERE state IN ({placeholders})",
        METADATA_REFRESH_STATES,
    ).fetchall()
    return [r["video_id"] for r in rows]


def _discover_metadata_classify(conn, config):
    """Shared discover -> metadata -> classify pipeline.

    Returns (discover_result, new_ids, classify_counts, upcoming_ids, quota_error, api_units).
    """
    discover_result = discover.discover_all(conn, config, dry_run=False)

    new_ids = [
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM videos WHERE state = ?", (VideoState.DISCOVERED.value,)
        ).fetchall()
    ]
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

    classify_counts = classify.classify_all(conn, config)
    upcoming_ids = [
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM videos WHERE state = ?",
            (VideoState.LIVE_UPCOMING.value,),
        ).fetchall()
    ]

    return discover_result, new_ids, classify_counts, upcoming_ids, quota_error, api_units


def cmd_seed(args) -> None:
    """Backfill: discover + metadata + classify, then force needs_transcript -> delivered.

    Never queues transcript fetches. This must run before the first real `run`.
    """
    config = _load_config(args)
    conn = _connect(config)

    discover_result, new_ids, classify_counts, _, quota_error, _ = _discover_metadata_classify(
        conn, config
    )
    if quota_error:
        print(f"Warning: {quota_error}", file=sys.stderr)

    now = utcnow_iso()
    cur = conn.execute(
        "UPDATE videos SET state = ?, updated_at = ? WHERE state = ?",
        (VideoState.DELIVERED.value, now, VideoState.NEEDS_TRANSCRIPT.value),
    )
    seeded = cur.rowcount
    conn.execute(
        "UPDATE videos SET state = ?, updated_at = ? WHERE state = ?",
        (VideoState.DELIVERED.value, now, VideoState.LIVE_FINISHED.value),
    )
    conn.commit()

    print(
        f"Seeded: {discover_result.new_videos} videos discovered, "
        f"{seeded} marked delivered (backfill, no transcripts fetched), "
        f"classify counts: {classify_counts}"
    )


def cmd_discover(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    result = discover.discover_all(conn, config, dry_run=args.dry_run)
    print(
        f"Polled {result.channels_polled} channel(s), {result.channels_failed} failed, "
        f"{result.new_videos} new video(s)"
    )
    for w in result.dead_channel_warnings:
        print(f"WARNING: {w}", file=sys.stderr)


# --------------------------------------------------------------------------------------
# Stage 2 phases, shared by `run` and the standalone commands
# --------------------------------------------------------------------------------------


def _run_transcript_phase(conn, config, limit=None) -> tuple[list[str], list[str]]:
    """Returns (notes, failed_permanent_ids_this_run). Mutates conn."""
    notes = []
    result = transcript_mod.run_transcript_phase(conn, config, limit=limit)
    if result.aborted:
        notes.append(f"transcript phase aborted: {result.abort_reason}")
    logger.info(
        "transcript phase: attempted=%d succeeded=%d failed_permanent=%d retrying=%d aborted=%s",
        result.attempted, len(result.succeeded_ids), len(result.failed_permanent_ids),
        result.retrying, result.aborted,
    )
    return notes, result.failed_permanent_ids


def _run_summarize_phase(conn, config) -> tuple[list[str], list[str]]:
    """Returns (notes, succeeded_ids)."""
    notes = []
    api_key = config.secrets.get("GEMINI_API_KEY")
    if not api_key:
        notes.append("GEMINI_API_KEY not set — cannot summarize")
        return notes, []
    result = summarize_mod.run_summarize_phase(conn, config, config.transcripts_dir, api_key)
    logger.info(
        "summarize phase: attempted=%d succeeded=%d failed=%d",
        result.attempted, len(result.succeeded_ids), len(result.failed_ids),
    )
    return notes, result.succeeded_ids


def _build_and_deliver_digest(conn, config, channel, upcoming_ids, failed_transcript_ids, notes, run_id):
    """Builds the digest from current DB state, writes the file, delivers it, and marks
    successfully-delivered videos as `delivered`. Returns (digest, delivery_error)."""
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
        conn, summarized_ids, upcoming_ids, failed_transcript_ids, notes,
        transcript_pending=transcript_pending,
    )
    digest_mod.write_digest_file(d, config.digests_dir)

    delivery_error = None
    try:
        if channel == "telegram":
            deliver_mod.deliver_telegram(
                d, config.secrets["TELEGRAM_BOT_TOKEN"], config.secrets["TELEGRAM_ALLOWED_CHAT_ID"],
                config, conn, run_id,
            )
        else:
            deliver_mod.deliver(d, channel, config)
    except Exception as exc:
        logger.exception("delivery failed")
        delivery_error = str(exc)

    if channel == "telegram":
        delivered_ids = {
            r["video_id"]
            for r in conn.execute("SELECT video_id FROM deliveries WHERE run_id = ?", (run_id,)).fetchall()
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


def cmd_run(args) -> None:
    config = _load_config(args)

    if args.dry_run:
        # Zero writes, zero outbound calls: don't touch the DB or network at all.
        print(
            "[dry-run] would run: discover -> metadata -> classify -> transcripts -> summarize "
            "-> deliver. Zero writes, zero outbound calls to YouTube Data API / Gemini / Telegram."
        )
        return

    conn = _connect(config)

    run_started = utcnow_iso()
    cur = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'ok')", (run_started,))
    run_id = cur.lastrowid
    conn.commit()

    channel = args.channel or config.values["delivery_channel"]
    status = "ok"
    notes = []
    try:
        (
            discover_result,
            new_ids,
            classify_counts,
            upcoming_ids,
            quota_error,
            api_units,
        ) = _discover_metadata_classify(conn, config)

        if discover_result.dead_channel_warnings:
            notes.extend(discover_result.dead_channel_warnings)
        if quota_error:
            notes.append(quota_error)
            status = "partial"

        transcript_notes, failed_transcript_ids = _run_transcript_phase(conn, config, limit=args.limit)
        if transcript_notes:
            notes.extend(transcript_notes)
            status = "partial"

        summarize_notes, _ = _run_summarize_phase(conn, config)
        if summarize_notes:
            notes.extend(summarize_notes)
            status = "partial"

        digest, delivery_error = _build_and_deliver_digest(
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
            deliver_mod.send_alert(
                config, f"run #{run_id} status={status}: {'; '.join(notes) if notes else 'see logs'}"
            )
    except Exception as exc:
        logger.exception("run failed")
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = 'error', notes = ? WHERE id = ?",
            (utcnow_iso(), str(exc), run_id),
        )
        conn.commit()
        deliver_mod.send_alert(config, f"run #{run_id} crashed: {exc}")
        raise


def cmd_fetch_transcripts(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    result = transcript_mod.run_transcript_phase(conn, config, limit=args.limit)
    print(
        f"Attempted {result.attempted}, succeeded {len(result.succeeded_ids)}, "
        f"failed_permanent {len(result.failed_permanent_ids)}, retrying {result.retrying}"
    )
    if result.aborted:
        print(f"WARNING: transcript phase aborted: {result.abort_reason}", file=sys.stderr)


def cmd_summarize(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    api_key = config.secrets.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    result = summarize_mod.run_summarize_phase(conn, config, config.transcripts_dir, api_key)
    print(f"Attempted {result.attempted}, succeeded {len(result.succeeded_ids)}, failed {len(result.failed_ids)}")


def cmd_deliver(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    channel = args.channel or config.values["delivery_channel"]

    cur = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'ok')", (utcnow_iso(),))
    run_id = cur.lastrowid
    conn.commit()

    upcoming_ids = [
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM videos WHERE state = ?", (VideoState.LIVE_UPCOMING.value,)
        ).fetchall()
    ]
    digest, delivery_error = _build_and_deliver_digest(conn, config, channel, upcoming_ids, [], [], run_id)
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ? WHERE id = ?",
        (utcnow_iso(), "error" if delivery_error else "ok", run_id),
    )
    conn.commit()
    if delivery_error:
        print(f"Error: {delivery_error}", file=sys.stderr)
        sys.exit(1)
    print(f"Delivered {len(digest.new_videos)} video(s) and {len(digest.live_announcements)} announcement(s)")


def cmd_retry(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    now = utcnow_iso()

    if args.all_failed:
        cur = conn.execute(
            """
            UPDATE videos
            SET state = ?, attempts = 0, next_retry_at = NULL, last_error = NULL, updated_at = ?
            WHERE state = ?
            """,
            (VideoState.NEEDS_TRANSCRIPT.value, now, VideoState.FAILED_PERMANENT.value),
        )
        conn.commit()
        print(f"Reset {cur.rowcount} failed video(s) for retry")
        return

    if not args.video_id:
        print("Error: provide a video_id or --all-failed", file=sys.stderr)
        sys.exit(1)

    row = conn.execute("SELECT state FROM videos WHERE video_id = ?", (args.video_id,)).fetchone()
    if row is None:
        print(f"Error: unknown video_id {args.video_id!r}", file=sys.stderr)
        sys.exit(1)
    if row["state"] != VideoState.FAILED_PERMANENT.value:
        print(f"Error: video is in state {row['state']!r}, not failed_permanent", file=sys.stderr)
        sys.exit(1)

    conn.execute(
        """
        UPDATE videos
        SET state = ?, attempts = 0, next_retry_at = NULL, last_error = NULL, updated_at = ?
        WHERE video_id = ?
        """,
        (VideoState.NEEDS_TRANSCRIPT.value, now, args.video_id),
    )
    conn.commit()
    print(f"Reset {args.video_id} for retry")


def cmd_export(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    row = conn.execute(
        """
        SELECT v.*, c.title AS channel_title FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.video_id = ?
        """,
        (args.video_id,),
    ).fetchone()
    if row is None:
        print(f"Error: unknown video_id {args.video_id!r}", file=sys.stderr)
        sys.exit(1)

    txt_path = config.transcripts_dir / row["channel_id"] / f"{row['video_id']}.txt"
    if not txt_path.exists():
        print(f"Error: no transcript on disk for {args.video_id} ({txt_path})", file=sys.stderr)
        sys.exit(1)
    transcript_text = txt_path.read_text(encoding="utf-8")

    if args.format == "txt":
        print(transcript_text)
    else:
        print(f"# {row['title']}\n")
        print(f"**Channel:** {row['channel_title']}  ")
        print(f"**Published:** {row['published_at']}  ")
        print(f"**Link:** https://youtu.be/{row['video_id']}\n")
        if row["summary"]:
            print(f"## Summary\n\n{row['summary']}\n")
        print(f"## Transcript\n\n{transcript_text}")


def cmd_status(args) -> None:
    config = _load_config(args)
    conn = _connect(config)

    print(f"Database: {config.db_path}")
    print()
    print("Videos by state:")
    for row in conn.execute("SELECT state, COUNT(*) AS n FROM videos GROUP BY state ORDER BY n DESC"):
        print(f"  {row['state']:<20} {row['n']}")

    print()
    print("Channels:")
    total = conn.execute("SELECT COUNT(*) AS n FROM channels").fetchone()["n"]
    enabled = conn.execute("SELECT COUNT(*) AS n FROM channels WHERE enabled = 1").fetchone()["n"]
    errored = conn.execute(
        "SELECT channel_id, title, consecutive_errors FROM channels WHERE consecutive_errors > 0"
    ).fetchall()
    print(f"  {enabled}/{total} enabled")
    for row in errored:
        print(f"  WARNING: {row['title'] or row['channel_id']} — {row['consecutive_errors']} consecutive errors")

    print()
    last_run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if last_run:
        print(
            f"Last run: #{last_run['id']} started {last_run['started_at']} "
            f"status={last_run['status']} discovered={last_run['discovered']} api_units={last_run['api_units']}"
        )
    else:
        print("Last run: none yet")

    pending_retry = conn.execute(
        "SELECT COUNT(*) AS n FROM videos WHERE next_retry_at IS NOT NULL"
    ).fetchone()["n"]
    print(f"Pending retries: {pending_retry}")


def _not_implemented(stage: str):
    def handler(args):
        print(f"Not implemented yet — this command lands in {stage}.", file=sys.stderr)
        sys.exit(2)

    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ytdigest")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create the database and tables")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("add-channel", help="add a single channel by URL/@handle/UC id")
    p.add_argument("channel")
    p.set_defaults(func=cmd_add_channel)

    p = sub.add_parser("import-channels", help="import channels from a Takeout CSV or list file")
    p.add_argument("file")
    p.set_defaults(func=cmd_import_channels)

    p = sub.add_parser("seed", help="backfill existing videos without fetching transcripts")
    p.add_argument("--since", required=True, help="YYYY-MM-DD (recorded for operator reference)")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser(
        "run", help="run the full pipeline: discover -> metadata -> classify -> transcripts -> summarize -> deliver"
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="cap on transcript fetches this run")
    p.add_argument("--channel", choices=["telegram", "stdout", "file"], default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("discover", help="run only the discovery phase")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("fetch-transcripts", help="run only the transcript-fetching phase")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_fetch_transcripts)

    p = sub.add_parser("summarize", help="run only the summarization phase")
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("deliver", help="build and deliver the digest from current DB state")
    p.add_argument("--channel", choices=["telegram", "stdout", "file"], default=None)
    p.set_defaults(func=cmd_deliver)

    p = sub.add_parser("retry", help="reset a failed_permanent video (or all of them) for retry")
    p.add_argument("video_id", nargs="?", default=None)
    p.add_argument("--all-failed", action="store_true")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("export", help="print a video's transcript (and summary, if any)")
    p.add_argument("video_id")
    p.add_argument("--format", choices=["txt", "md"], default="txt")
    p.set_defaults(func=cmd_export)

    for name, stage in [("ask", "Stage 3"), ("bot", "Stage 3")]:
        p = sub.add_parser(name, help=f"({stage}, not yet implemented)")
        p.add_argument("args", nargs="*")
        p.set_defaults(func=_not_implemented(stage))

    p = sub.add_parser("status", help="show counts by state, last run, quota, pending retries")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> None:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
