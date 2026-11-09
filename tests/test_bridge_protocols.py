"""Real dispatcher/fabric fixtures. Radios are fake and never transmit externally."""

# ruff: noqa: F811
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openhop_core.companion.contact_store import ContactStore
from openhop_core.companion.models import Contact
from openhop_core.node.handlers.protocol_response import ProtocolResponseHandler
from openhop_core.protocol import CryptoUtils, Identity, LocalIdentity, Packet
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_ANON_REQ,
    PAYLOAD_TYPE_CONTROL,
    PAYLOAD_TYPE_PATH,
    PAYLOAD_TYPE_REQ,
    PAYLOAD_TYPE_RESPONSE,
    PAYLOAD_TYPE_TRACE,
    PAYLOAD_TYPE_TXT_MSG,
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)
from openhop_core.protocol.packet_builder import PacketBuilder
from openhop_core.protocol.packet_utils import PathUtils

from repeater.bridge_protocols import (
    crossing_route_identity,
    is_protocol_direct,
    is_protocol_flood,
    is_trace_transit,
)
from repeater.handler_helpers.trace import TraceHelper
from tests.test_channel_bridge_engine import rig_factory  # noqa: F401
from tests.test_direct_bridge import ROUTES
from tests.test_fabric_fanout import LOCAL_HASH, _rx

KEY = bytes([LOCAL_HASH]) + bytes(range(1, 32))
CROSSING_KEY = crossing_route_identity(KEY)
PROTOCOLS = {"discovery": True, "repeater_admin": True, "trace": True}


def peer(kind=PAYLOAD_TYPE_REQ, *, width=1, direct=False, tag=1):
    packet = Packet()
    packet.header = kind << 2 | (ROUTE_TYPE_DIRECT if direct else ROUTE_TYPE_FLOOD)
    packet.path = bytearray(CROSSING_KEY[:width] if direct else bytes([0x91]) * width)
    packet.path_len = PathUtils.encode_path_len(width, 1)
    size = 51 if kind == PAYLOAD_TYPE_ANON_REQ else (4 if kind == PAYLOAD_TYPE_ACK else 20)
    packet.payload = bytearray([tag] * size)
    packet.payload_len = size
    return packet


