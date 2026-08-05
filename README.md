# ytdigest

Self-hosted daily YouTube digest for a Raspberry Pi: watches your subscribed channels, fetches
transcripts for new long-form videos, summarizes each in one paragraph, and delivers a digest to
Telegram — with follow-up Q&A per video.

Full design rationale lives in [`youtube-digest-spec.md`](youtube-digest-spec.md). This README is
the operator-facing quick reference.

## Status: Stage 2 (transcripts + summaries + Telegram) is built

**Stage 1** (config, database, RSS discovery, YouTube Data API metadata, classification, seeding,
titles-only digest) is done and was verified against real channels first — see git history if you
want that checkpoint.

**Stage 2** adds the three-tier transcript chain (captions API → yt-dlp → optional Groq Whisper
fallback), overlap dedup and cleaning, retry/backoff scheduling, Gemini summarization, real
per-video Telegram delivery with MarkdownV2, and failure alerting. `run` now executes the full
pipeline: discover → metadata → classify → transcripts → summarize → deliver.

Stage 3 (the Telegram Q&A bot: `ask`, `bot`) is not yet implemented; those subcommands still print
"not implemented yet."

**Before your first real Stage 2 run:** the transcript layer is the part that can get your
residential IP throttled. Run `ytdigest run --limit 3` first and inspect the stored transcripts by
hand in `data/transcripts/` — specifically check that overlap dedup worked (no duplicated phrases)
and the character count is plausible for the video's length. Only then run without `--limit`.

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt   # installs the ytdigest package (editable) + all dependencies
```

Requires Python 3.11+. All dependencies are pure-Python or have ARM64 wheels — nothing x86-only.

## First run (fully offline-safe until `seed`)

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

Set `delivery_channel: telegram` in `config.yaml` (and fill in `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_ALLOWED_CHAT_ID` in `.env`) once you're ready to move off `stdout`/`file`. To fetch a
chat ID: message your bot once (a bot can't message you first), then
`curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"`.

## CLI

| Command | Stage | Notes |
|---|---|---|
| `init-db` | 1 | Creates `data/ytdigest.db` (WAL mode) |
| `add-channel <url\|@handle\|UC…>` | 1 | Resolving a handle needs `YOUTUBE_API_KEY` (one API call) |
| `import-channels <file>` | 1 | Takeout CSV or newline list; UC ids need no network |
| `seed --since YYYY-MM-DD` | 1 | Backfill — run once before the first real `run` |
| `run [--dry-run] [--limit N] [--channel telegram\|stdout\|file]` | 1+2 | Full pipeline; `--dry-run` touches nothing; `--limit` caps transcript fetches this run |
| `discover [--dry-run]` | 1 | Discovery phase only |
| `fetch-transcripts [--limit N]` | 2 | Transcript phase only (3-tier chain, retry/backoff) |
| `summarize` | 2 | Summarize every video currently in `has_transcript` |
| `deliver [--channel telegram\|stdout\|file]` | 2 | Build + send the digest from current DB state (no discover/transcript/summarize) |
| `retry <video_id> \| --all-failed` | 2 | Reset `failed_permanent` video(s) back to `needs_transcript` |
| `export <video_id> [--format txt\|md]` | 2 | Print a video's transcript, with summary/metadata in `md` mode |
| `status` | 1 | Counts by state, last run, pending retries, channel errors |
| `ask`, `bot` | 3 | Not yet implemented |

## Configuration reference (`config.yaml`)

Secrets never go here — see `.env.example`. Unknown keys and any key that looks like a secret
(containing `key`/`token`/`secret`/`password`) are rejected at load time.

