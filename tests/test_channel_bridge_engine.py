import copy
from unittest.mock import AsyncMock

import pytest

from repeater.channel_bridge import BridgeSettings
from repeater.config import validate_fabric_config
from tests import test_fabric_fanout as fabric_tests
from tests.test_channel_bridge import C1, C2, packet


def config(tmp_path):
    return {
        "enabled": True,
        "channels": {C1.name: C1.key.hex(), C2.name: C2.key.hex()},
        "mappings": [
            {
                "source_radio": "local",
                "target_radio": "link",
                "source_channel": C1.name,
                "target_channel": C2.name,
            },
            {
                "source_radio": "link",
                "target_radio": "local",
                "source_channel": C2.name,
                "target_channel": C1.name,
            },
        ],
        "echo_database": str(tmp_path / "echo.sqlite"),
        "origin_home": "local",
    }


@pytest.fixture
def rig_factory(tmp_path, monkeypatch):
    original = fabric_tests._config
    rigs = []

    def create(**settings):
        bridge = config(tmp_path)
        bridge.update(settings)

        def configured(fabric, repeater):
            cfg = original(fabric, repeater)
            cfg["radios"] = [{"id": "local"}, {"id": "link"}]
            cfg["channel_bridge"] = bridge
            return cfg

        monkeypatch.setattr(fabric_tests, "_config", configured)
        rig = fabric_tests._Rig()
        rigs.append(rig)
        return rig

    yield create
    for rig in rigs:
        rig.handler.close_channel_bridge()


@pytest.mark.asyncio
@pytest.mark.parametrize("home,channel,other", [("local", C1, "link"), ("link", C2, "local")])
async def test_native_scheduler_translates_after_hop_update(rig_factory, home, channel, other):
    rig = rig_factory()
    source = packet(channel)
    assert await rig.handler(source, fabric_tests._rx(home))
    assert rig.air.order == [home, other]
    outgoing = rig.sent_packets[1][1]
    assert outgoing.path == bytearray([0x11, 0x22, fabric_tests.LOCAL_HASH])
    assert outgoing.payload != source.payload
    assert rig.sent_packets[0][1].payload == source.payload
    rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    assert [row["raw_packet"] for row in rows] == [frame.hex() for _, frame in rig.air.frames]
    assert all(row["success"] for row in rows)


@pytest.mark.asyncio
async def test_crossing_drop_preserves_native_repeat(rig_factory):
    rig = rig_factory(crossing_policy={"enabled": True, "default_action": "drop"})
    assert await rig.handler(packet(), fabric_tests._rx("local"))
    assert rig.air.order == ["local"]


@pytest.mark.asyncio
async def test_local_drop_preserves_crossing(rig_factory):
    rig = rig_factory(local_policy={"enabled": True, "default_action": "drop"})
    assert await rig.handler(packet(), fabric_tests._rx("local"))
    assert rig.air.order == ["link"]


@pytest.mark.asyncio
async def test_failed_modem_is_not_success_and_releases_ledger(rig_factory):
    rig = rig_factory()
    rig.link.send = AsyncMock(return_value=False)
    p = packet()
    assert await rig.handler(p, fabric_tests._rx("local"))
    rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    assert [row["success"] for row in rows] == [True, False]
    assert not await rig.handler.bridge_echoes.contains(p)
    assert rig.air.order == ["local"]


@pytest.mark.asyncio
async def test_echo_suppressed_across_restart(rig_factory):
    first = rig_factory()
    assert await first.handler(packet(), fabric_tests._rx("local"))
    translated = copy.deepcopy(first.sent_packets[1][1])
    # Remove the path to prove persistence, rather than own-hop detection, stops it.
    translated.path, translated.path_len = bytearray(), 0
    first.handler.close_channel_bridge()
    second = rig_factory()
    assert not await second.handler(translated, fabric_tests._rx("link"))
    assert not second.air.frames


