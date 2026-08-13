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
    channels_mod.add_channel(conn, "UCgone", title="Gone", source="subscribed", enable=True)
    channels_mod.add_channel(conn, "UCdisabled", title="Disabled", source="subscribed", enable=False)

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
            source="subscribed",
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
    assert ch.source == "subscribed"


def test_update_subscription_sources(conn):
    channels_mod.add_channel(conn, "UCkeep", title="Keep", source="manual", enable=True)
    channels_mod.add_channel(conn, "UCdrop", title="Drop", source="subscribed", enable=True)
    channels_mod.add_channel(conn, "UCoff", title="Off", source="import", enable=False)

    sync.update_subscription_sources(conn, {"UCkeep", "UCoff"})

    assert channels_mod.get_channel(conn, "UCkeep").source == "subscribed"
    assert channels_mod.get_channel(conn, "UCdrop").source == "manual"
    assert channels_mod.get_channel(conn, "UCoff").source == "subscribed"


def test_update_subscription_sources_empty_marks_all_manual(conn):
    channels_mod.add_channel(conn, "UCold", title="Old", source="sync", enable=True)

    sync.update_subscription_sources(conn, set())

    assert channels_mod.get_channel(conn, "UCold").source == "manual"


def test_update_subscription_sources_handles_many_youtube_ids(conn):
    channels_mod.add_channel(conn, "UC0001", title="Keep", source="manual")
    channels_mod.add_channel(conn, "UClocal", title="Local only", source="subscribed")
    yt_ids = {f"UC{i:04d}" for i in range(1200)}

    sync.update_subscription_sources(conn, yt_ids)

    assert channels_mod.get_channel(conn, "UC0001").source == "subscribed"
    assert channels_mod.get_channel(conn, "UClocal").source == "manual"
