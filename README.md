# ytdigest

Self-hosted daily YouTube digest for a Raspberry Pi (or any Linux box): watches your subscribed
channels, fetches transcripts for new long-form videos, summarizes each in one paragraph, and
delivers a digest to Telegram — with follow-up Q&A per video.

## Features

- RSS discovery with YouTube Data API metadata and Shorts/livestream classification
- Three-tier transcript chain: captions API → yt-dlp → optional Groq Whisper fallback
- Gemini summarization with map-reduce for long videos
- Telegram delivery (one message per video) with MarkdownV2 formatting
- Reply-based Q&A grounded in timestamped transcripts, with `?t=` deep links
- Optional LAN web UI for browsing digests, managing channels, and triggering runs
- Offline test suite with recorded fixtures

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # installs the ytdigest package (editable) + all dependencies
```

Requires Python 3.11+. All dependencies are pure-Python or have ARM64 wheels — nothing x86-only.

## First run

```bash
cp config.example.yaml config.yaml     # edit if you want different defaults
cp .env.example .env && chmod 600 .env # fill in YOUTUBE_API_KEY at minimum
ytdigest init-db
ytdigest import-channels subscriptions.csv   # Takeout export, or a newline list of URLs/@handles
ytdigest seed --since $(date -I)             # REQUIRED before the first real `run` — see below
ytdigest status
```

**`seed` is not optional.** Without it, the first `run` sees your entire back-catalogue (RSS gives
the last 15 uploads per channel — up to ~900 videos for 60 channels) and would try to fetch
transcripts for all of them in one burst. That is the single most reliable way to get a residential
IP throttled by YouTube. `seed` discovers and classifies everything currently visible but forces
would-be `needs_transcript` videos straight to `delivered` (backfilled, not summarized) and skips
the transcript layer entirely. After seeding, `status` should show zero `needs_transcript` rows.

Then:

```bash
ytdigest run --dry-run     # zero writes, zero outbound calls — safe to run anytime
ytdigest run --limit 3     # first real run: cap transcript fetches to 3, inspect the output
ytdigest run               # full pipeline: discover -> metadata -> classify -> transcripts ->
                            # summarize -> deliver
```

The transcript layer is the most IP-sensitive part of the pipeline. Run `ytdigest run --limit 3`
first and inspect the stored transcripts in `data/transcripts/` — check that overlap dedup worked
(no duplicated phrases) and the character count is plausible for the video's length. Only then run
without `--limit`.

Set `delivery_channel: telegram` in `config.yaml` (and fill in `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_ALLOWED_CHAT_ID` in `.env`) once you're ready to move off `stdout`/`file`. To fetch a
chat ID: message your bot once (a bot can't message you first), then
`curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"`.

For Q&A, also set `GEMINI_API_KEY` and start the bot (foreground or via systemd):

```bash
ytdigest ask <video_id> "What did they say about pricing?"
ytdigest bot    # long-polling listener — reply to digest messages or use /ask
```

## CLI

| Command | Notes |
|---|---|
| `init-db` | Creates `data/ytdigest.db` (WAL mode) |
| `add-channel <url\|@handle\|UC…>` | Resolving a handle needs `YOUTUBE_API_KEY` (one API call) |
| `import-channels <file>` | Takeout CSV or newline list; UC ids need no network |
| `seed --since YYYY-MM-DD` | Backfill — run once before the first real `run` |
| `run [--dry-run] [--limit N] [--channel telegram\|stdout\|file]` | Full pipeline; `--dry-run` touches nothing; `--limit` caps transcript fetches this run |
| `discover [--dry-run]` | Discovery phase only |
| `fetch-transcripts [--limit N]` | Transcript phase only (3-tier chain, retry/backoff) |
| `summarize` | Summarize every video currently in `has_transcript` |
| `deliver [--channel telegram\|stdout\|file]` | Build + send the digest from current DB state (no discover/transcript/summarize) |
| `retry <video_id> \| --all-failed` | Reset `failed_permanent` video(s) back to `needs_transcript` |
| `export <video_id> [--format txt\|md]` | Print a video's transcript, with summary/metadata in `md` mode |
| `status` | Counts by state, last run, pending retries, channel errors |
| `ask <video_id> <question>` | Ask a follow-up question (uses `.jsonl` transcript + Gemini) |
| `bot` | Long-polling Telegram Q&A bot (`/status`, `/last`, `/channels`, `/retry`, `/ask`) |
| `web` | Start the LAN web UI (digest browser, channels, sync, run now) |
| `enable-channel`, `disable-channel` | Toggle a channel on/off |

