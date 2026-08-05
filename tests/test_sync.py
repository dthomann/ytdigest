from ytdigest import channels as channels_mod, sync
from ytdigest.util import utcnow_iso


def test_compare_adds_new_youtube_subs(conn):
    channels_mod.add_channel(conn, "UCexisting", title="Existing", source="manual", enable=True)
    channels_mod.add_channel(conn, "UCdisabled", title="Disabled", source="manual", enable=False)

    result = sync.compare_subscriptions(
        conn,
        {"UCexisting", "UCnew"},
        {"UCnew": "New Channel"},
    )

    assert len(result.added) == 1
    assert result.added[0].channel_id == "UCnew"
    assert len(result.suggested_removals) == 0
    assert len(result.unchanged_disabled) == 1
    assert result.unchanged_disabled[0].channel_id == "UCdisabled"


def test_compare_suggests_removal_for_enabled_not_on_youtube(conn):
    channels_mod.add_channel(conn, "UCgone", title="Gone", source="sync", enable=True)
    channels_mod.add_channel(conn, "UCdisabled", title="Disabled", source="sync", enable=False)

    result = sync.compare_subscriptions(conn, set())

    assert result.added == []
    assert len(result.suggested_removals) == 1
    assert result.suggested_removals[0].channel_id == "UCgone"
    assert len(result.unchanged_disabled) == 1


def test_apply_sync_additions(conn):
    from ytdigest.channels import ChannelRow

    to_add = [
        ChannelRow(
            channel_id="UCnew",
            title="New",
            handle=None,
            enabled=True,
            source="sync",
            added_at=utcnow_iso(),
            consecutive_errors=0,
            last_error=None,
        )
    ]
    count = sync.apply_sync_additions(conn, to_add)
    assert count == 1
    ch = channels_mod.get_channel(conn, "UCnew")
    assert ch is not None
    assert ch.enabled is True
    assert ch.source == "sync"
