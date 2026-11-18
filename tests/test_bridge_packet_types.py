"""Offline packet-type controls: no radios, real queues and native wire routing."""

import copy

import pytest
from nacl.signing import SigningKey, VerifyKey
from openhop_core.protocol import Packet
from openhop_core.protocol.constants import (
    ROUTE_TYPE_DIRECT,
    ROUTE_TYPE_FLOOD,
    ROUTE_TYPE_TRANSPORT_DIRECT,
    ROUTE_TYPE_TRANSPORT_FLOOD,
)
from openhop_core.protocol.crypto import CryptoUtils
from repeater.bridge_protocols import (
    PACKET_TYPES,
    DIRECT_ONLY_TYPES,
    packet_type_enabled,
    validate_packet_types,
    is_protocol_direct,
    is_protocol_flood_candidate,
    is_protocol_flood,
)
from repeater.channel_bridge import BridgeSettings
from repeater.web.bridge_settings import bridge_candidate, bridge_view
from repeater.web.bridge_controls import _snapshot
from tests.test_channel_bridge_engine import rig_factory
from tests.test_channel_bridge import C1, C2, packet as group_packet
from tests.test_fabric_fanout import LOCAL_HASH, _rx

KEY = bytes([LOCAL_HASH]) + bytes(range(1, 32))
ROUTES = [
    {"source_radio": "local", "target_radio": "link"},
    {"source_radio": "link", "target_radio": "local"},
]
PROTOCOLS = {"discovery": True, "repeater_admin": True, "trace": True}


def packet(name, route="flood", path=b""):
    p = Packet()
    p.header = (PACKET_TYPES[name] << 2) | (
        ROUTE_TYPE_FLOOD if route == "flood" else ROUTE_TYPE_DIRECT
    )
    if name in ("req", "response", "txt_msg", "path"):
        payload = bytes(range(20))
    elif name == "anon_req":
        payload = bytes(range(51))
    elif name == "ack":
        payload = b"ack!"
    elif name == "multipart":
        payload = b"\x13ack!"
    elif name in ("grp_txt", "grp_data"):
        payload = b"\xfa" + bytes(range(18))
    elif name == "advert":
        signing = SigningKey(bytes(range(32)))
        public = bytes(signing.verify_key)
        stamp, app = b"\x01\x02\x03\x04", b"\x01bench"
        payload = public + stamp + signing.sign(public + stamp + app).signature + app
    elif name == "trace":
        payload = b"\x01" * 8 + b"\x00" + path
        path = b""
    else:
        payload = b"opaque custom"
    p.payload = bytearray(payload)
    p.payload_len = len(payload)
    p.path = bytearray(path)
    p.path_len = len(path)
    return p


@pytest.mark.parametrize("name", PACKET_TYPES)
@pytest.mark.parametrize("route", ["flood", "direct"])
def test_defaults_and_independent_overrides(name, route):
    kind = PACKET_TYPES[name]
    old_direct = name in ("txt_msg", "ack", "multipart")
    assert packet_type_enabled(kind, route, {}, {}) == (
        route == "direct" and old_direct
    )
    supported = not (route == "flood" and name in DIRECT_ONLY_TYPES)
    assert packet_type_enabled(kind, route, PROTOCOLS, {name: {route: False}}) is False
    assert packet_type_enabled(kind, route, {}, {name: {route: True}}) is supported


@pytest.mark.parametrize(
    "overrides",
    [
        None,
        [],
        {"control": {"direct": True}},
        {"unknown": {"flood": True}},
        {"advert": True},
        {"advert": {}},
        {"advert": {"flood": 1}},
        {"advert": {"transport_flood": True}},
        {"trace": {"flood": False}},
        {"multipart": {"flood": True}},
        {"raw_custom": {"flood": True}},
    ],
)
def test_invalid_overrides_fail_closed(overrides):
    with pytest.raises(ValueError):
        validate_packet_types(overrides)


