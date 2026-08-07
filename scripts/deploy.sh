#!/usr/bin/env bash
# Push code updates to the Pi. Skips venv, data, .env, and config.yaml.
#
# Usage (from anywhere):
#   scripts/deploy.sh
#
# Optional overrides (env var or repo-root .env):
#   YTDIGEST_PI=pi@192.168.1.100    SSH host (user@ip or ~/.ssh/config alias)
#   YTDIGEST_REMOTE=~/ytdigest      install dir on the Pi
#   YTDIGEST_PIP=1                    re-run pip install (only when requirements.txt changed)
#   YTDIGEST_RESTART_WEB=0          skip systemctl restart
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
_saved_pi="${YTDIGEST_PI:-}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

if [[ -n "$_saved_pi" ]]; then
  YTDIGEST_PI="$_saved_pi"
fi

PI="${YTDIGEST_PI:?Set YTDIGEST_PI in .env or the environment (e.g. pi@192.168.1.100)}"
REMOTE="${YTDIGEST_REMOTE:-~/ytdigest}"

RSYNC_EXCLUDES=(
  --exclude 'venv/'
  --exclude '.venv/'
  --exclude 'data/'
  --exclude '.env'
  --exclude 'config.yaml'
  --exclude '.git/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '.DS_Store'
  --exclude '*.egg-info/'
)

echo "Deploying ${ROOT}/ -> ${PI}:${REMOTE}/"

if [[ "${YTDIGEST_DEDICATED:-}" == 1 ]]; then
  rsync -avz "${RSYNC_EXCLUDES[@]}" "$ROOT/" "$PI:/tmp/ytdigest-deploy/"
  ssh "$PI" "sudo rsync -a /tmp/ytdigest-deploy/ ${REMOTE}/ && sudo chown -R ytdigest:ytdigest ${REMOTE}"
else
  rsync -avz "${RSYNC_EXCLUDES[@]}" "$ROOT/" "$PI:${REMOTE}/"
fi

if [[ "${YTDIGEST_PIP:-}" == 1 ]]; then
  echo "Reinstalling dependencies..."
  ssh "$PI" "${REMOTE}/venv/bin/pip install -r ${REMOTE}/requirements.txt"
fi

if [[ "${YTDIGEST_RESTART_WEB:-1}" == 1 ]]; then
  echo "Restarting ytdigest-web..."
  # Uses passwordless sudo for `ytdigest services *` (see systemd/ytdigest-sudoers.example).
  ssh "$PI" "sudo -n ${REMOTE}/venv/bin/ytdigest --config ${REMOTE}/config.yaml services restart-web"
fi

echo "Deploy complete: $(date -Iseconds)"
