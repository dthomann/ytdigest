import argparse
import threading

import pytest

from ytdigest import bot as bot_mod, cli, db
from ytdigest.models import VideoState

from .conftest import insert_channel
from .test_qa import write_jsonl
from .test_summarize import gemini_ok


class FakeTelegramResponse:
    def __init__(self, ok=True, result=None):
        self._ok = ok
        self._result = result or []

    def json(self):
        if self._ok:
            return {"ok": True, "result": self._result}
        return {"ok": False, "description": "boom"}


def make_update(chat_id, text, message_id=1, reply_to_message_id=None):
    message = {
        "message_id": message_id,
        "chat": {"id": chat_id},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": message_id, "message": message}


def test_is_allowed_chat():
    update = make_update("12345", "/status")
    assert bot_mod.is_allowed_chat(update, "12345") is True
    assert bot_mod.is_allowed_chat(update, "99999") is False


def test_parse_command():
    assert bot_mod.parse_command("/status") == ("/status", [])
    assert bot_mod.parse_command("/ask v1 what happened?") == ("/ask", ["v1", "what", "happened?"])
    assert bot_mod.parse_command("/retry@v1bot vid") == ("/retry", ["vid"])
    assert bot_mod.parse_command("plain text") == ("", ["plain", "text"])


def test_format_status(conn):
    insert_channel(conn, "UC1")
    conn.execute(
        "INSERT INTO videos (video_id, channel_id, state, discovered_at, updated_at) "
        "VALUES ('v1', 'UC1', ?, 'now', 'now')",
        (VideoState.DELIVERED.value,),
    )
    conn.execute("INSERT INTO runs (started_at, status, discovered) VALUES ('now', 'ok', 1)")
    conn.commit()
    text = bot_mod.format_status(conn)
    assert "delivered: 1" in text
    assert "Last run:" in text


def test_cmd_retry_resets_failed_video(conn):
    insert_channel(conn, "UC1")
    conn.execute(
        "INSERT INTO videos (video_id, channel_id, state, discovered_at, updated_at) "
        "VALUES ('v1', 'UC1', ?, 'now', 'now')",
        (VideoState.FAILED_PERMANENT.value,),
    )
    conn.commit()
    msg = bot_mod.cmd_retry(conn, "v1")
    assert "Reset v1" in msg
    row = conn.execute("SELECT state FROM videos WHERE video_id='v1'").fetchone()
    assert row["state"] == VideoState.NEEDS_TRANSCRIPT.value


def test_handle_update_ignores_unauthorized_chat(conn, config):
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        return FakeTelegramResponse()

    bot_mod.handle_update(
        make_update("999", "/status"),
        config,
        conn,
        "token",
        "12345",
        "gemini-key",
        post_fn=post,
    )
    assert sent == []


def test_handle_update_status_command(conn, config):
    config.secrets["TELEGRAM_BOT_TOKEN"] = "tok"
    config.secrets["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["text"])
        return FakeTelegramResponse()

    bot_mod.handle_update(
        make_update("12345", "/status"),
        config,
        conn,
        "tok",
        "12345",
        "gemini-key",
        post_fn=post,
    )
    assert sent and "Videos by state" in sent[0]


def test_format_run_result():
    from ytdigest import pipeline

    text = bot_mod.format_run_result(
        pipeline.RunResult(
            run_id=7,
            status="ok",
            discovered=2,
            summarized=1,
            failed=0,
            api_units=12,
        )
    )
    assert "Run #7 finished status=ok" in text
    assert "discovered=2 summarized=1" in text


def test_start_pipeline_run_sends_completion_message(config, monkeypatch):
    from ytdigest import pipeline

    bot_mod._run_in_progress = False
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["text"])
        return FakeTelegramResponse()

    def fake_run_pipeline(conn, config):
        return pipeline.RunResult(run_id=3, status="ok", discovered=1, summarized=1)

    monkeypatch.setattr("ytdigest.bot.pipeline.run_pipeline", fake_run_pipeline)
    msg = bot_mod.start_pipeline_run(config, "tok", "12345", post_fn=post)
    assert msg == "Pipeline run started…"

    for _ in range(50):
        if sent:
            break
        threading.Event().wait(0.05)
    assert any("Run #3 finished status=ok" in text for text in sent)
    bot_mod._run_in_progress = False


def test_start_pipeline_run_rejects_concurrent_start(config, monkeypatch):
    bot_mod._run_in_progress = True
    try:
        msg = bot_mod.start_pipeline_run(config, "tok", "12345")
    finally:
        bot_mod._run_in_progress = False
    assert msg == "A run is already in progress."


def test_handle_update_run_command(conn, config, monkeypatch):
    config.secrets["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["text"])
        return FakeTelegramResponse()

    monkeypatch.setattr(
        "ytdigest.bot.start_pipeline_run",
        lambda config, bot_token, chat_id, post_fn=None: "Pipeline run started…",
    )
    bot_mod.handle_update(
        make_update("12345", "/run"),
        config,
        conn,
        "tok",
        "12345",
        "gemini-key",
        post_fn=post,
    )
    assert sent == ["Pipeline run started…"]


def test_handle_update_reply_to_digest_message(conn, config, monkeypatch):
    config.secrets["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
    insert_channel(conn, "UC1", title="Chan")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, discovered_at, updated_at)
        VALUES ('v1', 'UC1', 'Video', 'delivered', 'now', 'now')
        """
    )
    conn.execute(
        "INSERT INTO deliveries (message_id, video_id, run_id, sent_at) VALUES ('55', 'v1', NULL, 'now')"
    )
    conn.commit()

    monkeypatch.setattr(
        "ytdigest.bot.qa.answer_question",
        lambda conn, config, video_id, question, api_key, post_fn=None: "The answer.",
    )

    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json)
        return FakeTelegramResponse()

    bot_mod.handle_update(
        make_update("12345", "What did they say?", message_id=10, reply_to_message_id=55),
        config,
        conn,
        "tok",
        "12345",
        "gemini-key",
        post_fn=post,
    )
    assert any(item.get("text") == "The answer." for item in sent)


def test_handle_update_reply_without_delivery_mapping(conn, config):
    config.secrets["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["text"])
        return FakeTelegramResponse()

    bot_mod.handle_update(
        make_update("12345", "question?", reply_to_message_id=999),
        config,
        conn,
        "tok",
        "12345",
        "gemini-key",
        post_fn=post,
    )
    assert "Reply to a specific digest video message" in sent[0]


def test_run_bot_stops_on_event(conn, config):
    config.secrets["TELEGRAM_BOT_TOKEN"] = "tok"
    config.secrets["TELEGRAM_ALLOWED_CHAT_ID"] = "12345"
    config.secrets["GEMINI_API_KEY"] = "gemini"
    stop = threading.Event()
    stop.set()

    def get_fn(url, params=None, timeout=None):
        return FakeTelegramResponse(result=[])

    bot_mod.run_bot(config, conn, get_fn=get_fn, stop_event=stop)


def test_cmd_ask_cli(tmp_path, capsys):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"data_dir: {tmp_path / 'data'}\n")
    (tmp_path / ".env").write_text("YOUTUBE_API_KEY=test\nGEMINI_API_KEY=gem\n")
    config = cli.load_config(config_path)
    conn = db.init_db(config.db_path)
    insert_channel(conn, "UC1")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, state, discovered_at, updated_at)
        VALUES ('v1', 'UC1', 'T', 'delivered', 'now', 'now')
        """
    )
    conn.commit()
    write_jsonl(config.transcripts_dir / "UC1" / "v1.jsonl", [{"t": 0, "text": "content"}])
    conn.close()

    import ytdigest.qa as qa_mod

    original = qa_mod.answer_question

    def fake_answer(conn, config, video_id, question, api_key, post_fn=None):
        return "CLI answer."

    qa_mod.answer_question = fake_answer
    try:
        args = argparse.Namespace(config=str(config_path), video_id="v1", question="What?")
        cli.cmd_ask(args)
    finally:
        qa_mod.answer_question = original

    assert "CLI answer." in capsys.readouterr().out


def test_run_bot_requires_credentials(conn, config):
    config.secrets["TELEGRAM_BOT_TOKEN"] = ""
    with pytest.raises(bot_mod.BotError):
        bot_mod.run_bot(config, conn, stop_event=threading.Event())
