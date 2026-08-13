from ytdigest import channels as channels_mod


def test_display_source_maps_legacy_values():
    assert channels_mod.display_source("subscribed") == "subscribed"
    assert channels_mod.display_source("sync") == "subscribed"
    assert channels_mod.display_source("manual") == "manual"
    assert channels_mod.display_source("import") == "manual"
    assert channels_mod.display_source(None) == "manual"


def test_list_channels_puts_manual_first(conn):
    channels_mod.add_channel(conn, "UCsub", title="Alpha Sub", source="subscribed")
    channels_mod.add_channel(conn, "UCman", title="Zeta Manual", source="manual")
    channels_mod.add_channel(conn, "UCold", title="Beta Sync", source="sync")
    channels_mod.add_channel(conn, "UCimp", title="Gamma Import", source="import")

    listed = channels_mod.list_channels(conn)
    assert [ch.channel_id for ch in listed] == ["UCimp", "UCman", "UCsub", "UCold"]
    assert [ch.display_source for ch in listed] == ["manual", "manual", "subscribed", "subscribed"]
