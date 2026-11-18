"""Active ears must not remove the directional radio's existing native repeat.

Destination-specific admission preserves forwarding across unlike ears while
local delivery remains once per network.
No test opens hardware or transmits RF.
"""

import copy
import asyncio

import pytest

from openhop_core.protocol.constants import PAYLOAD_TYPE_TXT_MSG
from repeater.channel_bridge import BridgeSettings
from tests import test_fabric_fanout as f
from tests.test_channel_bridge import C1, C2, packet
from tests.test_network_topology import Rig, config


def active_rig(tmp_path):
    cfg = config(tmp_path, receiver=True, repeat=False)
    cfg["channel_bridge"].update(
        protocols={"discovery": False, "repeater_admin": False, "trace": False},
    )
    rak = copy.deepcopy(cfg["radios"][3])
    rak.update(id="rak910-shadow", role="shadow")
    cfg["radios"].append(rak)
    rig = Rig(cfg)
    # The shared rig uses a one-byte identity; distinct crossing routes require
    # a full public key while keeping its first routing byte unchanged.
    cfg["channel_bridge"]["peer_routing"] = "distinct"
    rig.handler.local_hash_bytes = bytes([f.LOCAL_HASH]) + bytes(31)
    rig.handler.channel_bridge = BridgeSettings.from_config(cfg)
    rig.handler.channel_bridge.bind_routing_identity(rig.handler.local_hash_bytes)
    return rig


def incoming(kind, channel, receiver):
    if kind == "native_direct":
        return f._direct(
            payload=b"opaque native directed message",
            payload_type=PAYLOAD_TYPE_TXT_MSG,
            path=bytes([f.LOCAL_HASH, 0xCC]),
            rx=receiver,
        )
    result = packet(channel)
    result._rx_radio_id = receiver
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "home,ear,other,channel", [("local", "omni909", "link", C1), ("link", "omni910", "local", C2)]
)
@pytest.mark.parametrize("kind", ["mapped", "unmapped", "native_direct"])
@pytest.mark.parametrize("receiver_first", [True, False])
@pytest.mark.parametrize("concurrent", [True, False])
async def test_dispatcher_router_preserves_directional_destinations_regardless_of_ear_order(
    tmp_path, home, ear, other, channel, kind, receiver_first, concurrent
):
    rig = active_rig(tmp_path)
    try:
        if kind == "unmapped":
            # The other network's channel is not a mapped source on this network.
            channel = C2 if channel is C1 else C1
        source = incoming(kind, channel, ear)
        router = f._router_for(rig)
        rig.dispatcher.register_handler(source.get_payload_type(), router._route_packet)
        order = (ear, home) if receiver_first else (home, ear)

        async def receive(rid):
            await rig.dispatcher._process_received_packet(
                source.write_to(), -90, 8, rx_radio_id=rid
            )

        if concurrent:
            await asyncio.gather(*(receive(rid) for rid in order))
        else:
            for rid in order:
                await receive(rid)
        expected = sorted([home, other] if kind == "mapped" else [home])
        assert sorted(rig.air.order) == expected
        for _, raw in rig.air.frames:
            outgoing = f.Packet()
            outgoing.read_from(raw)
            expected_path = (
                bytes([0xCC]) if kind == "native_direct" else bytes([0x11, 0x22, f.LOCAL_HASH])
            )
            assert bytes(outgoing.path) == expected_path
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("clear_seen", [False, True])
async def test_receiver_crossing_does_not_consume_later_native_repeat_in_engine(
    tmp_path, clear_seen
):
    rig = active_rig(tmp_path)
    try:
        receiver_packet = incoming("mapped", C1, "omni909")
        assert await rig.handler(receiver_packet, f._rx("omni909"))
        assert rig.air.order == ["link"]
        if clear_seen:
            # Isolate the durable echo ledger from the in-memory handler cache.
            rig.handler.seen_packets.clear()
        directional = incoming("mapped", C1, "local")
        await rig.handler(directional, f._rx("local"))
        assert sorted(rig.air.order) == ["link", "local"]
    finally:
        rig.close()
