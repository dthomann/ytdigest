# Manual Setup — things Claude Code cannot do for you

Everything here is yours to do: accounts, keys, hardware decisions, and the handful of commands that
touch the Pi's system state. Steps 1–7 should be done **before** Claude Code starts, because the spec
assumes these values exist. Steps 8–13 come after Stage 1 is built.

---

## Before Claude Code starts

### 1. Pi baseline

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-venv python3-pip sqlite3 ffmpeg git rsync
python3 --version          # need 3.11+
free -h                    # confirm headroom next to Pi-hole
df -h                      # confirm free space
```

`ffmpeg` is only needed for the rare Whisper fallback, but installing it now avoids a confusing
failure at 3am three weeks from now.

### 2. Decide where the data lives

The SD card will hold this fine — ~450 transcripts/month at ~100 KB is roughly 0.5 GB/year. The
concern is write wear, not capacity.

- **Do nothing** if you accept the (small) risk and take backups. WAL mode keeps writes modest.
- **Better:** plug in any spare USB stick or SSD, format ext4, mount at `/mnt/ytdata`, add to
  `/etc/fstab` with `noatime`. Point the tool's `data_dir` there.

Either way, step 12 (backups) is not optional. SD cards fail without warning and you would lose the
transcript archive, not just the state.

### 3. Create a dedicated system user

```bash
sudo useradd -r -m -d /opt/ytdigest -s /usr/sbin/nologin ytdigest
sudo mkdir -p /opt/ytdigest && sudo chown ytdigest:ytdigest /opt/ytdigest
```

Keeps the job away from your personal account and lets systemd's `MemoryMax` apply cleanly.

### 4. YouTube Data API key

1. https://console.cloud.google.com → create a project (e.g. `ytdigest`)
2. **APIs & Services → Library** → search "YouTube Data API v3" → **Enable**
3. **Credentials → Create credentials → API key**
4. Click the key → **API restrictions → Restrict key → YouTube Data API v3**. Leave application
   restrictions unset (the Pi's IP is dynamic).

Free quota is 10 000 units/day. You will use ~65. No billing account needed, no card.

### 5. Gemini API key

https://aistudio.google.com/apikey → **Create API key** → attach to the same Cloud project.

There is a free tier with rate limits that may well cover you outright. If you enable billing, the
expected spend is roughly **CHF 0.30–0.50/month**. Set a budget alert anyway:
Cloud Console → **Billing → Budgets & alerts** → budget of CHF 5 with an email trigger at 50%. This
is your protection against a retry loop bug.

### 6. Groq API key (optional — ASR fallback only)

https://console.groq.com → API Keys. Used only when a video has no captions at all, which is a
minority of uploads. The free tier is generous; paid is ~$0.04 per hour of audio. Skip this if you
prefer, and set `enable_whisper_fallback: false`.

### 7. Telegram bot + your chat ID

1. Message **@BotFather** in Telegram → `/newbot` → pick a name and a username ending in `bot`
   → copy the token (`123456789:AA...`).
2. Send your new bot any message (e.g. `hi`) — a bot cannot message you first.
3. Get your chat ID:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Look for `"chat": {"id": 12345678, ...}`. That number is `TELEGRAM_ALLOWED_CHAT_ID`.
4. Optional: `/setprivacy` → Disabled is **not** needed; leave defaults.

### 8. Collect your 60 channel IDs

You need canonical `UC…` IDs, not handles. Easiest path first:

**Google Takeout (recommended for 60 channels):**
https://takeout.google.com → **Deselect all** → select **YouTube and YouTube Music** → click
*All YouTube data included* → keep only **subscriptions** → export. You get a CSV with a
`Channel Id` column. Feed it straight to `ytdigest import-channels`.

**Per-channel fallback:** open the channel page, View Source, search for `"channelId":"UC` — or use
`https://www.youtube.com/@handle/about` and read the ID from the share dialog.

Prune while you are here. 60 channels at ~15 summaries/day is a lot of reading; this is the moment to
decide which ones you actually want a daily paragraph from.

### 9. Put the secrets in place

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