## Configuration reference (`config.yaml`)

Secrets never go here — see `.env.example`. Unknown keys and any key that looks like a secret
(containing `key`/`token`/`secret`/`password`) are rejected at load time.

| Key | Default | Why |
|---|---|---|
| `data_dir` | `data` | Root for db/transcripts/digests. Relative paths resolve against `config.yaml`'s directory. |
| `timezone` | `UTC` | Used for local-time display and the systemd timer. |
| `digest_hour` | `6` | Intended local hour for the daily run; the systemd timer reads this indirectly (set `OnCalendar` to match). |
| `rss_delay_seconds` | `[1, 2]` | Jittered sleep between RSS polls — protects the residential IP. |
| `max_channel_consecutive_errors` | `10` | After this many consecutive poll failures, warn in the digest (likely a dead/renamed channel). |
| `min_duration_seconds` | `180` | Videos at/under this length are classified as Shorts and skipped. |
| `shorts_probe` | `false` | HEAD-probe `/shorts/{id}` for the 60–180s band to disambiguate real Shorts from short normal videos. Adds a request per ambiguous video; off by default. |
| `summarize_finished_livestreams` | `false` | Never summarize livestreams by default. Flip to `true` to route finished streams into the transcript queue instead of the terminal `live_finished` state. |
| `youtube_api_quota_daily` | `10000` | YouTube Data API's free daily unit quota. |
| `youtube_api_quota_warn_fraction` | `0.9` | Abort the run (loudly) before crossing this fraction of daily quota, rather than failing opaquely at the hard limit. |
| `transcript_languages` | `[en]` | Caption language preference order: manual in these languages, then auto-generated in these languages, then any available track. |
| `transcript_delay_seconds` | `[2, 5]` | Jittered sleep between transcript fetches — the most IP-sensitive phase. |
| `max_transcript_fetches_per_run` | `40` | Hard cap per run; excess stays queued for next time. Override per invocation with `run --limit N`. |
| `enable_whisper_fallback` | `false` | Tier-3 ASR fallback via Groq's remote API (never local). Requires `GROQ_API_KEY`. |
| `whisper_max_duration_minutes` | `120` | Skip ASR fallback beyond this length (cost/time control). |
| `retry_backoff_hours` | `[6, 12, 24, 48, 96]` | Auto-captions are often missing for hours after upload — this is a retryable, not fatal, condition. |
| `max_transcript_attempts` | `5` | After this many retries, a video becomes `failed_permanent` (still listed once in the digest so nothing silently vanishes). |
| `summary_model` | `gemini-2.5-flash-lite` | Cheap, fast, sufficient for one-paragraph summaries. |
| `summary_mode` | `sync` | `batch` is documented (50% cheaper, up to 24h latency) but not implemented. |
| `summary_words` | `[60, 100]` | Target summary length. |
| `output_language` | `en` | All summaries are produced in this language regardless of the source video's language. |
| `max_input_chars` | `400000` | Above this, map-reduce chunking kicks in instead of a single call. |
| `delivery_channel` | `stdout` | `telegram` \| `stdout` \| `file`. `stdout` is the development default; switch to `telegram` once `.env` has bot credentials. |
| `telegram_message_delay_seconds` | `1` | Per-chat rate limit between digest messages. |
| `web_host` | `0.0.0.0` | Web UI bind address. Use `0.0.0.0` for LAN access; browse via `http://<host-lan-ip>:8080`. |
| `web_port` | `8080` | Web UI port. |
| `web_public_url` | `null` | Optional full URL (e.g. `http://192.168.1.100:8080`) for stable YouTube OAuth redirect URI. |

## Secrets (`.env`, chmod 600)