@pytest.mark.asyncio
async def test_origin_home_is_explicit_and_missing_home_fails_closed(rig_factory):
    rig = rig_factory()
    rig.handler.channel_bridge.origin_home = None
    assert not await rig.handler(packet(), {}, local_transmission=True)
    assert not rig.air.frames
    assert await rig.handler(packet(), {"home_radio_id": "link"}, local_transmission=True)
    # Source channel is test-a, which has no mapping out of link.
    assert rig.air.order == ["link"]


@pytest.mark.asyncio
async def test_queue_full_does_not_disable_local_repeat(rig_factory):
    rig = rig_factory(max_pending_crossings=1)
    rig.handler._bridge_pending = 1
    assert await rig.handler(packet(), fabric_tests._rx("local"))
    assert rig.air.order == ["local"]
    rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    assert rows[1]["failure_reason"] == "Bridge queue full"


@pytest.mark.asyncio
async def test_expired_sends_never_reach_modem(rig_factory):
    rig = rig_factory(queue_ttl_seconds=1)
    rig.handler._calculate_tx_delay = lambda *args: 1.01
    assert not await rig.handler(packet(), fabric_tests._rx("local"))
    assert not rig.air.frames
    assert not await rig.handler.bridge_echoes.contains(packet())


@pytest.mark.asyncio
async def test_exception_keeps_uncertain_reservation(rig_factory):
    rig = rig_factory()
    rig.link.send = AsyncMock(side_effect=OSError("serial interrupted"))
    assert await rig.handler(packet(), fabric_tests._rx("local"))
    assert await rig.handler.bridge_echoes.contains(packet())
    rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    assert not rows[1]["success"]


def test_bad_configuration_fails_before_hardware(tmp_path):
    cfg = {
        "radios": [{"id": "local"}, {"id": "link"}],
        "channel_bridge": config(tmp_path),
    }
    assert BridgeSettings.from_config(cfg)
    for field, value in [
        ("enabled", "false"),
        ("echo_database", "relative.db"),
        ("origin_home", "missing"),
        ("queue_ttl_seconds", 999),
        ("unexpected", True),
    ]:
        invalid = copy.deepcopy(cfg)
        invalid["channel_bridge"][field] = value
        with pytest.raises(ValueError):
            validate_fabric_config(invalid)


def test_actual_outgoing_bytes_persist_and_old_schema_migrates(tmp_path):
    import sqlite3
    from repeater.data_acquisition.sqlite_handler import SQLiteHandler

    store = SQLiteHandler(tmp_path)
    with sqlite3.connect(store.sqlite_path) as db:
        db.execute("ALTER TABLE packet_egress DROP COLUMN raw_packet")
        db.execute("ALTER TABLE packet_egress DROP COLUMN failure_reason")
    store = SQLiteHandler(tmp_path)
    store.store_packet_egress(
        1,
        123.0,
        [
            {
                "radio_id": "link",
                "success": False,
                "raw_packet": "010203",
                "failure_reason": "unconfirmed",
            }
        ],
    )
    with sqlite3.connect(store.sqlite_path) as db:
        assert db.execute(
            "SELECT raw_packet, failure_reason, success FROM packet_egress"
        ).fetchone() == ("010203", "unconfirmed", 0)

@pytest.mark.asyncio
async def test_confirmed_crossing_observer_is_not_local_repeat_and_cannot_change_tx_success(rig_factory):
    rig = rig_factory()
    observer = AsyncMock(side_effect=RuntimeError('local observer unavailable'))
    rig.handler.bridge_egress_callback = observer
    assert await rig.handler(packet(), fabric_tests._rx('local'))
    assert observer.await_count == 1
    assert observer.call_args.args[2] == 'link'
    assert rig.air.order == ['local', 'link']

@pytest.mark.asyncio
async def test_denied_crossing_has_no_destination_companion_observation(rig_factory):
    rig=rig_factory(crossing_policy={'enabled':True,'default_action':'drop'})
    rig.handler.bridge_egress_callback=AsyncMock()
    assert await rig.handler(packet(),fabric_tests._rx('local'))
    rig.handler.bridge_egress_callback.assert_not_called()