def trace(width=1, *, progress=0):
    packet = Packet()
    packet.header = PAYLOAD_TYPE_TRACE << 2 | ROUTE_TYPE_DIRECT
    # Bridge appears twice, on outbound and on return after the remote hop.
    route = CROSSING_KEY[:width] + b"\xcc" * width + CROSSING_KEY[:width] + b"\xdd" * width
    packet.payload = bytearray(b"tag1auth" + bytes([{1: 0, 2: 1, 4: 2, 8: 3}[width]]) + route)
    packet.payload_len = len(packet.payload)
    packet.path = bytearray([4] * progress)
    packet.path_len = progress
    return packet


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
@pytest.mark.parametrize(
    "kind",
    [
        PAYLOAD_TYPE_REQ,
        PAYLOAD_TYPE_RESPONSE,
        PAYLOAD_TYPE_ANON_REQ,
        PAYLOAD_TYPE_PATH,
        PAYLOAD_TYPE_TXT_MSG,
        PAYLOAD_TYPE_ACK,
    ],
)
async def test_opaque_flood_discovery_and_direct_return(rig_factory, width, home, other, kind):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    request = peer(kind, width=width)
    original = request.write_to()
    assert await rig.handler(request, _rx(home))
    assert rig.air.order == [home, other]
    outgoing = rig.sent_packets[-1][1]
    assert outgoing.path == bytes([0x91]) * width + CROSSING_KEY[:width]
    assert outgoing.path_len == PathUtils.encode_path_len(width, 2)
    assert outgoing.payload == request.payload
    assert request.write_to() == original

    response = peer(kind, width=width, direct=True, tag=2)
    assert await rig.handler(response, _rx(other))
    assert rig.air.order[-1] == home
    assert rig.sent_packets[-1][1].path == b""
    assert rig.sent_packets[-1][1].path_len == PathUtils.encode_path_len(width, 0)
    assert rig.sent_packets[-1][1].payload == response.payload


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
@pytest.mark.parametrize("same_side", [False, True])
async def test_real_endpoints_learn_paths_and_exchange_admin(
    rig_factory, width, home, other, same_side
):
    """Use endpoint crypto, PATH parsing/contact updates and reciprocal builder."""
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    server_home = home if same_side else other
    expected_hop = KEY[:width] if same_side else CROSSING_KEY[:width]
    client = LocalIdentity(bytes(32))
    server = LocalIdentity(bytes([0x5A]) + bytes(31))
    contact = Contact(public_key=server.get_public_key(), name="FixtureRepeater")
    contacts = ContactStore()
    contacts.add(contact)
    secret = Identity(server.get_public_key()).calc_shared_secret(client.get_private_key())

    login = PacketBuilder.create_login_packet(
        SimpleNamespace(public_key=server.get_public_key().hex(), out_path_len=-1),
        client,
        "fixture-only",
    )
    login.path_len = PathUtils.encode_path_len(width, 0)
    assert login.get_payload_type() == PAYLOAD_TYPE_ANON_REQ
    assert await rig.handler(login, _rx(home))
    received_login = next(pkt for radio, pkt in rig.sent_packets if radio == server_home)
    assert received_login.path == expected_hop
    assert received_login.payload[1:33] == client.get_public_key()
    server_secret = Identity(client.get_public_key()).calc_shared_secret(server.get_private_key())
    login_plain = CryptoUtils.mac_then_decrypt(
        server_secret[:16], server_secret, bytes(received_login.payload[33:])
    )
    assert login_plain[4:].rstrip(b"\0") == b"fixture-only"

    returned = PacketBuilder.create_path_return(
        client.get_public_key()[0],
        server.get_public_key()[0],
        secret,
        received_login.path,
        PAYLOAD_TYPE_RESPONSE,
        (1234).to_bytes(4, "little") + b"ok",
        path_len_encoded=received_login.path_len,
    )
    returned.path_len = PathUtils.encode_path_len(width, 0)
    previous = len(rig.sent_packets)
    assert await rig.handler(returned, _rx(server_home))
    received_return = next(pkt for radio, pkt in rig.sent_packets[previous:] if radio == home)
    assert received_return.payload == returned.payload
    response_handler = ProtocolResponseHandler(lambda *_: None, client, contacts)
    reciprocal = []

    async def capture(packet):
        reciprocal.append(packet)
        return True

    response_handler.set_packet_injector(capture)
    result = await response_handler(received_return)
    await response_handler.wait_for_pending_reciprocals()
    assert result.authenticated
    assert contact.out_path == expected_hop
    assert contact.out_path_len == PathUtils.encode_path_len(width, 1)
    assert len(reciprocal) == 1
    assert await rig.handler(reciprocal[0], _rx(home))
    taught = rig.sent_packets[-1][1]
    assert rig.sent_packets[-1][0] == server_home
    assert taught.path == b""
    plain = CryptoUtils.mac_then_decrypt(secret[:16], secret, bytes(taught.payload[2:]))
    assert plain[0] == PathUtils.encode_path_len(width, 1)
    assert plain[1 : 1 + width] == expected_hop

    request, _ = PacketBuilder.create_protocol_request(
        SimpleNamespace(
            public_key=server.get_public_key().hex(),
            out_path=contact.out_path,
            out_path_len=contact.out_path_len,
        ),
        client,
        1,
        timestamp=1250,
    )
    assert request.header & 3 == ROUTE_TYPE_DIRECT
    assert await rig.handler(request, _rx(home))
    received_request = rig.sent_packets[-1][1]
    assert rig.sent_packets[-1][0] == server_home
    assert received_request.path == b""
    assert (
        CryptoUtils.mac_then_decrypt(secret[:16], secret, bytes(received_request.payload[2:]))[:5]
        == (1250).to_bytes(4, "little") + b"\x01"
    )

    response = peer(PAYLOAD_TYPE_RESPONSE, width=width, direct=True)
    response.path = bytearray(expected_hop)
    response.payload = bytearray(
        bytes([client.get_public_key()[0], server.get_public_key()[0]])
        + CryptoUtils.encrypt_then_mac(secret[:16], secret, b"reply")
    )
    response.payload_len = len(response.payload)
    assert await rig.handler(response, _rx(server_home))
    assert rig.air.order[-1] == home
    assert CryptoUtils.mac_then_decrypt(
        secret[:16], secret, bytes(rig.sent_packets[-1][1].payload[2:])
    ).startswith(b"reply")


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4, 8])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
async def test_trace_round_trip_uses_snr_progress_and_restart_echoes(
    rig_factory, width, home, other
):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    packet = trace(width)
    original = packet.write_to()
    assert await rig.handler(packet, {**_rx(home), "snr": -2.25})
    assert rig.air.order == [other]
    forwarded = rig.sent_packets[-1][1]
    assert forwarded.payload == packet.payload
    assert forwarded.path == b"\xf7"
    assert packet.write_to() == original
    row = rig.handler.storage.record_packet.call_args.args[0]
    assert row["is_trace"] and row["original_path"][0] == CROSSING_KEY[:width].hex().upper()
    assert row["transmitted"]
    rig.handler.close_channel_bridge()
    restarted = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    restarted.handler.local_hash_bytes = KEY
    assert not await restarted.handler(copy.deepcopy(packet), _rx(home))
    assert not restarted.air.frames
    returned = trace(width, progress=2)
    assert await restarted.handler(returned, {**_rx(other), "snr": 3.5})
    assert restarted.air.order == [home]
    assert restarted.sent_packets[-1][1].path == b"\x04\x04\x0e"


