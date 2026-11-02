"""Opaque, explicit-path transit is independent of group channel translation."""

# ruff: noqa: F811
import copy
from unittest.mock import AsyncMock

import pytest
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_CONTROL,
    PAYLOAD_TYPE_MULTIPART,
    PAYLOAD_TYPE_PATH,
    PAYLOAD_TYPE_TXT_MSG,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
)
from openhop_core.protocol.packet_utils import PathUtils

from repeater.channel_bridge import BridgeSettings
from tests.test_channel_bridge_engine import rig_factory  # noqa: F401
from tests.test_fabric_fanout import LOCAL_HASH, _direct, _rx

ROUTES = [
    {"source_radio": "local", "target_radio": "link"},
    {"source_radio": "link", "target_radio": "local"},
]


def direct(width=1, kind=PAYLOAD_TYPE_TXT_MSG, *, suffix=True, payload=b"opaque ciphertext"):
    prefix = bytes([LOCAL_HASH, 0x12, 0x34])[:width]
    path = prefix + (bytes([0xCC]) * width if suffix else b"")
    p = _direct(payload=payload, path=path, payload_type=kind)
    p.path_len = PathUtils.encode_path_len(width, 2 if suffix else 1)
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
async def test_direct_ciphertext_crosses_only_after_consuming_own_hop(
    rig_factory, width, home, other
):
    rig = rig_factory(direct_routes=ROUTES, channels={}, mappings=[])
    rig.handler.local_hash_bytes = bytes([LOCAL_HASH, 0x12, 0x34])
    callback = rig.handler.bridge_egress_callback = AsyncMock()
    source = direct(width)
    original = source.write_to()
    assert await rig.handler(source, _rx(home))
    assert rig.air.order == [other]
    out = rig.sent_packets[0][1]
    assert out.payload == source.payload and out.header == source.header
    assert out.path == bytes([0xCC]) * width
    assert out.path_len == PathUtils.encode_path_len(width, 1)
    assert source.write_to() == original
    callback.assert_not_awaited()
    assert await rig.handler.bridge_echoes.contains(source)
    assert not await rig.handler(copy.deepcopy(source), _rx(home))
    assert rig.air.order == [other]


@pytest.mark.asyncio
async def test_reverse_ack_with_last_hop_consumed(rig_factory):
    rig = rig_factory(direct_routes=ROUTES)
    p = direct(kind=PAYLOAD_TYPE_ACK, suffix=False, payload=b"ack1\x00\x19")
    assert await rig.handler(p, _rx("link"))
    out = rig.sent_packets[0][1]
    assert rig.air.order == ["local"] and out.path == b""
    assert out.payload == p.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("multi_acks", [0, 1])
@pytest.mark.parametrize("multipart", [False, True])
async def test_native_ack_redundancy_stays_on_target_and_primary_survives(
    rig_factory, multi_acks, multipart
):
    rig = rig_factory(direct_routes=ROUTES)
    rig.handler.multi_acks = multi_acks
    kind = PAYLOAD_TYPE_MULTIPART if multipart else PAYLOAD_TYPE_ACK
    ack = b"ack2\x00\x19"
    p = direct(kind=kind, payload=(bytes([0x10 | PAYLOAD_TYPE_ACK]) if multipart else b"") + ack)
    assert await rig.handler(p, _rx("link"))
    assert rig.air.order == ["local"] * (1 + multi_acks)
    sent = [packet for _, packet in rig.sent_packets]
    plain = [packet for packet in sent if packet.get_payload_type() == PAYLOAD_TYPE_ACK]
    assert len(plain) == 1 and plain[0].payload == ack
    assert all(packet.path == bytes([0xCC]) for packet in sent)
    rig.handler.close_channel_bridge()
    second = rig_factory(direct_routes=ROUTES)
    assert not await second.handler(copy.deepcopy(p), _rx("link"))
    assert not second.air.frames


@pytest.mark.asyncio
async def test_crossing_denial_has_no_ingress_fallback(rig_factory):
    rig = rig_factory(
        direct_routes=ROUTES, crossing_policy={"enabled": True, "default_action": "drop"}
    )
    assert not await rig.handler(direct(), _rx("local"))
    assert not rig.air.frames