Confirm `.env` is in `.gitignore` before the first commit.

### 10. Pi-hole sanity check

Some aggressive blocklists include YouTube tracking domains that overlap with what the tool needs.
From the Pi itself:

```bash
dig +short www.youtube.com
dig +short www.googleapis.com
dig +short rr1---sn-example.googlevideo.com   # any googlevideo host
curl -sI https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw | head -1
```

Anything resolving to `0.0.0.0` is being blocked by your own Pi-hole. Whitelist it. This is worth
five minutes now to avoid a long evening debugging "transcripts randomly fail" that turns out to be
your DNS blocking yourself.

---

## After Stage 1 is built

### 11. Seed before you ever run for real

```bash
sudo -u ytdigest /opt/ytdigest/venv/bin/ytdigest init-db
sudo -u ytdigest /opt/ytdigest/venv/bin/ytdigest import-channels subscriptions.csv
sudo -u ytdigest /opt/ytdigest/venv/bin/ytdigest seed --since $(date -I)
sudo -u ytdigest /opt/ytdigest/venv/bin/ytdigest status
```

**Do not skip this.** Without seeding, the first run sees ~900 back-catalogue videos and tries to
fetch transcripts for all of them in one burst — the single most reliable way to get your home IP
throttled by YouTube. `status` should show a large `skipped`/`delivered` count and **zero**
`needs_transcript`.

### 12. Install the systemd units

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ytdigest.timer
systemctl list-timers ytdigest.timer          # confirm next run time
journalctl -u ytdigest -f                     # watch a manual run
sudo systemctl start ytdigest.service         # trigger one now
```

Start the bot service only after Stage 3 exists:
`sudo systemctl enable --now ytdigest-bot.service`

### 13. Backups

Set up an SSH key from the Pi to your VPS, then schedule `scripts/backup.sh` weekly (its own timer or
a cron line). Verify once by restoring the DB copy elsewhere and running `ytdigest status` against
it. An untested backup is not a backup.

---

## Ongoing, low-effort maintenance

| Cadence | Task | Why |
|---|---|---|
| Monthly | `pip install -U yt-dlp` inside the venv | yt-dlp breaks whenever YouTube changes something. It is the most update-sensitive dependency you have. Consider a monthly systemd timer for this one package. |
| Monthly | Check `youtube-transcript-api` releases | Same cat-and-mouse game; the maintainer ships fixes when YouTube changes the caption endpoint. |
| Monthly | Glance at the Google Cloud billing page | Should read well under CHF 1. Anything higher means a retry loop. |
| Occasionally | `ytdigest status` | Watch `failed_permanent` counts and `consecutive_errors` per channel — that is how you notice a renamed or deleted channel. |
| If summaries stop arriving | `journalctl -u ytdigest --since yesterday` | The tool alerts on hard failures, but check the log before assuming it was a quiet day. |

---

## Two decisions to make before Claude Code writes the config

1. **`output_language`** — `en` or `de`. Summaries are produced in this language regardless of the
   video's language, so mixed German/English channels all land in one readable digest.
2. **`digest_hour`** — when the timer fires, in `Europe/Zurich`. Note that auto-captions on videos
   uploaded a few hours earlier may not exist yet; the retry logic handles it, but an early-morning
   run means slightly more videos land in the *next* day's digest. Something like 07:00 or 08:00 is a
   reasonable default.

---

## If something goes wrong

**"IpBlocked" / "RequestBlocked" from the transcript library** — your home IP got throttled. Stop the
timer for 24–48 hours, then resume with a lower `max_transcript_fetches_per_run` and longer delays.
If it recurs at normal volume, the residential-IP route has stopped working for you and the fallbacks
are a rotating residential proxy or a hosted transcript API (~$5–17/month).

**Everything worked, then stopped after a YouTube change** — update `yt-dlp` and
`youtube-transcript-api` first, before debugging anything else. That fixes it most of the time.

**Pi-hole gets sluggish during runs** — confirm `MemoryMax=300M` and `Nice=10` are actually applied
(`systemctl show ytdigest.service | grep -i memory`), and lower the per-run transcript cap.
