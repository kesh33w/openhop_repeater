from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from openhop_core.protocol import Packet
from repeater.main import RepeaterDaemon
from repeater.packet_router import PacketRouter


def daemon():
    config = {
        "repeater": {"node_name": "test"},
        "radio": {"frequency": 917450000, "spreading_factor": 9},
        "radios": [
            {"id": "a", "radio": {"frequency": 917450000, "spreading_factor": 9}},
            {"id": "b", "radio": {"frequency": 917550000, "spreading_factor": 7}},
        ],
    }
    return RepeaterDaemon(config, radio=None)


def test_home_reporting_and_unknown_radio_rejection():
    d = daemon()
    assert d._get_companion_radio_settings("b")["spreading_factor"] == 7
    assert d._get_companion_radio_settings("b")["frequency"] == 917550000
    with pytest.raises(ValueError):
        d._get_companion_radio_settings("missing")
    with pytest.raises(ValueError):
        d._configure_companion_routing(SimpleNamespace(), {"receive_radio_ids": ["missing"]})


@pytest.mark.asyncio
async def test_decoded_and_raw_feeds_filter_radio_and_exclude_origin():
    d = daemon()
    a = SimpleNamespace(_receive_radio_ids=frozenset(["a"]))
    b = SimpleNamespace(_receive_radio_ids=frozenset(["b"]))
    legacy = SimpleNamespace()
    d.companion_bridges = {1: a, 2: b, 3: legacy}
    router = PacketRouter(d)
    router._policy_blocks_companion = lambda p, m: False
    p = Packet()
    p._injected_origin_hash = "0x03"
    assert router._companion_bridges_for_packet(p, {"rx_radio_id": "a"}) == {1: a}
    assert router._companion_bridges_for_packet(p, {}) == {}
    fs = [
        SimpleNamespace(bridge=x, companion_hash=f"0x{i:02x}", push_rx_raw=Mock())
        for i, x in d.companion_bridges.items()
    ]
    d.companion_frame_servers = fs
    await d._on_raw_rx_for_companions(b"test", -90, 7, rx_radio_id="b")
    assert [x.push_rx_raw.call_count for x in fs] == [0, 1, 1]


@pytest.mark.asyncio
async def test_confirmed_crossing_delivers_only_opted_in_destination_without_retransmit():
    d = daemon()
    a = SimpleNamespace(_receive_radio_ids=frozenset(["a"]))
    b = SimpleNamespace(_receive_radio_ids=frozenset(["b"]))
    legacy = SimpleNamespace()
    d.companion_bridges = {1: a, 2: b, 3: legacy}
    router = PacketRouter(d)
    router._policy_blocks_companion = lambda p, m: False
    router._fan_out_to_bridges = AsyncMock()
    d._on_raw_rx_for_companions = AsyncMock()
    source = Packet()
    source._injected_origin_hash = "0x01"
    target = Packet()
    target.header = 0x15
    target.payload = bytearray(b"test")
    target.payload_len = 4
    target._rssi = -90
    target._snr = 28
    await router.deliver_bridge_egress(target, source, "b")
    assert router._fan_out_to_bridges.call_args.args[1] == {2: b}
    observed = router._fan_out_to_bridges.call_args.args[0]
    assert observed.rssi == 0 and observed.snr == 0 and observed._rx_radio_id == "b"
    assert d._on_raw_rx_for_companions.call_args.kwargs["rx_radio_id"] == "b"