`YOUTUBE_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_CHAT_ID`, `YOUTUBE_OAUTH_CLIENT_ID`, `YOUTUBE_OAUTH_CLIENT_SECRET` — see
`.env.example`. `YOUTUBE_API_KEY` is required for metadata lookups (and for resolving @handles).
`GEMINI_API_KEY` is required to summarize — without it, `run` and `summarize` skip that phase, log
a note, and the video stays queued at `has_transcript` for next time. `GROQ_API_KEY` is only needed
if `enable_whisper_fallback: true`. `delivery_channel: telegram` requires the two Telegram
variables; failure alerts also use them regardless of the configured delivery channel. The config
loader fails loudly and immediately if a required secret is missing.

## Production setup (Raspberry Pi)

Throughout this section, `$YTDIGEST_HOME` is wherever you installed the app.

### Pi baseline

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-pip sqlite3 ffmpeg git rsync
python3 --version          # need 3.11+
free -h                    # confirm headroom
df -h                      # confirm free space
```

`ffmpeg` is only needed for the rare Whisper fallback, but installing it now avoids a confusing
failure later.

### Data directory

The SD card will hold this fine — ~450 transcripts/month at ~100 KB is roughly 0.5 GB/year. The
concern is write wear, not capacity.

- **Do nothing** if you accept the (small) risk and take backups. WAL mode keeps writes modest.
- **Better:** plug in any spare USB stick or SSD, format ext4, mount at `/mnt/ytdata`, add to
  `/etc/fstab` with `noatime`. Point the tool's `data_dir` there.

Either way, backups (below) are not optional. SD cards fail without warning and you would lose the
transcript archive, not just the state.

### Install location

Pick one approach. **Home setup** is the default for a single-user Pi — simpler deploy, no extra
accounts. **Dedicated user** is optional if you want process isolation from your login.

| | Home setup (recommended) | Dedicated user (optional) |
|---|---|---|
| Install dir (`$YTDIGEST_HOME`) | `~/ytdigest` | `/opt/ytdigest` |
| Run as | your SSH user (`pi`, etc.) | `ytdigest` system user |
| Deploy from dev machine | `rsync` straight into `~/ytdigest` | rsync to `/tmp`, then `sudo cp` + `chown` |

**Home setup** — on the Pi:

```bash
export YTDIGEST_HOME=~/ytdigest
mkdir -p "$YTDIGEST_HOME"
git clone <repo-url> "$YTDIGEST_HOME"
python3 -m venv "$YTDIGEST_HOME/venv"
"$YTDIGEST_HOME/venv/bin/pip" install -r "$YTDIGEST_HOME/requirements.txt"
cp "$YTDIGEST_HOME/config.example.yaml" "$YTDIGEST_HOME/config.yaml"
```

**Dedicated user** — only if you want isolation:

```bash
export YTDIGEST_HOME=/opt/ytdigest
sudo useradd -r -m -d "$YTDIGEST_HOME" -s /usr/sbin/nologin ytdigest
sudo mkdir -p "$YTDIGEST_HOME" && sudo chown ytdigest:ytdigest "$YTDIGEST_HOME"
# deploy code into $YTDIGEST_HOME, then:
sudo -u ytdigest python3 -m venv "$YTDIGEST_HOME/venv"
sudo -u ytdigest "$YTDIGEST_HOME/venv/bin/pip" install -r "$YTDIGEST_HOME/requirements.txt"
sudo -u ytdigest cp "$YTDIGEST_HOME/config.example.yaml" "$YTDIGEST_HOME/config.yaml"
```

The shipped systemd units assume `/opt/ytdigest` and `User=ytdigest`. For home setup, patch them
after copying to `/etc/systemd/system/`:

```bash
PI_USER=pi   # replace with your SSH username if different
YTDIGEST_HOME=/home/$PI_USER/ytdigest
sudo sed -i "s|User=ytdigest|User=$PI_USER|; s|/opt/ytdigest|$YTDIGEST_HOME|g" \
  /etc/systemd/system/ytdigest*.service
