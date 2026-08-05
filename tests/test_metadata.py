import pytest

from ytdigest.metadata import (
    QuotaExceededError,
    apply_metadata,
    fetch_all_metadata,
)

from .conftest import insert_channel, load_fixture_json


def make_fetch(fixture_by_id: dict):
    def fetch(ids, api_key):
        items = []
        for vid in ids:
            fx = fixture_by_id.get(vid)
            if fx:
                items.append(load_fixture_json(fx)["items"][0])
        return {"items": items}

    return fetch


def test_fetch_all_metadata_batches_and_flags_missing():
    fetch = make_fetch({"vid_normal_001": "videos_normal.json"})
    items, missing, units = fetch_all_metadata(
        ["vid_normal_001", "vid_deleted_999"], "key", fetch_fn=fetch
    )
    assert len(items) == 1
    assert items[0]["video_id"] == "vid_normal_001"
    assert missing == {"vid_deleted_999"}
    assert units == 1


def test_quota_abort_at_threshold():
    fetch = make_fetch({})
    ids = [f"v{i}" for i in range(51)]  # forces 2 batches
    with pytest.raises(QuotaExceededError):
        fetch_all_metadata(
            ids, "key", fetch_fn=fetch, quota_used_today=9899, quota_daily=10000,
            quota_warn_fraction=0.9,
        )


def test_apply_metadata_marks_missing_as_failed_permanent(conn):
    insert_channel(conn, "UCnormal0000000000000000")
    conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, state, discovered_at, updated_at)
        VALUES ('vid_deleted_999', 'UCnormal0000000000000000', 'discovered', 'now', 'now')
        """
    )
    conn.commit()
    apply_metadata(conn, [], {"vid_deleted_999"})
    row = conn.execute("SELECT * FROM videos WHERE video_id = 'vid_deleted_999'").fetchone()
    assert row["state"] == "failed_permanent"
    assert "deleted or private" in row["last_error"]