@pytest.mark.asyncio
async def test_trace_helper_runs_real_engine_once_and_reports_failed_tx(rig_factory):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    helper = TraceHelper(LOCAL_HASH, rig.handler, AsyncMock())
    rig.link.send = AsyncMock(return_value=False)
    packet = trace()
    packet._rx_radio_id = "local"
    packet._snr = 2
    await helper.process_trace_packet(packet)
    helper.packet_injector.assert_not_awaited()
    assert rig.handler.storage.record_packet.call_count == 1
    assert not rig.handler.storage.record_packet.call_args.args[0]["transmitted"]
    assert packet.path == b""


@pytest.mark.asyncio
async def test_pause_invalidates_queued_crossing_even_after_resume(rig_factory):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    rig.handler._calculate_tx_delay = lambda *_: 0.05
    pending = asyncio.create_task(rig.handler(peer(direct=True), _rx("local")))
    await asyncio.sleep(0.01)
    rig.handler.channel_bridge.set_paused(True)
    rig.handler.channel_bridge.set_paused(False)
    assert not await pending
    assert not rig.air.frames


@pytest.mark.asyncio
async def test_control_adverts_scoped_and_origins_never_cross(rig_factory):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    for kind in (PAYLOAD_TYPE_CONTROL, PAYLOAD_TYPE_ADVERT):
        await rig.handler(peer(kind, tag=kind), _rx("local"))
    scoped = peer(tag=9)
    scoped.header = PAYLOAD_TYPE_REQ << 2 | ROUTE_TYPE_TRANSPORT_FLOOD
    await rig.handler(scoped, _rx("local"))
    await rig.handler(peer(tag=10), {"home_radio_id": "local"}, local_transmission=True)
    assert "link" not in rig.air.order


@pytest.mark.parametrize("change", ["truncated", "reserved", "bad_path", "loop", "oversize"])
def test_malformed_peer_not_eligible(change):
    packet = peer()
    if change == "truncated":
        packet.payload.pop()
    if change == "reserved":
        packet.header |= 0x40
    if change == "bad_path":
        packet.path_len = 2
    if change == "loop":
        packet.path[0] = LOCAL_HASH
    if change == "oversize":
        packet.payload = bytearray(196)
    assert not is_protocol_flood(packet, KEY, PROTOCOLS, CROSSING_KEY)
    assert not is_protocol_direct(packet, CROSSING_KEY, PROTOCOLS)


