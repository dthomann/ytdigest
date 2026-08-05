#!/usr/bin/env bash
# Push code updates to the Pi. Skips venv, data, .env, and config.yaml.
#
# Usage (from anywhere):
#   scripts/deploy.sh
#
# Override defaults via environment:
#   YTDIGEST_PI=example.local          SSH host (from ~/.ssh/config)
#   YTDIGEST_REMOTE=~/ytdigest      install dir on the Pi
#   YTDIGEST_PIP=1                    re-run pip install (only when requirements.txt changed)
#   YTDIGEST_RESTART_WEB=0          skip systemctl restart
set -euo pipefail

PI="${YTDIGEST_PI:-example.local}"
REMOTE="${YTDIGEST_REMOTE:-~/ytdigest}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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
  ssh "$PI" 'sudo systemctl restart ytdigest-web'
fi

echo "Deploy complete: $(date -Iseconds)"
