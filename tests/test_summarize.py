import pytest

from ytdigest import summarize as sm


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


def gemini_ok(text="A concise summary paragraph."):
    def post(url, params=None, json=None, timeout=None):
        return FakeResponse(
            200, {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        )

    return post


def test_build_prompt_bans_openers_and_includes_metadata():
    prompt = sm.build_prompt(
        "some transcript text", "Clickbait Title!!", "Some Channel", "en", 60, 100
    )
    assert "In this video" in prompt  # named in the ban instruction
    assert "Clickbait Title!!" in prompt
    assert "Some Channel" in prompt
    assert "60-100 words" in prompt
    assert "in en" in prompt


def test_summarize_transcript_happy_path(config):
    post = gemini_ok("Real content summary here.")
    result = sm.summarize_transcript(
        "a" * 500, "Title", "Channel", config, api_key="key", post_fn=post
    )
    assert result == "Real content summary here."


def test_summarize_transcript_retries_on_429_then_succeeds(config, monkeypatch):
    monkeypatch.setattr("ytdigest.util.time.sleep", lambda *_: None)
    calls = {"n": 0}

    def post(url, params=None, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return FakeResponse(429, text="rate limited")
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    result = sm.summarize_transcript("text", "T", "C", config, api_key="key", post_fn=post)
    assert result == "ok"
    assert calls["n"] == 3


def test_summarize_transcript_gives_up_after_3_attempts(config, monkeypatch):
    monkeypatch.setattr("ytdigest.util.time.sleep", lambda *_: None)

    def post(url, params=None, json=None, timeout=None):
        return FakeResponse(500, text="server error")

    with pytest.raises(sm.GeminiError):
        sm.summarize_transcript("text", "T", "C", config, api_key="key", post_fn=post)


def test_summarize_transcript_non_retryable_error_raises_immediately(config):
    calls = {"n": 0}

    def post(url, params=None, json=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(400, text="bad request")

    with pytest.raises(RuntimeError):
        sm.summarize_transcript("text", "T", "C", config, api_key="key", post_fn=post)
    assert calls["n"] == 1  # no retries for a non-retryable status


def test_summarize_transcript_map_reduce_for_long_input(config, monkeypatch):
    config.values["max_input_chars"] = 100
    calls = []

    def post(url, params=None, json=None, timeout=None):
        prompt = json["contents"][0]["parts"][0]["text"]
        calls.append(prompt)
        if "This is one part of a longer video transcript" in prompt:
            return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "partial fact."}]}}]})
        return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "final summary."}]}}]})

    long_text = "word " * 100  # > 100 chars, forces map-reduce
    result = sm.summarize_transcript(long_text, "T", "C", config, api_key="key", post_fn=post)
    assert result == "final summary."
    assert len(calls) >= 2  # at least one chunk call + the final combine call