@pytest.mark.parametrize("name", PACKET_TYPES)
def test_wire_eligibility_and_scope(name):
    p = packet(name, "direct", path=KEY[:1] + b"\xcc")
    flags = {name: {"direct": True}}
    if name != "trace":
        assert is_protocol_direct(p, KEY, {}, flags)
        p.header = (p.header & ~3) | ROUTE_TYPE_TRANSPORT_DIRECT
        assert not is_protocol_direct(p, KEY, {}, flags)
    if name not in DIRECT_ONLY_TYPES:
        p = packet(name)
        flags = {name: {"flood": True}}
        assert is_protocol_flood_candidate(p, {}, flags)
        assert is_protocol_flood(p, KEY, {}, b"\xfd" * 32, flags)
        p.path, p.path_len = bytearray(KEY[:1]), 1
        assert not is_protocol_flood(p, KEY, {}, b"\xfd" * 32, flags)
        p.path, p.path_len = bytearray(b"\xfd"), 1
        assert not is_protocol_flood(p, KEY, {}, b"\xfd" * 32, flags)
        p.header = (p.header & ~3) | ROUTE_TYPE_TRANSPORT_FLOOD
        assert not is_protocol_flood_candidate(p, {}, flags)


def test_malformed_payload_and_version_rejected():
    for name in PACKET_TYPES:
        if name in DIRECT_ONLY_TYPES:
            continue
        p = packet(name)
        flags = {name: {"flood": True}}
        p.header |= 0x40
        assert not is_protocol_flood_candidate(p, {}, flags)
        p.header &= ~0x40
        p.payload = bytearray(b"x")
        assert not is_protocol_flood_candidate(p, {}, flags)


@pytest.fixture
def traffic_rig(rig_factory, monkeypatch):
    bind = BridgeSettings.bind_routing_identity
    monkeypatch.setattr(
        BridgeSettings, "bind_routing_identity", lambda self, key: bind(self, KEY)
    )

    def make(**settings):
        settings.setdefault("direct_routes", copy.deepcopy(ROUTES))
        settings.setdefault("peer_routing", "distinct")
        rig = rig_factory(**settings)
        rig.handler.local_hash_bytes = KEY
        return rig

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name", [n for n in PACKET_TYPES if n not in DIRECT_ONLY_TYPES]
)
async def test_selected_flood_crosses_with_marker_and_unchanged_payload(
    traffic_rig, name
):
    rig = traffic_rig(packet_types={name: {"flood": True}})
    p = packet(name)
    original = p.write_to()
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == ["local", "link"]
    native, crossed = [p for _, p in rig.sent_packets]
    assert bytes(native.path) == KEY[:1]
    assert bytes(crossed.path) == rig.handler.channel_bridge.crossing_identity[:1]
    assert native.payload == crossed.payload == p.payload
    assert p.write_to() == original
    if name == "advert":
        data = bytes(crossed.payload)
        VerifyKey(data[:32]).verify(data[:36] + data[100:], data[36:100])
    assert await rig.handler.bridge_echoes.contains(p)
    assert not await rig.handler(copy.deepcopy(crossed), _rx("link"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PACKET_TYPES)
async def test_selected_direct_crosses_only_target_and_preserves_special_semantics(
    traffic_rig, name
):
    rig = traffic_rig(packet_types={name: {"direct": True}})
    prefix = rig.handler.channel_bridge.crossing_identity[:1]
    p = packet(name, "direct", prefix + b"\xcc")
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == ["link"]
    out = rig.sent_packets[0][1]
    if name == "trace":
        assert out.payload == p.payload
        assert len(out.path) == 1  # measured SNR, not a route hash
    elif name == "multipart":
        assert out.get_payload_type() == PACKET_TYPES["ack"]
        assert out.payload == p.payload[1:]
        assert out.path == b"\xcc"
    else:
        assert out.payload == p.payload
        assert out.path == b"\xcc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["txt_msg", "ack", "path", "req", "response", "anon_req", "multipart", "trace"],
)
async def test_explicit_deny_overrides_all_existing_bundles(traffic_rig, name):
    rig = traffic_rig(protocols=PROTOCOLS, packet_types={name: {"direct": False}})
    p = packet(
        name, "direct", rig.handler.channel_bridge.crossing_identity[:1] + b"\xcc"
    )
    assert not await rig.handler(p, _rx("local"))
    assert not rig.air.frames


