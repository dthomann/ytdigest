"""Gemini summarization: one paragraph per video, synchronous calls only.

`summary_mode: batch` is documented in config but intentionally not implemented — see README.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .models import VideoState
from .util import retry, utcnow_iso

logger = logging.getLogger("ytdigest")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

CHUNK_SIZE = 80_000

BANNED_OPENERS_NOTE = (
    'Do not open with "In this video...", "The speaker discusses...", '
    '"This video explores various topics...", or any similar meta-framing. '
    "Start directly with the substance: what was said, claimed, argued, or demonstrated."
)


def build_prompt(
    transcript_text: str,
    title: str,
    channel: str,
    output_language: str,
    words_low: int,
    words_high: int,
    transcript_lang: str | None = None,
    transcript_auto: bool | None = None,
) -> str:
    lang_note = ""
    if transcript_lang:
        auto_note = "auto-generated" if transcript_auto else "manually created"
        lang_note = f"\nThe transcript is {auto_note} captions in language '{transcript_lang}'."

    return f"""You are summarizing a YouTube video transcript for a daily digest.

Metadata (for context only — the transcript is the source of truth, not this title):
Title: {title}
Channel: {channel}{lang_note}

Write exactly one paragraph, {words_low}-{words_high} words, in {output_language}, regardless of
the language the video was actually spoken in.

Requirements:
- State what was actually discussed, claimed, argued, or demonstrated: concrete nouns, names,
  numbers, and conclusions from the transcript.
- {BANNED_OPENERS_NOTE}
- No marketing tone, no hype, no words like "fascinating", "deep dive", or "game-changing".
- If the video is largely one thing (an interview, a tutorial, a news roundup, a product review),
  say which — that is the single most useful signal for deciding whether to watch.
- Never invent anything not present in the transcript. Do not let a clickbait title override what
  the transcript actually says.
- If the transcript is auto-generated and clearly garbled or incoherent in places, say so briefly
  in a final short clause rather than inventing content to paper over the gaps.

Transcript:
{transcript_text}
"""


def build_chunk_prompt(chunk_text: str, title: str, channel: str) -> str:
    return f"""This is one part of a longer video transcript. Extract the concrete content —
claims, facts, names, numbers, arguments, conclusions — in dense factual sentences. No commentary,
no meta-framing, no invented content. This is an intermediate summary that will be combined with
other parts, not the final output.

Video: {title} ({channel})

Transcript part:
{chunk_text}
"""


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


class GeminiError(Exception):
    pass


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


@retry(times=3, exceptions=(GeminiError,))
def _call_gemini(
    prompt: str,
    model: str,
    api_key: str,
    max_output_tokens: int,
    temperature: float = 0.3,
    post_fn=None,
) -> str:
    post_fn = post_fn or requests.post
    url = GEMINI_URL.format(model=model)
    resp = post_fn(
        url,
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        },
        timeout=60,
    )
    if resp.status_code != 200:
        if _is_retryable_status(resp.status_code):
            raise GeminiError(f"Gemini API {resp.status_code}: {resp.text[:300]}")
        raise RuntimeError(f"Gemini API {resp.status_code} (not retryable): {resp.text[:300]}")

    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected Gemini response shape: {data}") from exc


def summarize_transcript(
    transcript_text: str,
    title: str,
    channel: str,
    config,
    api_key: str,
    transcript_lang: str | None = None,
    transcript_auto: bool | None = None,
    post_fn=None,
) -> str:
    """Summarize a transcript into one paragraph. Falls back to map-reduce above max_input_chars."""
    model = config.values["summary_model"]
    output_language = config.values["output_language"]
    words_low, words_high = config.values["summary_words"]
    max_input_chars = config.values["max_input_chars"]
    max_output_tokens = int(words_high * 2.5) + 100  # rough words->tokens headroom

    if len(transcript_text) <= max_input_chars:
        prompt = build_prompt(
            transcript_text, title, channel, output_language, words_low, words_high,
            transcript_lang, transcript_auto,
        )
        return _call_gemini(prompt, model, api_key, max_output_tokens, post_fn=post_fn)

    logger.info("transcript exceeds max_input_chars (%d); using map-reduce", max_input_chars)
    chunks = _chunk_text(transcript_text)
    partial_summaries = []
    for chunk in chunks:
        chunk_prompt = build_chunk_prompt(chunk, title, channel)
        partial_summaries.append(_call_gemini(chunk_prompt, model, api_key, 1000, post_fn=post_fn))

    combined = "\n\n".join(partial_summaries)
    final_prompt = build_prompt(
        combined, title, channel, output_language, words_low, words_high,
        transcript_lang, transcript_auto,
    )
    return _call_gemini(final_prompt, model, api_key, max_output_tokens, post_fn=post_fn)


@dataclass
class SummarizePhaseResult:
    attempted: int = 0
    succeeded_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)


def run_summarize_phase(
    conn: sqlite3.Connection,
    config,
    transcripts_dir: Path,
    api_key: str,
    post_fn=None,
) -> SummarizePhaseResult:
    """Summarize every video in state has_transcript (this run's and any left over from a prior
    run whose summarization failed). Failures stay in has_transcript for the next run — no backoff
    scheduling."""
    result = SummarizePhaseResult()
    rows = conn.execute(
        "SELECT * FROM videos WHERE state = ? ORDER BY discovered_at", (VideoState.HAS_TRANSCRIPT.value,)
    ).fetchall()

    for row in rows:
        result.attempted += 1
        txt_path = transcripts_dir / row["channel_id"] / f"{row['video_id']}.txt"
        if not txt_path.exists():
            logger.error("transcript file missing for %s: %s", row["video_id"], txt_path)
            result.failed_ids.append(row["video_id"])
            continue

        transcript_text = txt_path.read_text(encoding="utf-8")
        channel_row = conn.execute(
            "SELECT title FROM channels WHERE channel_id = ?", (row["channel_id"],)
        ).fetchone()
        channel_title = channel_row["title"] if channel_row else row["channel_id"]

        try:
            summary = summarize_transcript(
                transcript_text,
                row["title"] or "",
                channel_title or "",
                config,
                api_key,
                transcript_lang=row["transcript_lang"],
                transcript_auto=bool(row["transcript_auto"]),
                post_fn=post_fn,
            )
        except Exception as exc:
            logger.warning("summarization failed for %s: %s", row["video_id"], exc)
            conn.execute(
                "UPDATE videos SET last_error = ?, updated_at = ? WHERE video_id = ?",
                (f"summarization failed: {exc}", utcnow_iso(), row["video_id"]),
            )
            conn.commit()
            result.failed_ids.append(row["video_id"])
            continue

        conn.execute(
            """
            UPDATE videos
            SET state = ?, summary = ?, summary_model = ?, last_error = NULL, updated_at = ?
            WHERE video_id = ?
            """,
            (VideoState.SUMMARIZED.value, summary, config.values["summary_model"], utcnow_iso(), row["video_id"]),
        )
        conn.commit()
        result.succeeded_ids.append(row["video_id"])

    return result