@pytest.mark.parametrize("change", ["width", "alignment", "progress", "terminal", "wrong", "empty"])
def test_malformed_trace_not_eligible(change):
    packet = trace(2)
    if change == "width":
        packet.path_len = 0x40
    if change == "alignment":
        packet.payload.pop()
    if change == "progress":
        packet.path_len = 1
    if change == "terminal":
        packet.path_len = 4
        packet.path = bytearray(4)
    if change == "wrong":
        packet.payload[9] ^= 1
    if change == "empty":
        packet.payload = packet.payload[:9]
    assert not is_trace_transit(packet, CROSSING_KEY)


@pytest.mark.asyncio
async def test_directional_flood_budget_preserves_local_and_reverse_traffic(rig_factory):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    for tag in range(1, 5):
        assert await rig.handler(peer(tag=tag), _rx("local"))
    assert rig.air.order.count("local") == 4
    assert rig.air.order.count("link") == 3
    assert await rig.handler(peer(tag=8), _rx("link"))
    assert rig.air.order[-2:] == ["link", "local"]
    for tag in range(10, 14):
        assert await rig.handler(peer(tag=tag, direct=True), _rx("local"))
    assert rig.air.order[-4:] == ["link"] * 4
    directions = rig.handler.bridge_traffic.status()["directions"]
    assert sum(row["pending"] for row in directions) == 0
    assert sum(row["rate_limited"] for row in directions) == 1


@pytest.mark.asyncio
async def test_evidence_starts_at_actual_dispatch_with_final_outgoing_bytes(rig_factory):
    from repeater.bridge_evidence import EvidenceSettings, Observer, ReceptionEvidence

    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    evidence = ReceptionEvidence(
        EvidenceSettings(enabled=True, observers=(Observer("test", "SFO", "12" * 32, "link"),))
    )
    rig.handler.bridge_evidence = evidence
    snapshots = []
    begin = evidence.begin

    def capture(packet, source, target):
        snapshots.append((packet.write_to(), source, target))
        return begin(packet, source, target)

    evidence.begin = capture
    rig.handler._calculate_tx_delay = lambda *_: 0.05
    task = asyncio.create_task(rig.handler(peer(direct=True), _rx("local")))
    await asyncio.sleep(0.01)
    assert not snapshots
    assert await task
    assert snapshots == [(rig.air.frames[0][1], "local", "link")]
    status = evidence.status()
    assert status["recent"][0]["tx_state"] == "tx_confirmed"
    assert status["recent"][0]["reception_state"] == "waiting"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_ACK])
async def test_legacy_direct_routes_reject_structurally_invalid_payload(rig_factory, kind):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    packet = peer(kind, direct=True)
    packet.payload = bytearray(b"bad")
    packet.payload_len = 3
    await rig.handler(packet, _rx("local"))
    assert "link" not in rig.air.order


@pytest.mark.asyncio
async def test_cancelled_inflight_crossing_records_uncertain_tx(rig_factory):
    from repeater.bridge_evidence import EvidenceSettings, Observer, ReceptionEvidence

    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    rig.handler.bridge_evidence = ReceptionEvidence(
        EvidenceSettings(enabled=True, observers=(Observer("test", "SFO", "12" * 32, "link"),))
    )
    started = asyncio.Event()

    async def blocked_send(_):
        started.set()
        await asyncio.Event().wait()

    rig.link.send = blocked_send
    packet = peer(direct=True)
    task = asyncio.create_task(rig.handler(packet, _rx("local")))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.handler.bridge_evidence.status()["recent"][0]["tx_state"] == "tx_uncertain"
    assert await rig.handler.bridge_echoes.contains(packet)
    assert rig.handler._bridge_pending == 0
    assert rig.handler.bridge_traffic.status()["directions"][0]["pending"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [4, 8])
