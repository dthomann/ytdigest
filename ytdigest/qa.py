"""Question answering over one video's timestamped transcript."""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from .summarize import _call_gemini

logger = logging.getLogger("ytdigest")


class QAError(Exception):
    """User-facing Q&A failure (missing transcript, unknown video, etc.)."""


def load_transcript_segments(jsonl_path: Path) -> list[dict]:
    segments = []
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        segments.append(json.loads(line))
    return segments


def format_segments_for_prompt(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        t = int(seg.get("t", 0))
        text = str(seg.get("text", "")).strip()
        if text:
            lines.append(f"[{t}s] {text}")
    return "\n".join(lines)


def _segments_for_prompt(segments: list[dict], max_chars: int) -> str:
    formatted = format_segments_for_prompt(segments)
    if len(formatted) <= max_chars:
        return formatted
    # Even subsample so long videos still span the full runtime.
    step = max(1, len(segments) * max_chars // max(len(formatted), 1))
    sampled = segments[::step]
    result = format_segments_for_prompt(sampled)
    while len(result) > max_chars and len(sampled) > 1:
        step += 1
        sampled = segments[::step]
        result = format_segments_for_prompt(sampled)
    return result


def build_qa_prompt(
    question: str,
    segments_text: str,
    video_id: str,
    title: str,
    channel: str,
    output_language: str,
    summary: str | None = None,
) -> str:
    summary_block = ""
    if summary:
        summary_block = f"\nDigest summary (secondary context — transcript is authoritative):\n{summary}\n"

    return f"""You answer follow-up questions about a YouTube video using only the timestamped transcript below.

Metadata (context only — the transcript is the source of truth):
Title: {title}
Channel: {channel}
Video URL: https://youtu.be/{video_id}
{summary_block}
Rules:
- Answer in {output_language}, regardless of the video's spoken language.
- Base your answer only on what the transcript actually says. Never invent facts, quotes, or claims.
- When referencing a specific moment, cite the timestamp and include a deep link on its own line or inline:
  https://youtu.be/{video_id}?t={{seconds}}
  Use the [Ns] prefix from the transcript for the seconds value (integer seconds).
- If the transcript does not contain enough information to answer, say so plainly — do not guess.
- Be concise and direct. No filler openers like "In this video..." or "The speaker discusses...".

Transcript (each line is [seconds] text):
{segments_text}

Question: {question}
"""


def _jsonl_path(config, channel_id: str, video_id: str) -> Path:
    return config.transcripts_dir / channel_id / f"{video_id}.jsonl"


def _txt_path(config, channel_id: str, video_id: str) -> Path:
    return config.transcripts_dir / channel_id / f"{video_id}.txt"


def answer_question(
    conn: sqlite3.Connection,
    config,
    video_id: str,
    question: str,
    api_key: str,
    post_fn=None,
) -> str:
    """Answer a question about one video. Raises QAError on missing data."""
    question = question.strip()
    if not question:
        raise QAError("question is empty")

    row = conn.execute(
        """
        SELECT v.*, c.title AS channel_title
        FROM videos v
        JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.video_id = ?
        """,
        (video_id,),
    ).fetchone()
    if row is None:
        raise QAError(f"unknown video_id {video_id!r}")

    jsonl_path = _jsonl_path(config, row["channel_id"], video_id)
    txt_path = _txt_path(config, row["channel_id"], video_id)

    if jsonl_path.exists():
        segments = load_transcript_segments(jsonl_path)
        if not segments:
            raise QAError(f"transcript file is empty for {video_id}")
        segments_text = _segments_for_prompt(segments, config.values["max_input_chars"])
    elif txt_path.exists():
        logger.warning("jsonl missing for %s; answering from plain text without precise timestamps", video_id)
        plain = txt_path.read_text(encoding="utf-8").strip()
        if not plain:
            raise QAError(f"transcript file is empty for {video_id}")
        max_chars = config.values["max_input_chars"]
        if len(plain) > max_chars:
            plain = plain[:max_chars] + "\n[transcript truncated]"
        segments_text = plain
    else:
        raise QAError(f"no transcript on disk for {video_id}")

    prompt = build_qa_prompt(
        question,
        segments_text,
        video_id,
        row["title"] or video_id,
        row["channel_title"] or row["channel_id"],
        config.values["output_language"],
        summary=row["summary"],
    )

    model = config.values["summary_model"]
    return _call_gemini(prompt, model, api_key, max_output_tokens=1024, post_fn=post_fn)


def lookup_video_by_message_id(conn: sqlite3.Connection, message_id: str | int) -> str | None:
    row = conn.execute(
        "SELECT video_id FROM deliveries WHERE message_id = ?",
        (str(message_id),),
    ).fetchone()
    return row["video_id"] if row else None