```

### API keys and accounts

**YouTube Data API key**

1. https://console.cloud.google.com → create a project (e.g. `ytdigest`)
2. **APIs & Services → Library** → search "YouTube Data API v3" → **Enable**
3. **Credentials → Create credentials → API key**
4. Click the key → **API restrictions → Restrict key → YouTube Data API v3**. Leave application
   restrictions unset if the host's IP is dynamic.

Free quota is 10 000 units/day. Typical usage is ~65 units/day. No billing account needed.

**Gemini API key**

https://aistudio.google.com/apikey → **Create API key** → attach to the same Cloud project.

There is a free tier with rate limits that may cover you outright. If you enable billing, expected
spend is roughly $0.30–0.50/month for a daily digest. Set a budget alert in Cloud Console anyway.

**Groq API key (optional — ASR fallback only)**

https://console.groq.com → API Keys. Used only when a video has no captions at all. Skip this if
you prefer, and set `enable_whisper_fallback: false`.

**Telegram bot + your chat ID**

1. Message **@BotFather** in Telegram → `/newbot` → pick a name and a username ending in `bot`
   → copy the token.
2. Send your new bot any message — a bot cannot message you first.
3. Get your chat ID:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Look for `"chat": {"id": 12345678, ...}`. That number is `TELEGRAM_ALLOWED_CHAT_ID`.

**Channel IDs**

You need canonical `UC…` IDs, not handles.

**Google Takeout (recommended for many channels):**
https://takeout.google.com → **Deselect all** → select **YouTube and YouTube Music** → click
*All YouTube data included* → keep only **subscriptions** → export. You get a CSV with a
`Channel Id` column. Feed it straight to `ytdigest import-channels`.

**Per-channel fallback:** open the channel page, View Source, search for `"channelId":"UC` — or use
`https://www.youtube.com/@handle/about` and read the ID from the share dialog.

**Put secrets in place**

Home setup:

```bash
tee ~/ytdigest/.env >/dev/null <<'EOF'
YOUTUBE_API_KEY=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_ID=...
EOF
chmod 600 ~/ytdigest/.env
```

Dedicated user:

```bash
sudo -u ytdigest tee /opt/ytdigest/.env >/dev/null <<'EOF'
YOUTUBE_API_KEY=...
GEMINI_API_KEY=...
GROQ_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_CHAT_ID=...
EOF
sudo chmod 600 /opt/ytdigest/.env
sudo chown ytdigest:ytdigest /opt/ytdigest/.env
```

Confirm `.env` is in `.gitignore`. Do not rsync your local `.env` to the Pi — create it on the Pi
directly so secrets stay on the machine that runs the job.

**DNS sanity check**

If you run a local DNS blocker (Pi-hole, AdGuard, etc.), verify from the host itself:

```bash
dig +short www.youtube.com
dig +short www.googleapis.com
curl -sI https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw | head -1
```

Anything resolving to `0.0.0.0` is being blocked locally. Whitelist the affected domains.

### Deploy code updates

For routine updates, run from the repo on your dev machine:

```bash
scripts/deploy.sh
```

Set `YTDIGEST_PI` in `.env` (or export it) to your Pi's SSH host — `pi@192.168.1.100`, a LAN
hostname, or an entry from `~/.ssh/config`. The script skips
`venv/`, `data/`, `.env`, and `config.yaml`. Restarts `ytdigest-web` when done. Set
`YTDIGEST_PIP=1` if `requirements.txt` changed.

Manual rsync (same thing the script does):

```bash
PI=pi@192.168.1.100
YTDIGEST_HOME=~/ytdigest

rsync -avz \
  --exclude 'venv/' --exclude 'data/' --exclude '.env' --exclude 'config.yaml' \
  --exclude '.git/' --exclude '__pycache__/' --exclude '.pytest_cache/' \
  --exclude '.DS_Store' \
  ./ "$PI:$YTDIGEST_HOME/"
```

Re-run `pip install -r requirements.txt` only when dependencies change. The package is installed
editable (`-e .`), so Python code updates take effect without reinstall.

After deploying, restart the web service if you use it:

```bash
ssh "$PI" 'sudo systemctl restart ytdigest-web'
```

The daily timer picks up code changes on its next run; trigger manually with
`sudo systemctl start ytdigest.service`.

### Seed on the Pi

```bash
~/ytdigest/venv/bin/ytdigest --config ~/ytdigest/config.yaml init-db
~/ytdigest/venv/bin/ytdigest --config ~/ytdigest/config.yaml import-channels subscriptions.csv
~/ytdigest/venv/bin/ytdigest --config ~/ytdigest/config.yaml seed --since $(date -I)
~/ytdigest/venv/bin/ytdigest --config ~/ytdigest/config.yaml status
```

**Do not skip seeding.** `status` should show a large `skipped`/`delivered` count and **zero**
`needs_transcript`.

