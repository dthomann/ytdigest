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
#   YTDIGEST_RESTART_WEB=0          skip service restarts after deploy
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
_saved_pi="${YTDIGEST_PI:-}"

# Load only YTDIGEST_* from .env (don't `source` the whole file — API keys often
# use KEY= value spacing that bash interprets as a command).
if [[ -f "$ROOT/.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    if [[ "$line" =~ ^[[:space:]]*(YTDIGEST_[A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)[[:space:]]*$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
      fi
    fi
  done < "$ROOT/.env"
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
  echo "Restarting ytdigest services..."
  # Passwordless sudo for the exact ytdigest path (see systemd/ytdigest-sudoers.example).
  # Sudoers user must match your SSH login — not necessarily "pi".
  ssh "$PI" "d=${REMOTE}; d=\${d/#\\~/\$HOME}; y=\${d}/venv/bin/ytdigest; c=\${d}/config.yaml; sudo -n \"\$y\" --config \"\$c\" services restart-web; sudo -n \"\$y\" --config \"\$c\" services restart-bot"
fi

echo "Deploy complete: $(date -Iseconds)"
