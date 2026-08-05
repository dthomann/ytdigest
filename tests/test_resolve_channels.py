import pytest

from scripts.resolve_channels import extract_handle_or_id, is_channel_id, parse_takeout_csv, resolve_file

from .conftest import FIXTURES_DIR


def test_is_channel_id():
    assert is_channel_id("UCnormal0000000000000000")
    assert not is_channel_id("@somehandle")
    assert not is_channel_id("not-a-channel-id")


def test_extract_from_channel_url():
    cid, handle = extract_handle_or_id("http://www.youtube.com/channel/UCnormal0000000000000000")
    assert cid == "UCnormal0000000000000000"
    assert handle is None


def test_extract_from_bare_id():
    cid, handle = extract_handle_or_id("UCnormal0000000000000000")
    assert cid == "UCnormal0000000000000000"


def test_extract_handle_from_url():
    cid, handle = extract_handle_or_id("https://www.youtube.com/@example")
    assert cid is None
    assert handle == "@example"


def test_extract_bare_handle():
    cid, handle = extract_handle_or_id("@example")
    assert handle == "@example"


def test_parse_takeout_csv():
    resolved = parse_takeout_csv(FIXTURES_DIR / "subscriptions_sample.csv")
    assert len(resolved) == 3
    ids = {r.channel_id for r in resolved}
    assert "UCnormal0000000000000000" in ids


def test_resolve_file_detects_takeout_csv():
    resolved = resolve_file(FIXTURES_DIR / "subscriptions_sample.csv")
    assert len(resolved) == 3


def test_resolve_file_handle_without_api_key_raises(tmp_path):
    f = tmp_path / "channels.txt"
    f.write_text("@somehandle\n")
    with pytest.raises(ValueError, match="Cannot resolve handle"):
        resolve_file(f, api_key=None)
