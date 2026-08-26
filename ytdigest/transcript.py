"""Three-tier transcript fetch chain + output normalization.

Tier 1: youtube-transcript-api (captions API). Tier 2: yt-dlp subtitle extraction.
Tier 3: audio + remote ASR (Groq whisper), only if enable_whisper_fallback.

Politeness is enforced by the caller (run_transcript_phase): strictly sequential, jittered
delay between fetches, a hard cap per run, and immediate phase-abort on any block/rate-limit
signal (including youtube-transcript-api wrapping HTTP 429 as YouTubeRequestFailed — do not
fall through to tier2 timedtext on the same throttle).

When PROXYSCRAPE_USERNAME + PROXYSCRAPE_PASSWORD are set, a tier1 block/429 is retried up to
3 times via ProxyScrape residential (fresh sticky session each attempt). Each attempt tries
youtube-transcript-api first, then yt-dlp timedtext through the same proxy — HTML-scraped
caption URLs often return empty 200s; yt-dlp client URLs still work. Never fall through to
unproxied tier2/3 on the home IP after a rate-limit block.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import ParseError as XmlParseError
from zoneinfo import ZoneInfo

from urllib.parse import parse_qs, quote, urlparse

import requests
import yt_dlp
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api import (
    AgeRestricted,
    CouldNotRetrieveTranscript,
    InvalidVideoId,
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
)
from youtube_transcript_api.proxies import GenericProxyConfig

from .models import VideoState
from .util import collapse_whitespace, is_youtube_video_id, jittered_sleep, utcnow_iso

logger = logging.getLogger("ytdigest")

BRACKET_ARTIFACT_RE = re.compile(
    r"\[(?:Music|Musik|Applause|Applaus|Gelächter|Laughter|Lachen)\]|>>", re.IGNORECASE
)

WORDS_PER_MINUTE_LOW = 100
WORDS_PER_MINUTE_HIGH = 200

# ProxyScrape Regular Residential defaults (override via PROXYSCRAPE_HOST / PROXYSCRAPE_PORT).
PROXYSCRAPE_DEFAULT_HOST = "rp.scrapegw.com"
PROXYSCRAPE_DEFAULT_PORT = "6060"
PROXYSCRAPE_MAX_ATTEMPTS = 3


# --------------------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------------------


@dataclass
class TranscriptOutcome:
    ok: bool
    source: str | None = None
    language: str | None = None
    is_auto: bool | None = None
    segments: list[dict] = field(default_factory=list)  # raw {"text": ..., "start": float}
    fatal: bool = False
    video_missing: bool = False
    blocked: bool = False
    reason: str | None = None
    tier2_failed: bool = False


# --------------------------------------------------------------------------------------
# Output normalization (applies to all tiers)
# --------------------------------------------------------------------------------------


def _overlap_len(prev: str, current: str) -> int:
    """Length of the longest suffix of `prev` that matches a prefix of `current`."""
    max_check = min(len(prev), len(current))
    for length in range(max_check, 0, -1):
        if prev[-length:] == current[:length]:
            return length
    return 0


def dedup_segments(segments: list[dict]) -> list[str]:
    """Drop the rolling-window overlap between consecutive auto-caption segments.

    Each segment's text is compared against the *previous segment's original* text (not the
    accumulated output) since YouTube's rolling captions overlap one segment at a time.
    Returns the trimmed, non-empty text pieces in order.
    """
    out = []
    prev_text = ""
    for seg in segments:
        text = seg.get("text", "") or ""
        overlap = _overlap_len(prev_text, text)
        trimmed = text[overlap:]
        if trimmed.strip():
            out.append(trimmed)
        prev_text = text
    return out


def strip_artifacts(text: str) -> str:
    return BRACKET_ARTIFACT_RE.sub("", text)


def clean_transcript(segments: list[dict]) -> str:
    """Full normalization pipeline: dedup overlap -> strip artifacts -> collapse whitespace."""
    pieces = dedup_segments(segments)
    cleaned = [strip_artifacts(p) for p in pieces]
    joined = " ".join(p for p in cleaned if p.strip())
    return collapse_whitespace(joined)


def is_plausible_length(word_count: int, duration_seconds: int | None) -> bool:
    if not duration_seconds or duration_seconds <= 0:
        return True
    minutes = duration_seconds / 60
    low = minutes * WORDS_PER_MINUTE_LOW * 0.5  # generous margin either side
    high = minutes * WORDS_PER_MINUTE_HIGH * 2
    return low <= word_count <= high


def write_transcript_files(
    video_id: str, channel_id: str, transcripts_dir: Path, clean_text: str, segments: list[dict]
) -> tuple[Path, Path, int]:
    out_dir = transcripts_dir / channel_id
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"{video_id}.txt"
    jsonl_path = out_dir / f"{video_id}.jsonl"

    txt_path.write_text(clean_text, encoding="utf-8")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(json.dumps({"t": seg.get("start", 0), "text": seg.get("text", "")}) + "\n")

    return txt_path, jsonl_path, len(clean_text)


# --------------------------------------------------------------------------------------
# Rate-limit detection (shared across tiers)
# --------------------------------------------------------------------------------------


def _is_rate_limited(exc: BaseException) -> bool:
    """True for HTTP 429 / 'too many requests', including youtube-transcript-api wrappers."""
    response = getattr(exc, "response", None)
    if response is not None and getattr(response, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


def build_proxyscrape_proxy_url(
    secrets: dict, *, video_id: str | None = None, attempt: int = 1
) -> str | None:
    """Build an HTTP proxy URL for ProxyScrape residential, or None if not configured.

    Credentials come from .env (PROXYSCRAPE_USERNAME / PROXYSCRAPE_PASSWORD). Optional:
    PROXYSCRAPE_HOST, PROXYSCRAPE_PORT, PROXYSCRAPE_COUNTRY (e.g. ``ch`` / ``us``).
    When ``video_id`` is set, a short sticky session is appended so list+fetch share one IP.
    ``attempt`` (1-based) is folded into the session id so retries rotate residential IPs.
    """
    user = (secrets.get("PROXYSCRAPE_USERNAME") or "").strip()
    password = secrets.get("PROXYSCRAPE_PASSWORD") or ""
    if not user or not password:
        return None

    host = (secrets.get("PROXYSCRAPE_HOST") or PROXYSCRAPE_DEFAULT_HOST).strip()
    port = str(secrets.get("PROXYSCRAPE_PORT") or PROXYSCRAPE_DEFAULT_PORT).strip()
    country = (secrets.get("PROXYSCRAPE_COUNTRY") or "").strip().lower()
    if country and f"-country-{country}" not in user.lower():
        user = f"{user}-country-{country}"
    if video_id and "-session-" not in user.lower():
        sess = re.sub(r"[^A-Za-z0-9]", "", video_id)[:16] or "vid"
        if attempt > 1:
            sess = f"{sess}r{attempt}"
        user = f"{user}-session-{sess}-lifetime-5"

    return (
        f"http://{quote(user, safe='-._~')}:{quote(password, safe='')}@{host}:{port}"
    )


def _ytt_api_for_proxy(proxy_url: str | None):
    if not proxy_url:
        return YouTubeTranscriptApi()
    return YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
    )


# --------------------------------------------------------------------------------------
# Tier 1 — youtube-transcript-api
# --------------------------------------------------------------------------------------


def _select_transcript(transcript_list, languages):
    """Manual in `languages` -> generated in `languages` -> any available track."""
    try:
        return transcript_list.find_manually_created_transcript(languages)
    except NoTranscriptFound:
        pass
    try:
        return transcript_list.find_generated_transcript(languages)
    except NoTranscriptFound:
        pass
    return next(iter(transcript_list), None)


def fetch_tier1(
    video_id: str, languages: list[str], ytt_api=None, proxy_url: str | None = None
) -> TranscriptOutcome:
    ytt_api = ytt_api or _ytt_api_for_proxy(proxy_url)
    source = "captions_api_proxy" if proxy_url else "captions_api"

    try:
        transcript_list = ytt_api.list(video_id)
    except TranscriptsDisabled:
        return TranscriptOutcome(ok=False, fatal=True, reason="captions disabled by uploader")
    except (VideoUnavailable, InvalidVideoId):
        return TranscriptOutcome(ok=False, fatal=True, video_missing=True, reason="video private or deleted")
    except AgeRestricted:
        return TranscriptOutcome(ok=False, fatal=True, reason="age restricted, cannot fetch without auth")
    except IpBlocked:
        return TranscriptOutcome(ok=False, blocked=True, reason="IP blocked by YouTube (tier1)")
    except RequestBlocked:
        return TranscriptOutcome(ok=False, blocked=True, reason="request blocked / rate limited (tier1)")
    except CouldNotRetrieveTranscript as exc:
        if _is_rate_limited(exc):
            return TranscriptOutcome(ok=False, blocked=True, reason=f"tier1 blocked: {exc}")
        return TranscriptOutcome(ok=False, reason=f"tier1 list error: {exc}")
    except requests.RequestException as exc:
        if _is_rate_limited(exc):
            return TranscriptOutcome(ok=False, blocked=True, reason=f"tier1 blocked: {exc}")
        return TranscriptOutcome(ok=False, reason=f"network error (tier1): {exc}")

    transcript = _select_transcript(transcript_list, languages)
    if transcript is None:
        return TranscriptOutcome(ok=False, reason="no transcript track available yet")

    try:
        fetched = transcript.fetch()
    except IpBlocked:
        return TranscriptOutcome(ok=False, blocked=True, reason="IP blocked by YouTube (tier1)")
    except RequestBlocked:
        return TranscriptOutcome(ok=False, blocked=True, reason="request blocked / rate limited (tier1)")
    except CouldNotRetrieveTranscript as exc:
        if _is_rate_limited(exc):
            return TranscriptOutcome(ok=False, blocked=True, reason=f"tier1 blocked: {exc}")
        return TranscriptOutcome(ok=False, reason=f"tier1 fetch error: {exc}")
    except XmlParseError as exc:
        return TranscriptOutcome(ok=False, reason=f"tier1 fetch error: invalid transcript XML ({exc})")
    except requests.RequestException as exc:
        if _is_rate_limited(exc):
            return TranscriptOutcome(ok=False, blocked=True, reason=f"tier1 blocked: {exc}")
        return TranscriptOutcome(ok=False, reason=f"network error (tier1): {exc}")

    segments = [{"text": s.text, "start": s.start} for s in fetched.snippets]
    return TranscriptOutcome(
        ok=True,
        source=source,
        language=transcript.language_code,
        is_auto=transcript.is_generated,
        segments=segments,
    )


# --------------------------------------------------------------------------------------
# Tier 2 — yt-dlp subtitle extraction
# --------------------------------------------------------------------------------------


def _extract_info(video_id: str, proxy_url: str | None = None) -> dict:
    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "socket_timeout": 20,
        "ignore_no_formats_error": True,
    }
    if proxy_url:
        ydl_opts["proxy"] = proxy_url
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def _subtitle_url_is_translation(url: str | None) -> bool:
    """True when the timedtext URL fetches a translated track (``tlang=`` param)."""
    if not url:
        return False
    return bool(parse_qs(urlparse(url).query).get("tlang"))


def _ytdlp_json3_candidates(
    info: dict, languages: list[str]
) -> list[tuple[str, dict, bool, bool]]:
    """Collect json3 tracks as (listed_lang, fmt, is_auto, is_translation)."""
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    out: list[tuple[str, dict, bool, bool]] = []
    seen_urls: set[str] = set()

    def find_json3(track_dict, lang):
        for fmt in track_dict.get(lang, []):
            if fmt.get("ext") == "json3":
                return fmt
        return None

    def add(lang: str, fmt: dict, is_auto: bool) -> None:
        url = fmt.get("url")
        if not url or url in seen_urls:
            return
        seen_urls.add(url)
        out.append((lang, fmt, is_auto, _subtitle_url_is_translation(url)))

    for lang in languages:
        fmt = find_json3(manual, lang)
        if fmt:
            add(lang, fmt, False)
    for lang in languages:
        fmt = find_json3(auto, lang)
        if fmt:
            add(lang, fmt, True)
    for lang in manual:
        fmt = find_json3(manual, lang)
        if fmt:
            add(lang, fmt, False)
    for lang in auto:
        fmt = find_json3(auto, lang)
        if fmt:
            add(lang, fmt, True)
    return out


def _select_ytdlp_track(info: dict, languages: list[str]) -> tuple[str | None, dict | None, bool]:
    """Returns (language_code, format_dict, is_auto) for the json3 track, manual first."""
    candidates = _ytdlp_json3_candidates(info, languages)
    for native_only in (True, False):
        for lang, fmt, is_auto, is_translation in candidates:
            if native_only and is_translation:
                continue
            if not native_only and not is_translation:
                continue
            return lang, fmt, is_auto
    return None, None, False


def _parse_json3(data: dict) -> list[dict]:
    segments = []
    for event in data.get("events", []):
        if "segs" not in event:
            continue
        text = "".join(seg.get("utf8", "") for seg in event["segs"])
        if not text.strip():
            continue
        segments.append({"text": text, "start": event.get("tStartMs", 0) / 1000})
    return segments


def fetch_tier2(
    video_id: str,
    languages: list[str],
    extract_info_fn=_extract_info,
    fetch_fn=None,
    proxy_url: str | None = None,
) -> TranscriptOutcome:
    if fetch_fn is None:
        if proxy_url:
            fetch_fn = lambda url, _p=proxy_url: requests.get(
                url, timeout=20, proxies={"http": _p, "https": _p}
            )
        else:
            fetch_fn = lambda url: requests.get(url, timeout=20)

    try:
        try:
            info = extract_info_fn(video_id, proxy_url=proxy_url)
        except TypeError:
            info = extract_info_fn(video_id)
    except yt_dlp.utils.DownloadError as exc:
        msg = str(exc).lower()
        if "private video" in msg or "unavailable" in msg or "has been removed" in msg:
            return TranscriptOutcome(ok=False, fatal=True, video_missing=True, reason=f"tier2: {exc}")
        if "429" in msg or "too many requests" in msg or "blocked" in msg:
            return TranscriptOutcome(ok=False, blocked=True, reason=f"tier2 blocked: {exc}")
        return TranscriptOutcome(ok=False, reason=f"tier2 error: {exc}")
    except Exception as exc:  # yt-dlp raises a variety of internal exceptions
        return TranscriptOutcome(ok=False, reason=f"tier2 unexpected error: {exc}")

    lang, fmt, is_auto = _select_ytdlp_track(info, languages)
    if fmt is None:
        return TranscriptOutcome(ok=False, reason="tier2: no json3 subtitle track available")

    try:
        resp = fetch_fn(fmt["url"])
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        if _is_rate_limited(exc):
            return TranscriptOutcome(ok=False, blocked=True, reason=f"tier2 blocked: {exc}")
        return TranscriptOutcome(ok=False, reason=f"tier2 download error: {exc}")

    segments = _parse_json3(data)
    if not segments:
        return TranscriptOutcome(ok=False, reason="tier2: subtitle track was empty")

    source = "ytdlp_proxy" if proxy_url else "ytdlp"
    return TranscriptOutcome(ok=True, source=source, language=lang, is_auto=is_auto, segments=segments)


# --------------------------------------------------------------------------------------
# Tier 3 — audio + remote ASR (Groq whisper)
# --------------------------------------------------------------------------------------

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_MAX_BYTES = 25 * 1024 * 1024  # Groq's free-tier request size limit


def _download_audio(video_id: str, out_dir: Path) -> Path:
    if not is_youtube_video_id(video_id):
        raise ValueError(f"invalid video_id: {video_id!r}")
    out_template = str(out_dir / f"{video_id}.%(ext)s")
    subprocess.run(
        [
            "yt-dlp",
            "-f",
            "bestaudio",
            "--extract-audio",
            "--audio-format",
            "opus",
            "--audio-quality",
            "6",
            "-o",
            out_template,
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )
    candidates = list(out_dir.glob(f"{video_id}.*"))
    if not candidates:
        raise RuntimeError("yt-dlp did not produce an audio file")
    return candidates[0]


def _transcode_audio(src: Path, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", "-b:a", "16k", str(dst)],
        check=True,
        capture_output=True,
        timeout=600,
    )


def _groq_transcribe(audio_path: Path, api_key: str, post_fn=None) -> str:
    post_fn = post_fn or requests.post
    with open(audio_path, "rb") as f:
        resp = post_fn(
            GROQ_TRANSCRIBE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": GROQ_MODEL, "response_format": "text"},
            files={"file": (audio_path.name, f, "audio/ogg")},
            timeout=300,
        )
    resp.raise_for_status()
    return resp.text if isinstance(resp.text, str) else resp.json().get("text", "")


def fetch_tier3(
    video_id: str,
    api_key: str,
    download_fn=_download_audio,
    transcode_fn=_transcode_audio,
    post_fn=None,
) -> TranscriptOutcome:
    if not api_key:
        return TranscriptOutcome(ok=False, reason="tier3: GROQ_API_KEY not set")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            raw_audio = download_fn(video_id, tmp_path)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode(errors="replace")
            if "private" in stderr.lower() or "unavailable" in stderr.lower():
                return TranscriptOutcome(ok=False, fatal=True, video_missing=True, reason=f"tier3 download: {stderr[:200]}")
            return TranscriptOutcome(ok=False, reason=f"tier3 download failed: {stderr[:200]}")
        except Exception as exc:
            return TranscriptOutcome(ok=False, reason=f"tier3 download error: {exc}")

        transcoded = tmp_path / f"{video_id}.processed.opus"
        try:
            transcode_fn(raw_audio, transcoded)
        except Exception as exc:
            return TranscriptOutcome(ok=False, reason=f"tier3 transcode error: {exc}")
        finally:
            raw_audio.unlink(missing_ok=True)

        try:
            text = _groq_transcribe(transcoded, api_key, post_fn=post_fn)
        except requests.RequestException as exc:
            return TranscriptOutcome(ok=False, reason=f"tier3 ASR error: {exc}")
        finally:
            transcoded.unlink(missing_ok=True)

    if not text.strip():
        return TranscriptOutcome(ok=False, reason="tier3: empty transcription")

    return TranscriptOutcome(ok=True, source="whisper", language=None, is_auto=True, segments=[{"text": text, "start": 0}])


# --------------------------------------------------------------------------------------
# Retry scheduling
# --------------------------------------------------------------------------------------


def compute_next_retry(attempts: int, backoff_hours: list[int], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    idx = min(attempts - 1, len(backoff_hours) - 1)
    idx = max(idx, 0)
    return (now + timedelta(hours=backoff_hours[idx])).replace(microsecond=0).isoformat()


def transcript_lookback_cutoff_iso(config, *, now: datetime | None = None) -> str:
    """UTC ISO start of (local today − transcript_lookback_days).

    Default lookback_days=1 means today and yesterday in ``config.timezone``.
    """
    now = now or datetime.now(timezone.utc)
    days = int(config.values["transcript_lookback_days"])
    tz = ZoneInfo(config.values["timezone"])
    local_today = now.astimezone(tz).date()
    start_local = datetime.combine(local_today - timedelta(days=days), time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------


@dataclass
class TranscriptPhaseResult:
    attempted: int = 0
    succeeded_ids: list[str] = field(default_factory=list)
    failed_permanent_ids: list[str] = field(default_factory=list)
    retrying: int = 0
    aborted: bool = False
    abort_reason: str | None = None
    errors: list[str] = field(default_factory=list)
    had_tier2_error: bool = False
    retryable_ids: list[str] = field(default_factory=list)


def process_video(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    config,
    tier1_fn=fetch_tier1,
    tier2_fn=fetch_tier2,
    tier3_fn=fetch_tier3,
    tier1_proxy_fn=None,
) -> TranscriptOutcome:
    """Run the tier chain for one video and persist the result. Does not sleep."""
    languages = config.values["transcript_languages"]
    video_id = row["video_id"]
    now = utcnow_iso()

    outcome = tier1_fn(video_id, languages)
    tier2_failed = False
    tier1_was_blocked = outcome.blocked
    proxy_attempted = False

    if outcome.blocked:
        use_injected = tier1_proxy_fn is not None
        can_proxy = use_injected or build_proxyscrape_proxy_url(config.secrets) is not None
        if can_proxy:
            proxy_attempted = True
            for attempt in range(1, PROXYSCRAPE_MAX_ATTEMPTS + 1):
                logger.info(
                    "tier1 blocked for %s; ProxyScrape residential attempt %d/%d",
                    video_id,
                    attempt,
                    PROXYSCRAPE_MAX_ATTEMPTS,
                )
                if use_injected:
                    outcome = tier1_proxy_fn(video_id, languages)
                else:
                    proxy_url = build_proxyscrape_proxy_url(
                        config.secrets, video_id=video_id, attempt=attempt
                    )
                    outcome = fetch_tier1(video_id, languages, proxy_url=proxy_url)
                    if (
                        not outcome.ok
                        and not outcome.blocked
                        and not (outcome.fatal and outcome.video_missing)
                    ):
                        logger.info(
                            "tier1 via proxy failed for %s (%s); trying yt-dlp via same proxy",
                            video_id,
                            outcome.reason,
                        )
                        outcome = fetch_tier2(video_id, languages, proxy_url=proxy_url)
                if outcome.ok:
                    break
                logger.warning(
                    "ProxyScrape attempt %d/%d failed for %s: ok=%s blocked=%s reason=%s",
                    attempt,
                    PROXYSCRAPE_MAX_ATTEMPTS,
                    video_id,
                    outcome.ok,
                    outcome.blocked,
                    outcome.reason,
                )
                if attempt < PROXYSCRAPE_MAX_ATTEMPTS:
                    jittered_sleep(1, 2)

    # After a rate-limit block, never fall through to unproxied tier2/3 on the home IP.
    if (
        tier1_was_blocked
        and proxy_attempted
        and not outcome.ok
        and not (outcome.fatal and outcome.video_missing)
    ):
        reason = outcome.reason or "unknown"
        if not outcome.blocked:
            outcome = TranscriptOutcome(
                ok=False,
                blocked=True,
                reason=f"ProxyScrape retry failed after tier1 block: {reason}",
            )

    if not outcome.ok and not outcome.blocked and not (outcome.fatal and outcome.video_missing):
        outcome = tier2_fn(video_id, languages)
        if not outcome.ok:
            tier2_failed = True

    if (
        not outcome.ok
        and not outcome.blocked
        and not (outcome.fatal and outcome.video_missing)
        and config.values["enable_whisper_fallback"]
    ):
        duration = row["duration_seconds"] or 0
        max_seconds = config.values["whisper_max_duration_minutes"] * 60
        if duration <= max_seconds:
            t3 = tier3_fn(video_id, config.secrets.get("GROQ_API_KEY", ""))
            if t3.ok:
                tier2_failed = False
            outcome = t3

    outcome.tier2_failed = tier2_failed

    if outcome.ok:
        clean_text = clean_transcript(outcome.segments)
        word_count = len(clean_text.split())
        if not is_plausible_length(word_count, row["duration_seconds"]):
            logger.warning(
                "implausible transcript length for %s: %d words over %ss",
                video_id, word_count, row["duration_seconds"],
            )
        write_transcript_files(video_id, row["channel_id"], config.transcripts_dir, clean_text, outcome.segments)
        conn.execute(
            """
            UPDATE videos
            SET state = ?, transcript_source = ?, transcript_lang = ?, transcript_auto = ?,
                transcript_chars = ?, attempts = 0, next_retry_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE video_id = ?
            """,
            (
                VideoState.HAS_TRANSCRIPT.value,
                outcome.source,
                outcome.language,
                int(bool(outcome.is_auto)),
                len(clean_text),
                now,
                video_id,
            ),
        )
        conn.commit()
        return outcome

    if outcome.fatal:
        conn.execute(
            """
            UPDATE videos
            SET state = ?, last_error = ?, updated_at = ?
            WHERE video_id = ?
            """,
            (VideoState.FAILED_PERMANENT.value, outcome.reason, now, video_id),
        )
        conn.commit()
        return outcome

    if outcome.blocked:
        # Do not bump attempts — this wasn't the video's fault. Just record the reason.
        conn.execute(
            "UPDATE videos SET last_error = ?, updated_at = ? WHERE video_id = ?",
            (outcome.reason, now, video_id),
        )
        conn.commit()
        return outcome

    # Retryable (no transcript yet, network error, etc.)
    attempts = row["attempts"] + 1
    max_attempts = config.values["max_transcript_attempts"]
    if attempts >= max_attempts:
        conn.execute(
            """
            UPDATE videos
            SET state = ?, attempts = ?, last_error = ?, updated_at = ?
            WHERE video_id = ?
            """,
            (VideoState.FAILED_PERMANENT.value, attempts, outcome.reason, now, video_id),
        )
    else:
        next_retry = compute_next_retry(attempts, config.values["retry_backoff_hours"])
        conn.execute(
            """
            UPDATE videos
            SET attempts = ?, next_retry_at = ?, last_error = ?, updated_at = ?
            WHERE video_id = ?
            """,
            (attempts, next_retry, outcome.reason, now, video_id),
        )
    conn.commit()
    return outcome


def run_transcript_phase(
    conn: sqlite3.Connection,
    config,
    tier1_fn=fetch_tier1,
    tier2_fn=fetch_tier2,
    tier3_fn=fetch_tier3,
    limit: int | None = None,
    *,
    catch_up: bool = False,
    now: datetime | None = None,
) -> TranscriptPhaseResult:
    cap = config.values["max_transcript_fetches_per_run"]
    if limit is not None:
        cap = min(cap, limit)

    now_dt = now or datetime.now(timezone.utc)
    now_iso = now_dt.replace(microsecond=0).isoformat()
    params: list = [VideoState.NEEDS_TRANSCRIPT.value, now_iso]
    lookback_sql = ""
    if not catch_up:
        cutoff = transcript_lookback_cutoff_iso(config, now=now_dt)
        lookback_sql = " AND published_at IS NOT NULL AND published_at >= ?"
        params.append(cutoff)
        logger.info(
            "transcript phase: default window published_at>=%s (lookback_days=%s); "
            "pass --catch-up for all pending",
            cutoff,
            config.values["transcript_lookback_days"],
        )
    else:
        logger.info("transcript phase: --catch-up, fetching all pending needs_transcript")

    params.append(cap)
    rows = conn.execute(
        f"""
        SELECT * FROM videos
        WHERE state = ? AND (next_retry_at IS NULL OR next_retry_at <= ?)
        {lookback_sql}
        ORDER BY published_at IS NULL, published_at DESC, discovered_at
        LIMIT ?
        """,
        params,
    ).fetchall()

    delay_low, delay_high = config.values["transcript_delay_seconds"]
    result = TranscriptPhaseResult()

    for i, row in enumerate(rows):
        result.attempted += 1
        outcome = process_video(conn, row, config, tier1_fn=tier1_fn, tier2_fn=tier2_fn, tier3_fn=tier3_fn)

        if outcome.tier2_failed:
            result.had_tier2_error = True

        if outcome.blocked:
            result.aborted = True
            result.abort_reason = outcome.reason
            result.errors.append(f"{row['video_id']}: {outcome.reason}")
            result.retryable_ids.append(row["video_id"])
            break

        if outcome.ok:
            result.succeeded_ids.append(row["video_id"])
        elif outcome.fatal:
            result.failed_permanent_ids.append(row["video_id"])
            result.errors.append(f"{row['video_id']}: {outcome.reason}")
        else:
            updated = conn.execute(
                "SELECT state FROM videos WHERE video_id = ?", (row["video_id"],)
            ).fetchone()
            if updated and updated["state"] == VideoState.FAILED_PERMANENT.value:
                result.failed_permanent_ids.append(row["video_id"])
                result.errors.append(f"{row['video_id']}: {outcome.reason}")
            else:
                result.retrying += 1
                result.retryable_ids.append(row["video_id"])
                result.errors.append(f"{row['video_id']}: {outcome.reason}")

        if i < len(rows) - 1:
            jittered_sleep(delay_low, delay_high)

    return result