@pytest.mark.asyncio
async def test_flood_deny_preserves_local_repeat(traffic_rig):
    rig = traffic_rig(protocols=PROTOCOLS, packet_types={"txt_msg": {"flood": False}})
    assert await rig.handler(packet("txt_msg"), _rx("local"))
    assert rig.air.order == ["local"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "setting",
    [
        {"paused": True},
        {"direct_routes": []},
        {"crossing_policy": {"enabled": True, "default_action": "drop"}},
    ],
)
async def test_existing_crossing_gates_remain_final(traffic_rig, setting):
    rig = traffic_rig(packet_types={"advert": {"flood": True}}, **setting)
    assert await rig.handler(packet("advert"), _rx("local"))
    assert rig.air.order == ["local"]


@pytest.mark.asyncio
async def test_advert_do_not_retransmit_still_blocks(traffic_rig):
    rig = traffic_rig(packet_types={"advert": {"flood": True}})
    p = packet("advert")
    p.mark_do_not_retransmit()
    assert not await rig.handler(p, _rx("local"))
    assert not rig.air.frames


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_channel_translation_and_disabled_mappings_precede_opaque(
    traffic_rig, disabled
):
    rig = traffic_rig(packet_types={"grp_txt": {"flood": True}})
    if disabled:
        for rule in rig.handler.channel_bridge.planner.crossings:
            object.__setattr__(rule, "enabled", False)
    p = group_packet()
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == (["local"] if disabled else ["local", "link"])
    if not disabled:
        assert rig.sent_packets[1][1].payload != p.payload


@pytest.mark.asyncio
async def test_disabled_channel_blocks_opaque_unsupported_group_subtype(traffic_rig):
    rig = traffic_rig(
        packet_types={"grp_txt": {"flood": True}},
        disabled_channels=[C1.name],
        mappings=[],
    )
    p = group_packet()
    plain = bytearray(
        CryptoUtils.mac_then_decrypt(C1.secret[:16], C1.secret, bytes(p.payload[1:]))
    )
    plain[4] = 1  # cannot be translated, but still belongs to disabled channel
    p.payload = bytearray([C1.selector]) + CryptoUtils.encrypt_then_mac(
        C1.secret[:16], C1.secret, plain
    )
    p.payload_len = len(p.payload)
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == ["local"]


def test_saved_settings_roundtrip_defaults_and_explicit_false(tmp_path):
    cfg = {
        "radios": [{"id": "local"}, {"id": "link"}],
        "channel_bridge": {
            "enabled": True,
            "origin_home": "local",
            "peer_routing": "distinct",
            "protocols": PROTOCOLS,
            "direct_routes": ROUTES,
            "channels": {},
            "mappings": [],
            "echo_database": str(tmp_path / "echo.sqlite"),
        },
    }
    before = copy.deepcopy(cfg)
    viewed = bridge_view(cfg)["settings"]
    assert viewed["packet_types"] == {}
    assert _snapshot(cfg["channel_bridge"]) == _snapshot(
        {**cfg["channel_bridge"], "packet_types": {}}
    )
    updated = bridge_candidate(
        cfg, {"packet_types": {"advert": {"flood": True}, "txt_msg": {"flood": False}}}
    )
    assert updated["packet_types"]["txt_msg"]["flood"] is False
    assert updated["protocols"] == PROTOCOLS
    assert cfg == before
    cfg["channel_bridge"]["peer_routing"] = "legacy"
    with pytest.raises(ValueError):
        bridge_candidate(cfg, {"packet_types": {"advert": {"flood": False}}})


@pytest.mark.asyncio
async def test_flood_budget_applies_to_adverts(traffic_rig):
    rig = traffic_rig(
        packet_types={"advert": {"flood": True}},
        traffic_limits={"per_minute": 1, "burst": 1, "max_pending_per_direction": 16},
    )
    first = packet("advert")
    second = copy.deepcopy(first)
    second.payload[-1] ^= 1
    assert await rig.handler(first, _rx("local"))
    assert await rig.handler(second, _rx("local"))
    assert rig.air.order == ["local", "link", "local"]


@pytest.mark.asyncio
async def test_local_adverts_are_unchanged_by_received_advert_switch(traffic_rig):
    rig = traffic_rig(packet_types={"advert": {"flood": False}})
    assert await rig.handler(packet("advert"), {}, local_transmission=True)
    assert rig.air.order == ["local", "link"]
    assert all(not p.path for _, p in rig.sent_packets)