| Key | Default | Why |
|---|---|---|
| `data_dir` | `data` | Root for db/transcripts/digests. Relative paths resolve against `config.yaml`'s directory. |
| `timezone` | `Europe/Zurich` | Used for local-time display and the systemd timer. |
| `digest_hour` | `6` | Intended local hour for the daily run; the systemd timer reads this indirectly (set `OnCalendar` to match). |
| `rss_delay_seconds` | `[1, 2]` | Jittered sleep between RSS polls — protects the residential IP. |
| `max_channel_consecutive_errors` | `10` | After this many consecutive poll failures, warn in the digest (likely a dead/renamed channel). |
| `min_duration_seconds` | `180` | Videos at/under this length are classified as Shorts and skipped. |
| `shorts_probe` | `false` | HEAD-probe `/shorts/{id}` for the 60–180s band to disambiguate real Shorts from short normal videos. Adds a request per ambiguous video; off by default. |
| `summarize_finished_livestreams` | `false` | Explicit user requirement: never summarize livestreams by default. Flip to `true` to route finished streams into the transcript queue instead of the terminal `live_finished` state. |
| `youtube_api_quota_daily` | `10000` | YouTube Data API's free daily unit quota. |
| `youtube_api_quota_warn_fraction` | `0.9` | Abort the run (loudly) before crossing this fraction of daily quota, rather than failing opaquely at the hard limit. |
| `transcript_languages` | `[de, en]` | Caption language preference order: manual in these languages, then auto-generated in these languages, then any available track. |
| `transcript_delay_seconds` | `[2, 5]` | Jittered sleep between transcript fetches — the most IP-sensitive phase. |
| `max_transcript_fetches_per_run` | `40` | Hard cap per run; excess stays queued for next time. Override per-invocation with `run --limit N`. |
| `enable_whisper_fallback` | `false` | Tier-3 ASR fallback via Groq's remote API (never local — won't fit next to Pi-hole). Requires `GROQ_API_KEY`. |
| `whisper_max_duration_minutes` | `120` | Skip ASR fallback beyond this length (cost/time control). |
| `retry_backoff_hours` | `[6, 12, 24, 48, 96]` | Auto-captions are often missing for hours after upload — this is a retryable, not fatal, condition. |
| `max_transcript_attempts` | `5` | After this many retries, a video becomes `failed_permanent` (still listed once in the digest so nothing silently vanishes). |
| `summary_model` | `gemini-2.5-flash-lite` | Cheap, fast, sufficient for one-paragraph summaries. |
| `summary_mode` | `sync` | `batch` is documented (50% cheaper, up to 24h latency) but not implemented — the savings are ~20 Rappen/month, not worth the latency on a daily digest. |
| `summary_words` | `[60, 100]` | Target summary length. |
| `output_language` | `en` | All summaries are produced in this language regardless of the source video's language. |
| `max_input_chars` | `400000` | Above this, map-reduce chunking kicks in instead of a single call. |
| `delivery_channel` | `stdout` | `telegram` \| `stdout` \| `file`. `stdout` is the development default; switch to `telegram` once `.env` has bot credentials. |
| `telegram_message_delay_seconds` | `1` | Per-chat rate limit between digest messages. |

## Secrets (`.env`, chmod 600)

`YOUTUBE_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_CHAT_ID` — see `.env.example`. `YOUTUBE_API_KEY` is required for metadata lookups
(and for resolving @handles). `GEMINI_API_KEY` is required to summarize — without it, `run` and
`summarize` skip that phase, log a note, and the video stays queued at `has_transcript` for next
time. `GROQ_API_KEY` is only needed if `enable_whisper_fallback: true`. `delivery_channel: telegram`
requires the two Telegram variables; failure alerts also use them regardless of the configured
delivery channel, since alerting exists specifically to reach you when the primary channel is
broken. The config loader fails loudly and immediately if a required secret is missing.

## Testing

```bash
pytest
```

The full suite (100+ tests) runs offline against recorded fixtures in `tests/fixtures/` — no
network access is made or required. Beyond the Stage 1 coverage (classification routing, ISO 8601
duration parsing including the `P0D` upcoming-livestream edge case, RSS dedup, per-channel failure
isolation, seeding, config validation, `run --dry-run`'s zero-write/zero-network guarantee), Stage 2
adds: overlap dedup reducing a known rolling-caption input to a known clean output, the 3-tier
transcript fallback chain's exception classification (fatal vs. retryable vs. blocked-and-abort),
retry/backoff scheduling, Gemini summarization including the 429/5xx retry path and map-reduce for
long transcripts, MarkdownV2 message formatting and the Telegram send flow, and the `retry`/`export`
commands.

Note: tests run in the same process, and `load_config()` loads secrets into `os.environ` via
`python-dotenv` (which never *clears* an env var, only sets it). `tests/conftest.py` has an autouse
fixture that snapshots and restores the relevant secret env vars around every test so one test's
`.env` can't leak into another's — worth knowing if you add a test that reads secrets directly from
`os.environ` instead of going through `Config`.

## Operations

See [`manual-setup.md`](manual-setup.md) for account/key setup, the Pi baseline, systemd
installation, and backups — everything that's the human's job rather than the code's.

## Known limitation

RSS feeds only expose the last 15 uploads per channel. Fine for daily polling; if `ytdigest` is
offline for more than a few days on a very active channel, the oldest missed uploads may age out of
the feed before being discovered. There's no code-level fix for this — it's a YouTube feed
limitation.
