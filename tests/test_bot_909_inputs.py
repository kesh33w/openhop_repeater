"""Same-band bot ears share delivery while other bands stay excluded."""

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from repeater.packet_router import PacketRouter
from repeater.main import RepeaterDaemon
from tests.test_network_topology import Rig, config
from tests.test_channel_bridge import packet


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["local", "omni909"])
async def test_both_909_orders_and_concurrent_copies_deliver_once(first):
    rig = Rig(config(receiver=True))
    handler = AsyncMock()
    handler.network_topology = rig.topology
    daemon = SimpleNamespace(
        repeater_handler=handler,
        dispatcher=SimpleNamespace(packet_filter=SimpleNamespace(window_seconds=300)),
    )
    router = PacketRouter(daemon)
    delivered = []

    async def route(p):
        await asyncio.sleep(0.01)
        delivered.append(p._rx_radio_id)
        return True

    router._route_packet_on_radio = route
    p = packet()
    p._rx_radio_id = first
    q = copy.copy(p)
    q._rx_radio_id = "omni909" if first == "local" else "local"
    await asyncio.gather(router._route_packet(p), router._route_packet(q))
    await router._route_packet(q)
    assert delivered == [first]
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_allowlist_is_same_for_raw_decoded_and_excludes_910():
    bot = SimpleNamespace(_receive_radio_ids=frozenset(["host-rf", "v3-909-shadow"]))
    daemon = SimpleNamespace(companion_bridges={1: bot}, repeater_handler=SimpleNamespace())
    router = PacketRouter(daemon)
    router._policy_blocks_companion = lambda p, m: False
    fs = SimpleNamespace(bridge=bot, companion_hash="0x01", push_rx_raw=Mock())
    daemon.companion_frame_servers = [fs]
    for rid in ["host-rf", "v3-909-shadow", "bay", "v3-910-shadow", "rak910-shadow"]:
        allowed = rid in bot._receive_radio_ids
        assert bool(router._companion_bridges_for_packet(packet(), {"rx_radio_id": rid})) == allowed
        fs.push_rx_raw.reset_mock()
        await RepeaterDaemon._on_raw_rx_for_companions(daemon, b"frame", -90, 5, rx_radio_id=rid)
        assert fs.push_rx_raw.called == allowed
    p = packet()
    p._injected_origin_hash = "0x01"
    assert router._companion_bridges_for_packet(p, {"rx_radio_id": "host-rf"}) == {}
    router._policy_blocks_companion = lambda p, m: True
    assert router._companion_bridges_for_packet(packet(), {"rx_radio_id": "v3-909-shadow"}) == {}