@pytest.mark.asyncio
async def test_reverse_direction_and_direct_no_fallback(traffic_rig):
    rig = traffic_rig(
        packet_types={"raw_custom": {"direct": True}}, direct_routes=[ROUTES[1]]
    )
    p = packet(
        "raw_custom",
        "direct",
        rig.handler.channel_bridge.crossing_identity[:1] + b"\xcc",
    )
    assert not await rig.handler(copy.deepcopy(p), _rx("local"))
    assert not rig.air.order
    # A fresh rig prevents first receive's native dedupe from influencing routing.
    rig.handler.seen_packets.clear()
    assert await rig.handler(p, _rx("link"))
    assert rig.air.order == ["local"]


def test_inheritance_matches_prechange_wire_eligibility():
    import importlib.util
    from pathlib import Path

    old_path = Path(__file__).parent / "fixtures" / "prechange_bridge_protocols.py"
    spec = importlib.util.spec_from_file_location(
        "prechange_bridge_protocols", old_path
    )
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    for discovery in (False, True):
        for admin in (False, True):
            for trace in (False, True):
                protocols = {
                    "discovery": discovery,
                    "repeater_admin": admin,
                    "trace": trace,
                }
                for name in PACKET_TYPES:
                    p = packet(name, "flood")
                    assert is_protocol_flood_candidate(
                        p, protocols
                    ) == old.is_protocol_flood_candidate(p, protocols)
                    p = packet(name, "direct", KEY[:1] + b"\xcc")
                    assert is_protocol_direct(p, KEY, protocols) == (
                        old.is_protocol_direct(p, KEY, protocols)
                        or old.is_legacy_direct(p, KEY)
                    )


@pytest.mark.asyncio
async def test_trace_override_does_not_disable_native_route(traffic_rig):
    rig = traffic_rig(
        protocols={"trace": True}, packet_types={"trace": {"direct": False}}
    )
    p = packet("trace", "direct", KEY[:1] + b"\xcc")
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == ["local"]
    assert rig.sent_packets[0][1].payload == p.payload
    assert len(rig.sent_packets[0][1].path) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("incoming", ["ack", "multipart"])
async def test_ack_controls_classify_received_packets_not_generated_redundancy(
    traffic_rig, incoming
):
    other = "multipart" if incoming == "ack" else "ack"
    rig = traffic_rig(
        packet_types={incoming: {"direct": True}, other: {"direct": False}}
    )
    rig.handler.multi_acks = 1
    p = packet(
        incoming, "direct", rig.handler.channel_bridge.crossing_identity[:1] + b"\xcc"
    )
    assert await rig.handler(p, _rx("local"))
    assert rig.air.order == ["link", "link"]
    assert {p.get_payload_type() for _, p in rig.sent_packets} == {
        PACKET_TYPES["ack"],
        PACKET_TYPES["multipart"],
    }


@pytest.mark.asyncio
async def test_real_router_and_advert_signature_gate_reject_invalid_flood(traffic_rig):
    from types import SimpleNamespace
    from repeater.handler_helpers.advert import AdvertHelper
    from repeater.packet_router import PacketRouter
    from tests.test_packet_router import _make_daemon

    rig = traffic_rig(packet_types={"advert": {"flood": True}})
    daemon = _make_daemon()
    daemon.repeater_handler = rig.handler
    daemon.advert_helper = AdvertHelper(
        SimpleNamespace(get_public_key=lambda: KEY), rig.handler.storage, {}
    )
    p = packet("advert")
    p.payload[40] ^= 1
    p._rx_radio_id = "local"
    await PacketRouter(daemon)._route_packet(p)
    assert p.is_marked_do_not_retransmit()
    assert not rig.air.frames


@pytest.mark.asyncio
@pytest.mark.parametrize("rule_side", ["target", "unrelated"])
async def test_one_way_mapping_blocks_reverse_opaque_copy(traffic_rig, rule_side):
    # Only link -> local is configured for the channel heard on local (C1).
    mappings = [{
        "source_radio": "link", "target_radio": "local",
        "source_channel": C2.name,
        "target_channel": C1.name if rule_side == "target" else C2.name,
    }]
    rig = traffic_rig(packet_types={"grp_txt": {"flood": True}}, mappings=mappings)
    p = group_packet()  # C1, heard on local
    assert await rig.handler(p, _rx("local"))
    if rule_side == "target":
        assert rig.air.order == ["local"]  # one-way: never back across opaquely
    else:
        assert rig.air.order == ["local", "link"]  # unmapped channel: opaque passthrough