async def test_daemon_wires_full_trace_identity(monkeypatch, width):
    from unittest.mock import MagicMock

    from repeater.main import RepeaterDaemon

    seed = bytes([0x2A]) * 32
    public_key = LocalIdentity(seed).get_public_key()
    daemon = RepeaterDaemon(
        {"repeater": {"node_name": "fixture", "identity_key": seed}, "logging": {}}, radio=object()
    )
    monkeypatch.setattr(
        "repeater.service_utils.ensure_plugin_manager_service", lambda: (True, "fixture")
    )
    monkeypatch.setattr("repeater.main.socket.gethostbyname", lambda *_: "192.0.2.1")
    monkeypatch.setattr("openhop_core.node.dispatcher.Dispatcher", MagicMock())
    monkeypatch.setattr(daemon, "_load_additional_identities", AsyncMock())
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("captured handler wiring")

    monkeypatch.setattr("repeater.main.RepeaterHandler", capture)
    with pytest.raises(RuntimeError, match="captured handler wiring"):
        await daemon.initialize()
    assert captured["local_hash_bytes"] == public_key
    packet = trace(width)
    packet.payload[9 : 9 + width] = crossing_route_identity(public_key)[:width]
    assert is_trace_transit(packet, crossing_route_identity(captured["local_hash_bytes"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
async def test_new_protocols_do_not_expand_private_peer_transit(rig_factory, direct):
    import time

    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.local_hash_bytes = KEY
    packet = peer(PAYLOAD_TYPE_REQ, direct=direct)
    packet._private_peer = True
    packet._private_peer_deadline = time.monotonic() + 5
    await rig.handler(packet, {**_rx("local"), "origin": "private_peer"})
    assert "link" not in rig.air.order


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 4, 8])
@pytest.mark.parametrize("home", ["local", "link"])
async def test_native_trace_remains_local_with_distinct_crossing_paused(rig_factory, width, home):
    rig = rig_factory(
        direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct", paused=True
    )
    packet = trace(width)
    packet.payload[9 : 9 + width] = KEY[:width]
    assert await rig.handler(packet, {**_rx(home), "snr": 2.25})
    assert rig.air.order == [home]
    assert rig.sent_packets[-1][1].path == bytes([9])
    assert rig.handler.bridge_traffic.status()["directions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["paused", "disabled", "unconfigured", "policy"])
async def test_distinct_crossing_denied_has_no_local_fallback(rig_factory, mode):
    settings = {"direct_routes": ROUTES, "protocols": PROTOCOLS, "peer_routing": "distinct"}
    if mode == "paused":
        settings["paused"] = True
    elif mode == "disabled":
        settings["direct_routes"] = [{**row, "enabled": False} for row in ROUTES]
    elif mode == "unconfigured":
        settings["direct_routes"] = [ROUTES[1]]
    else:
        settings["crossing_policy"] = {"enabled": True, "default_action": "drop"}
    rig = rig_factory(**settings)
    assert not await rig.handler(peer(direct=True), _rx("local"))
    assert not rig.air.frames
    # A different valid native route is a local repeat, not fallback for the denied V packet.
    native = peer(direct=True, tag=8)
    native.path = bytearray(KEY[:1])
    assert await rig.handler(native, _rx("local"))
    assert rig.air.order == ["local"]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3])
async def test_distinct_crossing_marker_in_flood_does_not_cross_again(rig_factory, width):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    packet = peer(width=width)
    packet.path = bytearray(CROSSING_KEY[:width])
    await rig.handler(packet, _rx("local"))
    assert "link" not in rig.air.order


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("home,other", [("local", "link"), ("link", "local")])
async def test_distinct_routed_ack_and_multipart_use_marker(rig_factory, width, home, other):
    from openhop_core.protocol.constants import PAYLOAD_TYPE_MULTIPART

    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.multi_acks = 1
    packet = peer(PAYLOAD_TYPE_ACK, width=width, direct=True)
    packet.header = PAYLOAD_TYPE_MULTIPART << 2 | ROUTE_TYPE_DIRECT
    packet.payload = bytearray([0x10 | PAYLOAD_TYPE_ACK]) + packet.payload
    packet.payload_len = len(packet.payload)
    assert await rig.handler(packet, _rx(home))
    assert rig.air.order == [other, other]
    assert all(not outgoing.path for _, outgoing in rig.sent_packets)


