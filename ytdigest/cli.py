"""ytdigest CLI. Stage 1: init-db, add-channel, import-channels, seed, run, discover, status."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import classify, db, deliver as deliver_mod, digest as digest_mod, discover, metadata
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


def _discover_metadata_classify(conn, config):
    """Shared discover -> metadata -> classify pipeline. Returns (discover_result, new_ids, classify_counts, newly_announced, quota_error, api_units)."""
    discover_result = discover.discover_all(conn, config, dry_run=False)

    new_ids = [
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM videos WHERE state = ?", (VideoState.DISCOVERED.value,)
        ).fetchall()
    ]

    quota_error = None
    api_units = 0
    api_key = config.secrets.get("YOUTUBE_API_KEY")
    if new_ids:
        if not api_key:
            quota_error = "YOUTUBE_API_KEY not set — cannot fetch metadata"
        else:
            try:
                items, missing, api_units = metadata.fetch_all_metadata(
                    new_ids,
                    api_key,
                    quota_daily=config.values["youtube_api_quota_daily"],
                    quota_warn_fraction=config.values["youtube_api_quota_warn_fraction"],
                )
                metadata.apply_metadata(conn, items, missing)
            except metadata.QuotaExceededError as exc:
                quota_error = str(exc)

    classify_counts = classify.classify_all(conn, config)
    newly_announced = [
        r["video_id"]
            for r in conn.execute(
                "SELECT video_id FROM videos WHERE state = ? AND announced_at IS NOT NULL",
                (VideoState.LIVE_UPCOMING.value,),
            ).fetchall()
        ]

    return discover_result, new_ids, classify_counts, newly_announced, quota_error, api_units


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


def cmd_run(args) -> None:
    config = _load_config(args)

    if args.dry_run:
        # Zero writes, zero outbound calls: don't touch the DB or network at all.
        print(
            "[dry-run] would run: discover -> metadata -> classify -> deliver. "
            "Zero writes, zero outbound calls to YouTube Data API / Gemini / Telegram."
        )
        return

    conn = _connect(config)

    run_started = utcnow_iso()
    cur = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'ok')", (run_started,))
    run_id = cur.lastrowid
    conn.commit()

    status = "ok"
    notes = []
    try:
        (
            discover_result,
            new_ids,
            classify_counts,
            newly_announced,
            quota_error,
            api_units,
        ) = _discover_metadata_classify(conn, config)

        if discover_result.dead_channel_warnings:
            notes.extend(discover_result.dead_channel_warnings)
        if quota_error:
            notes.append(quota_error)
            status = "partial"

        d = digest_mod.build_digest(conn, new_ids, newly_announced, notes)
        digest_mod.write_digest_file(d, config.digests_dir)
        deliver_mod.deliver(d, args.channel or config.values["delivery_channel"], config)

        conn.execute(
            """
            UPDATE runs SET finished_at = ?, discovered = ?, api_units = ?, status = ?, notes = ?
            WHERE id = ?
            """,
            (
                utcnow_iso(),
                discover_result.new_videos,
                api_units,
                status,
                "; ".join(notes) if notes else None,
                run_id,
            ),
        )
        conn.commit()
    except Exception as exc:
        logger.exception("run failed")
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = 'error', notes = ? WHERE id = ?",
            (utcnow_iso(), str(exc), run_id),
        )
        conn.commit()
        raise


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

    p = sub.add_parser("run", help="run the full pipeline: discover -> metadata -> classify -> deliver")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="reserved for Stage 2 (transcript cap)")
    p.add_argument("--channel", choices=["telegram", "stdout", "file"], default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("discover", help="run only the discovery phase")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_discover)

    for name, stage in [
        ("fetch-transcripts", "Stage 2"),
        ("summarize", "Stage 2"),
        ("deliver", "Stage 2"),
        ("retry", "Stage 2"),
        ("ask", "Stage 3"),
        ("export", "Stage 2"),
        ("bot", "Stage 3"),
    ]:
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
