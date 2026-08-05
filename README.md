# ytdigest

Self-hosted daily YouTube digest for a Raspberry Pi: watches your subscribed channels, fetches
transcripts for new long-form videos, summarizes each in one paragraph, and delivers a digest to
Telegram — with follow-up Q&A per video.

Full design rationale lives in [`youtube-digest-spec.md`](youtube-digest-spec.md). This README is
the operator-facing quick reference.

## Status: Stage 1 (discovery skeleton) is built

Per the spec's build stages, this is a deliberate checkpoint. **Stage 1** implements config
loading, the database, RSS discovery, YouTube Data API metadata, short/live/normal classification,
seeding, and a titles-only digest (stdout/file delivery). It makes **zero transcript or LLM
requests** — it cannot endanger your home IP.

Stage 2 (transcripts, Gemini summaries, Telegram delivery) and Stage 3 (Telegram Q&A bot) are not
yet implemented; their CLI subcommands exist as stubs that print "not implemented yet."

**Before moving to Stage 2:** run Stage 1 against your real ~60 channels for 2–3 days and confirm
Shorts are correctly skipped, livestreams are correctly identified and announced exactly once, and
no duplicate videos appear (`ytdigest status`, and read a few `data/digests/*.md` files by eye).

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
ytdigest run --dry-run   # zero writes, zero outbound calls — safe to run anytime
ytdigest run             # discover -> metadata -> classify -> deliver (Stage 1: titles only)
```

## CLI

| Command | Stage | Notes |
|---|---|---|
| `init-db` | 1 | Creates `data/ytdigest.db` (WAL mode) |
| `add-channel <url\|@handle\|UC…>` | 1 | Resolving a handle needs `YOUTUBE_API_KEY` (one API call) |
| `import-channels <file>` | 1 | Takeout CSV or newline list; UC ids need no network |
| `seed --since YYYY-MM-DD` | 1 | Backfill — run once before the first real `run` |
| `run [--dry-run] [--channel telegram\|stdout\|file]` | 1 | Full pipeline; `--dry-run` touches nothing |
| `discover [--dry-run]` | 1 | Discovery phase only |
| `status` | 1 | Counts by state, last run, pending retries, channel errors |
| `fetch-transcripts`, `summarize`, `deliver`, `retry`, `export` | 2 | Not yet implemented |
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
| `transcript_languages` | `[de, en]` | Stage 2. Caption language preference order. |
| `transcript_delay_seconds` | `[2, 5]` | Stage 2. Jittered sleep between transcript fetches — the most IP-sensitive phase. |
| `max_transcript_fetches_per_run` | `40` | Stage 2. Hard cap per run; excess stays queued. |
| `enable_whisper_fallback` | `false` | Stage 2. Tier-3 ASR fallback via a remote API (never local — won't fit next to Pi-hole). |
| `whisper_max_duration_minutes` | `120` | Stage 2. Skip ASR fallback beyond this length (cost/time control). |
| `retry_backoff_hours` | `[6, 12, 24, 48, 96]` | Stage 2. Auto-captions are often missing for hours after upload — this is a retryable, not fatal, condition. |
| `max_transcript_attempts` | `5` | Stage 2. After this many retries, a video becomes `failed_permanent` (still listed in the digest so nothing silently vanishes). |
| `summary_model` | `gemini-2.5-flash-lite` | Stage 2. Cheap, fast, sufficient for one-paragraph summaries. |
| `summary_mode` | `sync` | Stage 2. `batch` is documented (50% cheaper, up to 24h latency) but not implemented — the savings are ~20 Rappen/month, not worth the latency on a daily digest. |
| `summary_words` | `[60, 100]` | Stage 2. Target summary length. |
| `output_language` | `en` | Stage 2. All summaries are produced in this language regardless of the source video's language. |
| `max_input_chars` | `400000` | Stage 2. Above this, map-reduce chunking kicks in instead of a single call. |
| `delivery_channel` | `stdout` | `telegram` \| `stdout` \| `file`. `stdout` is the development default; switch to `telegram` once Stage 2 lands and `.env` has bot credentials. |
| `telegram_message_delay_seconds` | `1` | Stage 2. Per-chat rate limit between digest messages. |

## Secrets (`.env`, chmod 600)

`YOUTUBE_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_CHAT_ID` — see `.env.example`. Only `YOUTUBE_API_KEY` is required for Stage 1
(and only if you add channels by handle, or run anything past `discover`/`import-channels` with a
Takeout CSV). `delivery_channel: telegram` requires the two Telegram variables; the config loader
fails loudly and immediately if they're missing.

## Testing

```bash
pytest
```

The full suite runs offline against recorded fixtures in `tests/fixtures/` — no network access is
made or required. It covers classification routing, ISO 8601 duration parsing (including the
`P0D` upcoming-livestream edge case), RSS dedup, per-channel failure isolation, seeding, MarkdownV2
escaping, config validation, and `run --dry-run`'s zero-write/zero-network guarantee.

## Operations

See [`manual-setup.md`](manual-setup.md) for account/key setup, the Pi baseline, systemd
installation, and backups — everything that's the human's job rather than the code's.

## Known limitation

RSS feeds only expose the last 15 uploads per channel. Fine for daily polling; if `ytdigest` is
offline for more than a few days on a very active channel, the oldest missed uploads may age out of
the feed before being discovered. There's no code-level fix for this — it's a YouTube feed
limitation.