def test_distinct_marker_is_stable_domain_separated_and_requires_full_key():
    marker = crossing_route_identity(KEY)
    assert marker == crossing_route_identity(KEY)
    assert len(marker) == 32 and marker[0] != KEY[0]
    assert marker != crossing_route_identity(bytes([KEY[0]]) + bytes(31))
    with pytest.raises(ValueError):
        crossing_route_identity(KEY[:3])


def test_new_protocol_flags_require_explicit_distinct_mode(tmp_path):
    from repeater.channel_bridge import BridgeSettings
    from tests.test_channel_bridge_engine import config

    cfg = {"radios": [{"id": "local"}, {"id": "link"}], "channel_bridge": config(tmp_path)}
    cfg["channel_bridge"]["protocols"] = PROTOCOLS
    with pytest.raises(ValueError, match="distinct"):
        BridgeSettings(cfg)
    cfg["channel_bridge"]["peer_routing"] = "distinct"
    settings = BridgeSettings(cfg)
    settings.bind_routing_identity(KEY)
    assert settings.crossing_identity == CROSSING_KEY


@pytest.mark.parametrize("snr,expected", [(-100, 128), (100, 127), (1e308, 127), (-2.25, 247)])
def test_trace_clamps_finite_snr_like_native_helper(snr, expected):
    from repeater.bridge_protocols import append_trace_snr

    packet = trace()
    assert append_trace_snr(packet, CROSSING_KEY, snr)
    assert packet.path == bytes([expected])


@pytest.mark.parametrize("snr", [None, float("nan"), float("inf"), "bad"])
def test_trace_rejects_missing_or_nonfinite_snr(snr):
    from repeater.bridge_protocols import append_trace_snr

    packet = trace()
    assert not append_trace_snr(packet, CROSSING_KEY, snr)
    assert not packet.path


@pytest.mark.asyncio
async def test_distinct_marker_is_not_a_physical_neighbor_even_after_restart(rig_factory):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.neighbour_link_tracker.observe = AsyncMock()
    packet = peer()
    packet.path = bytearray(CROSSING_KEY[:1])
    await rig.handler(packet, _rx("local"))
    rig.handler.neighbour_link_tracker.observe.assert_not_called()
    assert rig.handler._is_flood_looped(packet, "strict")
    row = rig.handler.storage.record_packet.call_args.args[0]
    assert row["upstream_hash"] is None and row["upstream_hash_size"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [(CROSSING_KEY, KEY), (CROSSING_KEY, CROSSING_KEY)])
@pytest.mark.parametrize("width", [1, 2, 3])
async def test_consecutive_bridge_hops_are_not_emitted(rig_factory, first, second, width):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    packet = peer(direct=True, width=width)
    packet.path = bytearray(first[:width] + second[:width])
    packet.path_len = PathUtils.encode_path_len(width, 2)
    assert not await rig.handler(packet, _rx("local"))
    assert not rig.air.frames


@pytest.mark.asyncio
async def test_group_collision_with_virtual_prefix_preserves_existing_routing(rig_factory):
    from unittest.mock import Mock

    from tests.test_channel_bridge import packet as group_packet

    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    rig.handler.loop_detect_mode = "strict"
    rig.handler.neighbour_link_tracker.observe = Mock()
    packet = group_packet()
    packet.path = bytearray(CROSSING_KEY[:1])
    packet.path_len = 1
    assert not rig.handler._is_flood_looped(packet, "strict")
    assert await rig.handler(packet, _rx("local"))
    assert rig.air.order == ["local", "link"]
    rig.handler.neighbour_link_tracker.observe.assert_called_once()
    row = rig.handler.storage.record_packet.call_args.args[0]
    assert row["upstream_hash"] == CROSSING_KEY[:1].hex().upper()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [KEY, CROSSING_KEY])
async def test_native_short_prefix_collisions_do_not_become_virtual_crossings(rig_factory, suffix):
    rig = rig_factory(direct_routes=ROUTES, protocols=PROTOCOLS, peer_routing="distinct")
    packet = peer(direct=True)
    packet.path = bytearray(KEY[:1] + suffix[:1])
    packet.path_len = 2
    assert await rig.handler(packet, _rx("local"))
    assert rig.air.order == ["local"]
    assert rig.sent_packets[-1][1].path == suffix[:1]
