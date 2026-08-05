"""ytdigest CLI.

Stage 1: init-db, add-channel, import-channels, seed, run, discover, status.
Stage 2: fetch-transcripts, summarize, deliver, retry, export.
Stage 3 (not yet implemented): ask, bot.
Web UI: web.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import classify, db, deliver as deliver_mod, digest as digest_mod, discover, metadata
from . import summarize as summarize_mod, transcript as transcript_mod
from . import channels as channels_mod, pipeline
from .config import Config, ConfigError, load_config
from .models import VideoState
from .run_lock import RunInProgressError
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
    channels_mod.add_channel(
        conn, resolved.channel_id, title=resolved.title, handle=resolved.handle, source="manual"
    )
    print(f"Added channel {resolved.channel_id} ({resolved.title or 'unknown title'})")


def cmd_import_channels(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    try:
        resolved = resolve_file(args.file, api_key=config.secrets.get("YOUTUBE_API_KEY") or None)
    except (ValueError, FileNotFoundError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    added = channels_mod.import_channels(conn, resolved, source="import")
    print(f"Imported {added} new channel(s) ({len(resolved)} total in file)")


def cmd_enable_channel(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    if not channels_mod.set_enabled(conn, args.channel_id, True):
        print(f"Error: unknown channel {args.channel_id!r}", file=sys.stderr)
        sys.exit(1)
    print(f"Enabled {args.channel_id}")


def cmd_disable_channel(args) -> None:
    config = _load_config(args)
    conn = _connect(config)
    if not channels_mod.set_enabled(conn, args.channel_id, False):
        print(f"Error: unknown channel {args.channel_id!r}", file=sys.stderr)
        sys.exit(1)
    print(f"Disabled {args.channel_id}")


def _discover_metadata_classify(conn, config):
    return pipeline.discover_metadata_classify(conn, config)


def cmd_seed(args) -> None:
    config = _load_config(args)
    conn = _connect(config)

    discover_result, _, quota_error, _ = _discover_metadata_classify(conn, config)
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

    print(f"Seeded: {discover_result.new_videos} videos discovered, {seeded} marked delivered (backfill)")


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


def cmd_run(args) -> None:
    config = _load_config(args)

    if args.dry_run:
        print(
            "[dry-run] would run: discover -> metadata -> classify -> transcripts -> summarize "
            "-> deliver. Zero writes, zero outbound calls to YouTube Data API / Gemini / Telegram."
        )
        return

    conn = _connect(config)
    try:
        result = pipeline.run_pipeline(conn, config, limit=args.limit, channel=args.channel)
        print(
            f"Run #{result.run_id} finished status={result.status} "
            f"discovered={result.discovered} summarized={result.summarized}"
        )
    except RunInProgressError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


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
    digest, delivery_error = pipeline.build_and_deliver_digest(
        conn, config, channel, upcoming_ids, [], [], run_id
    )
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
        """
        SELECT COUNT(*) AS n FROM videos
        WHERE state = ? AND next_retry_at IS NOT NULL
        """,
        (VideoState.NEEDS_TRANSCRIPT.value,),
    ).fetchone()["n"]
    print(f"Pending retries: {pending_retry}")


def cmd_web(args) -> None:
    import uvicorn

    config = _load_config(args)
    from .web.app import create_app

    app = create_app(config)
    uvicorn.run(
        app,
        host=config.values["web_host"],
        port=config.values["web_port"],
        log_level="info",
    )


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

    p = sub.add_parser("enable-channel", help="enable a channel")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_enable_channel)

    p = sub.add_parser("disable-channel", help="disable a channel")
    p.add_argument("channel_id")
    p.set_defaults(func=cmd_disable_channel)

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

    p = sub.add_parser("web", help="start the web UI")
    p.set_defaults(func=cmd_web)

    return parser


def main(argv: list[str] | None = None) -> None:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
