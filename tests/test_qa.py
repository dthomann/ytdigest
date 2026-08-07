import json

import pytest

from ytdigest import qa as qa_mod

from .conftest import insert_channel
from .test_summarize import gemini_ok


def write_jsonl(path, segments):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(json.dumps(seg) + "\n")


def test_format_segments_for_prompt():
    text = qa_mod.format_segments_for_prompt(
        [{"t": 0, "text": "Hello"}, {"t": 42, "text": "World"}]
    )
    assert "[0s] Hello" in text
    assert "[42s] World" in text


def test_build_qa_prompt_includes_timestamp_link_instructions():
    prompt = qa_mod.build_qa_prompt(
        "What is said at the start?",
        "[0s] intro",
        "vid123",
        "Title",
        "Channel",
        "en",
        summary="A summary.",
    )
    assert "https://youtu.be/vid123?t={seconds}" in prompt
    assert "What is said at the start?" in prompt
    assert "Answer in en" in prompt
    assert "A summary." in prompt


def test_answer_question_uses_jsonl(conn, config, tmp_path):
    insert_channel(conn, "UC1", title="Chan One")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, summary, discovered_at, updated_at)
        VALUES ('v1', 'UC1', 'Test Video', 'delivered', 'Digest summary.', 'now', 'now')
        """
    )
    conn.commit()

    segments = [
        {"t": 0, "text": "Opening remarks about pricing."},
        {"t": 120, "text": "They argue the fee should be lower."},
    ]
    write_jsonl(config.transcripts_dir / "UC1" / "v1.jsonl", segments)

    post = gemini_ok("The fee discussion starts at https://youtu.be/v1?t=120")
    answer = qa_mod.answer_question(conn, config, "v1", "What about pricing?", "key", post_fn=post)
    assert "youtu.be/v1" in answer


def test_answer_question_falls_back_to_txt(conn, config):
    insert_channel(conn, "UC1")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, discovered_at, updated_at)
        VALUES ('v2', 'UC1', 'Plain', 'delivered', 'now', 'now')
        """
    )
    conn.commit()
    txt_path = config.transcripts_dir / "UC1" / "v2.txt"
    txt_path.parent.mkdir(parents=True, exist_ok=True)
    txt_path.write_text("Plain transcript text here.", encoding="utf-8")

    post = gemini_ok("Answer from plain text.")
    answer = qa_mod.answer_question(conn, config, "v2", "Anything?", "key", post_fn=post)
    assert answer == "Answer from plain text."


def test_answer_question_unknown_video_raises(conn, config):
    with pytest.raises(qa_mod.QAError, match="unknown video_id"):
        qa_mod.answer_question(conn, config, "missing", "question?", "key", post_fn=gemini_ok())


def test_answer_question_no_transcript_raises(conn, config):
    insert_channel(conn, "UC1")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, discovered_at, updated_at)
        VALUES ('v3', 'UC1', 'No transcript', 'delivered', 'now', 'now')
        """
    )
    conn.commit()
    with pytest.raises(qa_mod.QAError, match="no transcript"):
        qa_mod.answer_question(conn, config, "v3", "question?", "key", post_fn=gemini_ok())


def test_lookup_video_by_message_id(conn):
    insert_channel(conn, "UC1")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, discovered_at, updated_at)
        VALUES ('vid_x', 'UC1', 'Title', 'delivered', 'now', 'now')
        """
    )
    conn.execute(
        "INSERT INTO deliveries (message_id, video_id, run_id, sent_at) VALUES ('99', 'vid_x', NULL, 'now')"
    )
    conn.commit()
    assert qa_mod.lookup_video_by_message_id(conn, 99) == "vid_x"
    assert qa_mod.lookup_video_by_message_id(conn, "missing") is None


def test_segments_for_prompt_subsamples_long_transcripts():
    segments = [{"t": i * 10, "text": f"word{i} " * 20} for i in range(500)]
    result = qa_mod._segments_for_prompt(segments, max_chars=5000)
    assert len(result) <= 5000
    assert "[0s]" in result