### systemd

Copy the units, patch paths if you chose home setup, then enable:

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
# home setup only — patch User and paths (see above)
sudo systemctl daemon-reload
sudo systemctl enable --now ytdigest.timer
systemctl list-timers ytdigest.timer          # confirm next run time
journalctl -u ytdigest -f                     # watch a manual run
sudo systemctl start ytdigest.service         # trigger one now
```

Start the bot service when ready for Q&A:
`sudo systemctl enable --now ytdigest-bot.service`

### Web UI (optional)

The web UI runs on your home LAN and provides digest browsing, channel management, YouTube
subscription sync, and a "Run now" button.

1. Find your host's LAN IP: `hostname -I`
2. Optionally set a **static DHCP reservation** so the IP stays stable
3. Add to `config.yaml`:
   ```yaml
   web_host: 0.0.0.0
   web_port: 8080
   web_public_url: http://192.168.1.100:8080   # for OAuth redirect URI
   ```
4. For YouTube subscription sync, create OAuth credentials in Google Cloud Console:
   - **Credentials → Create credentials → OAuth client ID → Web application**
   - Authorized redirect URI: `http://192.168.1.100:8080/auth/youtube/callback`
   - Add `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET` to `.env`
5. Install and start:
   ```bash
   sudo cp systemd/ytdigest-web.service /etc/systemd/system/
   # home setup only — patch User and paths
   sudo systemctl daemon-reload
   sudo systemctl enable --now ytdigest-web
   ```
6. Open `http://192.168.1.100:8080` from any device on your LAN

`web_host: 0.0.0.0` means listen on all interfaces — you still access it via the LAN IP. It does
not expose the UI to the internet (your router's NAT blocks inbound traffic).

### Backups

Set up an SSH key from the Pi to a remote host, then schedule `scripts/backup.sh` weekly (its own
timer or a cron line). Verify once by restoring the DB copy elsewhere and running `ytdigest status`
against it. An untested backup is not a backup.

```bash
YTDIGEST_DATA_DIR=~/ytdigest/data YTDIGEST_BACKUP_REMOTE=user@host:/path/ scripts/backup.sh
```

### Maintenance

| Cadence | Task | Why |
|---|---|---|
| Monthly | `pip install -U yt-dlp` inside the venv | yt-dlp breaks whenever YouTube changes something. It is the most update-sensitive dependency. |
| Monthly | Check `youtube-transcript-api` releases | Same cat-and-mouse game; the maintainer ships fixes when YouTube changes the caption endpoint. |
| Monthly | Glance at the Google Cloud billing page | Should stay well under $1. Anything higher means a retry loop. |
| Occasionally | `ytdigest status` | Watch `failed_permanent` counts and `consecutive_errors` per channel. |
| If summaries stop arriving | `journalctl -u ytdigest --since yesterday` | The tool alerts on hard failures, but check the log before assuming it was a quiet day. |

### Troubleshooting

**"IpBlocked" / "RequestBlocked" from the transcript library** — your home IP got throttled. Stop
the timer for 24–48 hours, then resume with a lower `max_transcript_fetches_per_run` and longer
delays. If it recurs at normal volume, consider a rotating residential proxy or a hosted transcript
API.

**Everything worked, then stopped after a YouTube change** — update `yt-dlp` and
`youtube-transcript-api` first, before debugging anything else. That fixes it most of the time.

**Host gets sluggish during runs** — confirm `MemoryMax=300M` and `Nice=10` are applied
(`systemctl show ytdigest.service | grep -i memory`), and lower the per-run transcript cap.

## Testing

```bash
pytest
```

The full suite runs offline against recorded fixtures in `tests/fixtures/` — no network access is
made or required.

Note: tests run in the same process, and `load_config()` loads secrets into `os.environ` via
`python-dotenv` (which never *clears* an env var, only sets it). `tests/conftest.py` has an autouse
fixture that snapshots and restores the relevant secret env vars around every test so one test's
`.env` can't leak into another's.

## Known limitation

RSS feeds only expose the last 15 uploads per channel. Fine for daily polling; if `ytdigest` is
offline for more than a few days on a very active channel, the oldest missed uploads may age out of
the feed before being discovered. There's no code-level fix for this — it's a YouTube feed
limitation.