@pytest.mark.asyncio
async def test_direct_route_is_opt_in_and_origins_keep_home(rig_factory):
    disabled = rig_factory()
    assert await disabled.handler(direct(), _rx("local"))
    assert disabled.air.order == ["local"]
    disabled.handler.close_channel_bridge()
    enabled = rig_factory(direct_routes=ROUTES)
    assert await enabled.handler(direct(), {"home_radio_id": "local"}, local_transmission=True)
    assert enabled.air.order == ["local"]
    assert enabled.sent_packets[0][1].path == direct().path


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_prefix", "terminal", "missing_ingress"])
async def test_nontransit_never_crosses(rig_factory, change):
    rig = rig_factory(direct_routes=ROUTES)
    p = direct()
    metadata = _rx("local")
    if change == "wrong_prefix":
        p.path[0] = 0x7F
    if change == "terminal":
        p.path, p.path_len = bytearray(), 0
    if change == "missing_ingress":
        metadata = {}
    assert not await rig.handler(p, metadata)
    assert not rig.air.frames


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,kind,payload",
    [
        (ROUTE_TYPE_FLOOD, PAYLOAD_TYPE_TXT_MSG, b"opaque"),
        (ROUTE_TYPE_FLOOD, PAYLOAD_TYPE_ACK, b"abcd"),
        (ROUTE_TYPE_TRANSPORT_DIRECT, PAYLOAD_TYPE_TXT_MSG, b"opaque"),
        (2, PAYLOAD_TYPE_CONTROL, b"\x01abcd"),
        (2, PAYLOAD_TYPE_PATH, b"opaque"),
        (2, PAYLOAD_TYPE_MULTIPART, b"\x15abcd"),
    ],
)
async def test_unsupported_routes_and_types_do_not_cross(rig_factory, route, kind, payload):
    rig = rig_factory(direct_routes=ROUTES)
    p = _direct(payload=payload, payload_type=kind, route=route)
    await rig.handler(p, _rx("local"))
    assert "link" not in rig.air.order


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["queue", "known_send_failure", "expiry"])
async def test_failed_crossing_does_not_report_success_or_repeat_locally(rig_factory, failure):
    rig = rig_factory(direct_routes=ROUTES, queue_ttl_seconds=1, max_pending_crossings=1)
    if failure == "queue":
        rig.handler._bridge_pending = 1
    if failure == "known_send_failure":
        rig.link.send = AsyncMock(return_value=False)
    if failure == "expiry":
        rig.handler._calculate_tx_delay = lambda *a: 1.01
    assert not await rig.handler(direct(), _rx("local"))
    assert not rig.air.frames
    assert not await rig.handler.bridge_echoes.contains(direct())


@pytest.mark.parametrize("path_len,path", [(0xC1, b"abcd"), (0x42, b"ab"), (0x01, b"")])
def test_malformed_path_is_not_eligible(path_len, path):
    p = direct()
    p.path_len, p.path = path_len, bytearray(path)
    assert not BridgeSettings.is_direct_transit(p, bytes([LOCAL_HASH, 0x12, 0x34]))


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [2, 3])
async def test_multibyte_prefix_requires_all_bytes(rig_factory, width):
    rig = rig_factory(direct_routes=ROUTES)
    rig.handler.local_hash_bytes = bytes([LOCAL_HASH, 0x12, 0x34])
    packet = direct(width)
    packet.path[width - 1] ^= 1
    assert packet.path[0] == LOCAL_HASH
    assert not await rig.handler(packet, _rx("local"))
    assert not rig.air.frames


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [2, 3])
async def test_final_consumed_multibyte_hop_preserves_zero_hop_width(rig_factory, width):
    rig = rig_factory(direct_routes=ROUTES)
    rig.handler.local_hash_bytes = bytes([LOCAL_HASH, 0x12, 0x34])
    packet = direct(width, suffix=False)
    assert await rig.handler(packet, _rx("local"))
    outgoing = rig.sent_packets[0][1]
    assert outgoing.path == b""
    assert outgoing.path_len == PathUtils.encode_path_len(width, 0)
    assert outgoing.payload == packet.payload


@pytest.mark.asyncio
async def test_uncertain_direct_send_keeps_restart_reservation_without_retry(rig_factory):
    first = rig_factory(direct_routes=ROUTES)
    first.link.send = AsyncMock(side_effect=RuntimeError("write may have reached modem"))
    packet = direct()
    assert not await first.handler(packet, _rx("local"))
    assert first.link.send.await_count == 1
    assert await first.handler.bridge_echoes.contains(packet)
    first.handler.close_channel_bridge()
    second = rig_factory(direct_routes=ROUTES)
    assert not await second.handler(copy.deepcopy(packet), _rx("local"))
    assert not second.air.frames
