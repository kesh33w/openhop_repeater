"""Reviewer regressions through inbound Core parsing/dedup and the real router.

Only the physical radios are fake. The fallback calls the real route handler
synchronously rather than enqueueing it, keeping assertions deterministic.
"""

import copy

# ruff: noqa: F811
from types import SimpleNamespace

import pytest
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_MULTIPART,
    PAYLOAD_TYPE_PATH,
    PAYLOAD_TYPE_REQ,
    PAYLOAD_TYPE_TXT_MSG,
    ROUTE_TYPE_DIRECT,
)

from repeater.handler_helpers.trace import TraceHelper
from tests.test_bridge_protocols import CROSSING_KEY, KEY, PROTOCOLS, peer, trace
from tests.test_channel_bridge_engine import rig_factory  # noqa: F401
from tests.test_direct_bridge import ROUTES
from tests.test_fabric_fanout import LOCAL_HASH, _router_for


def wire(rig):
    router = _router_for(rig)
    identity = SimpleNamespace(get_public_key=lambda: KEY)
    rig.dispatcher.local_identity = identity
    router.daemon.trace_helper = TraceHelper(
        LOCAL_HASH, rig.handler, router.inject_packet, local_identity=identity
    )
    rig.dispatcher.register_fallback_handler(router._route_packet)
    return router


async def receive(rig, packet, home):
    await rig.dispatcher._process_received_packet(packet.write_to(), -70, 3, rx_radio_id=home)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", [PAYLOAD_TYPE_REQ, PAYLOAD_TYPE_ACK, PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_PATH]
)
@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
@pytest.mark.parametrize("native", [False, True])
async def test_core_rx_routes_native_local_and_virtual_cross(
    rig_factory, kind, width, home, other, native
):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    wire(rig)
    packet = peer(kind, width=width, direct=True)
    if native:
        packet.path = bytearray(KEY[:width])
    await receive(rig, packet, home)
    assert rig.air.order == [home if native else other]
    assert rig.sent_packets[-1][1].path == b""
    assert rig.sent_packets[-1][1].payload == packet.payload
    # A received copy of the emitted zero-hop packet must never be repeated.
    await receive(rig, copy.deepcopy(rig.sent_packets[-1][1]), other)
    assert len(rig.air.frames) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4, 8])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
@pytest.mark.parametrize("native", [False, True])
async def test_core_rx_trace_uses_payload_hop_and_one_snr(rig_factory, width, home, other, native):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    wire(rig)
    packet = trace(width)
    if native:
        packet.payload[9 : 9 + width] = KEY[:width]
    await receive(rig, packet, home)
    assert rig.air.order == [home if native else other]
    outgoing = rig.sent_packets[-1][1]
    assert outgoing.path == bytes([12])
    assert outgoing.payload == packet.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3])
async def test_core_rx_virtual_multipart_ack(rig_factory, width):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    wire(rig)
    packet = peer(PAYLOAD_TYPE_ACK, width=width, direct=True)
    packet.header = PAYLOAD_TYPE_MULTIPART << 2 | ROUTE_TYPE_DIRECT
    packet.payload = bytearray([0x10 | PAYLOAD_TYPE_ACK]) + packet.payload
    packet.payload_len = len(packet.payload)
    await receive(rig, packet, "local")
    assert rig.air.order == ["link"]
    outgoing = rig.sent_packets[-1][1]
    assert outgoing.get_payload_type() == PAYLOAD_TYPE_ACK
    assert outgoing.path == b""
    assert outgoing.payload == packet.payload[1:]


@pytest.mark.asyncio
async def test_core_rx_overheard_longer_direct_does_not_suppress_virtual_hop(rig_factory):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    wire(rig)
    packet = peer(direct=True)
    overheard = copy.deepcopy(packet)
    overheard.path = bytearray([0x37]) + overheard.path
    overheard.path_len += 1
    await receive(rig, overheard, "local")
    assert not rig.air.frames
    await receive(rig, packet, "local")
    assert rig.air.order == ["link"]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4, 8])
@pytest.mark.parametrize("home", ["local", "link"])
@pytest.mark.parametrize("second", [KEY, CROSSING_KEY])
@pytest.mark.parametrize("progress", [0, 2])
async def test_core_rx_rejects_consecutive_trace_crossing_hops(
    rig_factory, width, home, second, progress
):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    wire(rig)
    packet = trace(width, progress=progress)
    offset = 9 + progress * width
    packet.payload[offset : offset + 2 * width] = CROSSING_KEY[:width] + second[:width]
    original = packet.write_to()
    await receive(rig, packet, home)
    assert not rig.air.frames
    assert rig.handler._bridge_pending == 0
    assert packet.write_to() == original
    assert rig.handler.storage.record_packet.call_args.args[0]["drop_reason"] == (
        "Consecutive bridge route hops"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4, 8])
@pytest.mark.parametrize("home", ["local", "link"])
@pytest.mark.parametrize("second", [KEY, CROSSING_KEY])
async def test_core_rx_native_trace_short_prefix_collisions_remain_local(
    rig_factory, width, home, second
):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    wire(rig)
    packet = trace(width)
    packet.payload[9 : 9 + 2 * width] = KEY[:width] + second[:width]
    await receive(rig, packet, home)
    assert rig.air.order == [home]
    assert rig.sent_packets[-1][1].path == bytes([12])
    assert rig.sent_packets[-1][1].payload == packet.payload
