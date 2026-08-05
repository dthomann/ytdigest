#!/usr/bin/env bash
# Backs up the ytdigest database and transcript archive to a remote host via rsync.
# Idempotent and safe to run while the main job is running (uses sqlite3 .backup, not a raw copy).
#
# Configure via environment or edit the defaults below, then schedule with cron/systemd timer.
set -euo pipefail

DATA_DIR="${YTDIGEST_DATA_DIR:-/opt/ytdigest/data}"
REMOTE="${YTDIGEST_BACKUP_REMOTE:-}"   # e.g. user@host:/path/to/backups/ytdigest/
TMP_DB="$(mktemp /tmp/ytdigest-backup-XXXXXX.db)"
trap 'rm -f "$TMP_DB"' EXIT

if [ -z "$REMOTE" ]; then
  echo "Set YTDIGEST_BACKUP_REMOTE (e.g. user@host:/path/) before running this script." >&2
  exit 1
fi

sqlite3 "$DATA_DIR/ytdigest.db" ".backup '$TMP_DB'"

rsync -az "$TMP_DB" "$REMOTE/ytdigest.db"
rsync -az "$DATA_DIR/transcripts/" "$REMOTE/transcripts/"

echo "Backup complete: $(date -Iseconds)"
