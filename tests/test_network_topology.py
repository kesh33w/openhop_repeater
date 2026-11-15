import asyncio
import copy
from unittest.mock import AsyncMock, patch

import pytest
from openhop_core.node.dispatcher import Dispatcher
from openhop_core.protocol.constants import (
    PAYLOAD_TYPE_ADVERT,
    PAYLOAD_TYPE_ACK,
    PAYLOAD_TYPE_MULTIPART,
)
from openhop_core.rf_fabric import FabricRadio
from openhop_core.rf_fabric.topology import ReceptionComparison
from repeater.config import build_radio_stack, validate_fabric_config
from repeater.network_topology import topology_from_config
from tests import test_fabric_fanout as f
from tests.test_channel_bridge_engine import config as bridge_config
from tests.test_channel_bridge import packet, C1, C2


def config(tmp_path=None, receiver=False, repeat=False):
    cfg = f._config({"tx_mode": "sticky"}, {})
    cfg["radios"] = [
        dict(
            id=rid,
            network=net,
            role=role,
            radio_type="modem_tcp",
            radio={"frequency": freq},
        )
        for rid, net, role, freq in (
            ("local", "909", "transceiver", 909875000),
            ("link", "910", "transceiver", 910525000),
            ("omni909", "909", "receiver" if receiver else "shadow", 909875000),
            ("omni910", "910", "receiver" if receiver else "shadow", 910525000),
        )
    ]
    cfg["fabric"]["topology"] = {
        "networks": {"909": "local", "910": "link"},
        "enable_receiver_forwarding": receiver,
        "repeat_receiver_on_network": repeat,
    }
    if tmp_path:
        cfg["channel_bridge"] = bridge_config(tmp_path)
    return cfg


class Rig:
    def __init__(self, cfg):
        self.air = f._Air()
        self.ears = {e["id"]: f._FakeRadio(e["id"], self.air) for e in cfg["radios"]}
        self.radio = FabricRadio(
            radios=[(v, k) for k, v in self.ears.items()], default_radio_id="local"
        )
        self.topology = topology_from_config(cfg)
        self.radio.fabric.configure_topology(self.topology)
        self.dispatcher = Dispatcher(self.radio)
        with (
            patch("repeater.engine.StorageCollector"),
            patch("repeater.engine.RepeaterHandler._start_background_tasks"),
        ):
            self.handler = f.RepeaterHandler(
                cfg,
                self.dispatcher,
                f.LOCAL_HASH,
                local_hash_bytes=bytes([f.LOCAL_HASH]),
            )
        self.handler._calculate_tx_delay = lambda *args: 0

    def close(self):
        self.handler.close_channel_bridge()


def test_validation_rejects_invalid_maps_before_opening(tmp_path):
    cfg = config(tmp_path)
    validate_fabric_config(cfg)
    variants = []
    for field, value in [("network", "typo"), ("role", "rx"), ("role", None)]:
        c = copy.deepcopy(cfg)
        c["radios"][2][field] = value
        variants.append(c)
    c = copy.deepcopy(cfg)
    c["fabric"]["default_radio"] = "omni909"
    variants.append(c)
    c = copy.deepcopy(cfg)
    c["fabric"]["topology"]["networks"]["909"] = "omni909"
    variants.append(c)
    c = copy.deepcopy(cfg)
    c["radios"][2]["radio"]["frequency"] = 910525000
    variants.append(c)
    c = copy.deepcopy(cfg)
    c["radios"][2]["role"] = "receiver"
    variants.append(c)
    for invalid in variants:
        with (
            patch("repeater.config.get_radio_for_board") as hardware,
            pytest.raises(ValueError),
        ):
            build_radio_stack(invalid)
        hardware.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("ear", ["omni909", "omni910"])
async def test_shadow_never_dispatches_or_consumes_dedup(tmp_path, ear):
    rig = Rig(config(tmp_path))
    try:
        dispatch = AsyncMock()
        rig.dispatcher._dispatch = dispatch
        p = packet(C1 if ear == "omni909" else C2)
        rig.ears[ear].rx_callback(p.write_to(), -90, 8)
        await asyncio.sleep(0)
        dispatch.assert_not_called()
        await rig.dispatcher._process_received_packet(
            p.write_to(), -90, 8, rx_radio_id=ear
        )
        dispatch.assert_not_called()
        assert await rig.handler(p, f._rx("local" if ear == "omni909" else "link"))
        assert set(rig.air.order) == {"local", "link"}
        assert (
            rig.radio.fabric.comparison.snapshot()["receivers"][ear]["unique_packets"]
            == 1
        )
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [PAYLOAD_TYPE_ADVERT, PAYLOAD_TYPE_ACK, 2, 3])
@pytest.mark.parametrize("ear", ["omni909", "omni910"])
async def test_all_originated_and_explicit_tx_guard(kind, ear):
    rig = Rig(config())
    p = f._flood(payload_type=kind)
    try:
        with pytest.raises(ValueError):
            await rig.radio.send(p.write_to(), radio_id=ear)
        with pytest.raises(ValueError):
            await rig.radio.send(p.write_to(), radio_id="local", rx_radio_id=ear)
        assert not rig.air.frames
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rid,channel,order",
    [("local", C1, ["local", "link"]), ("link", C2, ["link", "local"])],
)
async def test_directional_channel_paths_once(tmp_path, rid, channel, order):
    rig = Rig(config(tmp_path))
    try:
        p = packet(channel)
        p._rx_radio_id = rid
        assert await rig.handler(p, f._rx(rid))
        assert rig.air.order == order
        for _, raw in rig.air.frames:
            out = f.Packet()
            out.read_from(raw)
            assert list(out.path) == [0x11, 0x22, f.LOCAL_HASH]
        assert not await rig.handler(copy.deepcopy(p), f._rx(rid))
        assert len(rig.air.frames) == 2
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_delayed_concurrent_ingress_and_disconnect(tmp_path):
    rig = Rig(config(tmp_path))
    try:
        rig.handler._calculate_tx_delay = lambda *args: 0.02
        a, b = packet(C1, "message A"), packet(C2, "message B")
        a._rx_radio_id = "local"
        b._rx_radio_id = "link"
        await asyncio.gather(
            rig.handler(a, f._rx("local")), rig.handler(b, f._rx("link"))
        )
        assert rig.air.order.count("local") == 2 and rig.air.order.count("link") == 2
        assert not rig.air.overlapped
        assert (
            rig.radio.resolve_tx_radio_id(a.write_to(), rx_radio_id="local") == "local"
        )
        rig.radio.fabric.unregister_radio("link")
        with pytest.raises(KeyError):
            await rig.radio.send(b.write_to(), radio_id="link")
        rig.radio.fabric.register_radio(rig.ears["link"], radio_id="link")
        rig.ears["link"].is_connected = False
        assert (await rig.radio.send(b.write_to(), radio_id="link"))["ok"] is False
        rig.ears["link"].is_connected = True
    finally:
        rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rid,channel,target", [("omni909", C1, "link"), ("omni910", C2, "local")]
)
async def test_future_receiver_crossing_is_separate_from_same_network(
    tmp_path, rid, channel, target
):
    rig = Rig(config(tmp_path, receiver=True))
    try:
        p = packet(channel)
        p._rx_radio_id = rid
        assert await rig.handler(p, f._rx(rid))
        assert rig.air.order == [target]
    finally:
        rig.close()


def test_network_hashes_and_multipart_copy():
    rig = Rig(config(receiver=True))
    p = f._flood(rx="local")
    q = copy.deepcopy(p)
    q._rx_radio_id = "omni909"
    assert rig.dispatcher._seen_packet_hash(p) == rig.dispatcher._seen_packet_hash(q)
    q._rx_radio_id = "link"
    assert rig.dispatcher._seen_packet_hash(p) != rig.dispatcher._seen_packet_hash(q)
    rig.dispatcher._client_repeat_enabled = True
    p = f._direct(
        payload=bytes([PAYLOAD_TYPE_ACK]) + b"1234",
        payload_type=PAYLOAD_TYPE_MULTIPART,
        rx="omni909",
    )
    assert rig.dispatcher._copy_packet_for_forward(p)._rx_radio_id == "omni909"
    assert rig.dispatcher._seen_packet_hash(p).startswith("909:")
    rig.handler.mark_seen(p)
    q = copy.deepcopy(p)
    q._rx_radio_id = "local"
    # Unlike ears have separate forwarding admission, but local delivery is shared.
    assert rig.handler.is_duplicate(p)
    assert not rig.handler.is_duplicate(q)
    q._rx_radio_id = "link"
    assert not rig.handler.is_duplicate(q)
    rig.close()


def test_comparison_unique_exclusive_overlap_bounded_and_expiring():
    now = [0.0]
    t = topology_from_config(config())
    m = ReceptionComparison(t, capacity=2, ttl=10, clock=lambda: now[0])
    m.receive("omni909", "a", b"a")
    m.receive("local", "a", b"a")
    m.receive("omni910", "a", b"a")
    m.start_tx("local", b"echo")
    m.receive("omni909", "echo", b"echo")
    m.end_tx("909", b"echo")
    s = m.snapshot()
    assert s["networks"] == {"909": 1, "910": 1}
    assert s["receivers"]["omni909"]["exclusive_packets"] == 0
    assert s["receivers"]["omni910"]["exclusive_packets"] == 1
    assert s["receivers"]["omni909"]["own_tx_match"] == 1
    assert s["receivers"]["omni909"]["tx_overlap"] == 1
    now[0] = 3
    m.receive("local", "echo", b"echo")
    assert m.snapshot()["capacity_evictions"] == 1
    m.start_tx("link", b"failed")
    m.end_tx("910", b"failed", failed=True)
    m.receive("omni910", "failed", b"failed")
    assert m.snapshot()["receivers"]["omni910"]["unique_packets"] == 1
    now[0] = 20
    assert m.snapshot()["networks"] == {"909": 0, "910": 0}


def test_absent_shadow_does_not_prevent_two_radio_start(tmp_path):
    cfg = config(tmp_path)
    air = f._Air()

    def open_radio(c):
        if c["modem_tcp"]["host"] == "absent":
            raise OSError("not connected")
        return f._FakeRadio(c["modem_tcp"]["host"], air)

    for e in cfg["radios"]:
        e["modem_tcp"] = {"host": "absent" if e["role"] == "shadow" else e["id"]}
    with patch("repeater.config.get_radio_for_board", side_effect=open_radio):
        radio, meta = build_radio_stack(cfg)
    assert set(radio.fabric.radios) == {"local", "link"}
    assert meta["radio_ids"] == ["local", "link"]


@pytest.mark.asyncio
async def test_receive_backpressure_bounds_tasks_and_shadows_use_no_tasks():
    rig = Rig(config())
    gate = asyncio.Event()

    async def blocked(*a, **kw):
        await gate.wait()

    rig.dispatcher._process_received_packet = blocked
    raw = packet().write_to()
    for _ in range(300):
        rig.ears["local"].rx_callback(raw, -90, 8)
    assert len(rig.dispatcher._topology_rx_tasks) == 256
    assert rig.dispatcher.topology_rx_dropped == 44
    for _ in range(300):
        rig.ears["omni909"].rx_callback(raw, -90, 8)
    assert len(rig.dispatcher._topology_rx_tasks) == 256
    gate.set()
    await asyncio.gather(*rig.dispatcher._topology_rx_tasks)
    rig.close()


@pytest.mark.asyncio
async def test_once_per_destination_concurrent_networks_and_ack_redundancy():
    rig = Rig(config(receiver=True, repeat=True))
    try:
        raw = packet().write_to()
        await asyncio.gather(
            *(
                rig.radio.send(raw, radio_id=target, rx_radio_id=ear)
                for ear in ("local", "link", "omni909", "omni910")
                for target in ("local", "link")
            )
        )
        assert sorted(rig.air.order) == ["link", "local"]
        ack = f._direct(
            payload=b"1234", payload_type=PAYLOAD_TYPE_ACK, rx="local"
        ).write_to()
        await rig.radio.send(ack, radio_id="local", rx_radio_id="local")
        await rig.radio.send(ack, radio_id="local", rx_radio_id="local")
        assert rig.air.order.count("local") == 3
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_destination_failure_release_and_uncertain_reservation():
    rig = Rig(config())
    raw = packet().write_to()
    rig.ears["local"].send = AsyncMock(return_value=False)
    assert await rig.radio.send(raw, radio_id="local") is False
    rig.ears["local"].send = AsyncMock(return_value={"ok": True})
    assert (await rig.radio.send(raw, radio_id="local"))["ok"]
    rig.ears["link"].send = AsyncMock(side_effect=OSError("uncertain"))
    with pytest.raises(OSError):
        await rig.radio.send(raw, radio_id="link")
    assert (await rig.radio.send(raw, radio_id="link"))[
        "reason"
    ] == "destination duplicate"
    rig.close()


def test_legacy_callback_arities_and_internal_typeerror_are_once_only():
    air = f._Air()
    ear = f._FakeRadio("local", air)
    radio = FabricRadio(radios=[(ear, "local")])
    calls = []
    radio.set_rx_callback(lambda data: calls.append(data))
    ear.rx_callback(b"raw", -90, 8)
    assert calls == [b"raw"]

    def broken(data, rssi, snr):
        calls.append(data)
        raise TypeError("inside callback")

    radio.set_rx_callback(broken)
    ear.rx_callback(b"bad", -90, 8)
    assert calls == [b"raw", b"bad"]


@pytest.mark.asyncio
async def test_durable_echo_keys_are_network_scoped(tmp_path):
    from repeater.channel_bridge import AsyncEchoLedger

    ledger = AsyncEchoLedger(tmp_path / "scoped.sqlite")
    source, outgoing = packet(C1), packet(C2)
    try:
        token = await ledger.reserve(
            source, outgoing, source_network="909", target_network="910"
        )
        assert token
        assert await ledger.contains(source, network="909")
        assert not await ledger.contains(source, network="910")
        assert await ledger.contains(outgoing, network="910")
        await ledger.finish(token, success=True)
    finally:
        await ledger.aclose()
    again = AsyncEchoLedger(tmp_path / "scoped.sqlite")
    try:
        assert await again.contains(outgoing, network="910")
    finally:
        await again.aclose()


def test_unrelated_transmission_does_not_extend_old_echo_window():
    now = [0.0]
    m = ReceptionComparison(topology_from_config(config()), clock=lambda: now[0])
    m.start_tx("local", b"old")
    m.end_tx("909", b"old")
    now[0] = 5
    m.start_tx("local", b"new")
    m.receive("omni909", "old", b"old")
    s = m.snapshot()["receivers"]["omni909"]
    assert s["unique_packets"] == 1 and s["own_tx_match"] == 0 and s["tx_overlap"] == 1
    m.end_tx("909", b"new")


@pytest.mark.parametrize(
    "mode,repeat,expected",
    [
        ("sticky", False, ("local",)),
        ("bridge", False, ("link",)),
        ("bridge", True, ("link", "local")),
        ("default", False, ("local",)),
    ],
)
def test_existing_modes_preserved_with_extra_shadows(mode, repeat, expected):
    cfg = config()
    cfg["fabric"].update(tx_mode=mode, repeat_on_ingress=repeat)
    rig = Rig(cfg)
    assert rig.handler._resolve_relay_tx_radio_ids("local") == expected
    assert (
        rig.radio.resolve_tx_radio_id(packet().write_to(), rx_radio_id="local")
        == expected[0]
    )
    assert rig.handler._resolve_origin_tx_radio_ids() == ("local",)
    rig.handler.config["fabric"]["origin_tx"] = "all"
    assert rig.handler._resolve_origin_tx_radio_ids() == ("local", "link")
    rig.close()


@pytest.mark.asyncio
async def test_transmitted_echo_dedup_uses_destination_network():
    rig = Rig(config(receiver=True))
    p = packet()
    p._rx_radio_id = "local"
    await rig.dispatcher.send_packet(p, wait_for_ack=False, radio_id="link")
    q = copy.deepcopy(p)
    q._rx_radio_id = "omni910"
    assert rig.dispatcher.packet_filter.is_duplicate(
        rig.dispatcher._seen_packet_hash(q)
    )
    q._rx_radio_id = "omni909"
    assert not rig.dispatcher.packet_filter.is_duplicate(
        rig.dispatcher._seen_packet_hash(q)
    )
    rig.close()


@pytest.mark.asyncio
async def test_dispatcher_captures_ingress_before_async_delay():
    rig = Rig(config())
    captured = []

    async def capture(data, rssi, snr, *, rx_radio_id):
        await asyncio.sleep(0)
        captured.append(rx_radio_id)

    rig.dispatcher._process_received_packet = capture
    rig.ears["local"].rx_callback(packet(C1).write_to(), -90, 8)
    rig.ears["link"].rx_callback(packet(C2).write_to(), -90, 8)
    await asyncio.gather(*rig.dispatcher._topology_rx_tasks)
    assert captured == ["local", "link"]
    rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rid,target",
    [("local", "link"), ("link", "local"), ("omni909", "link"), ("omni910", "local")],
)
@pytest.mark.parametrize("multipart", [False, True])
async def test_direct_ack_routes_and_redundancy_with_four_radios(
    tmp_path, rid, target, multipart
):
    cfg = config(tmp_path, receiver=True)
    cfg["channel_bridge"]["direct_routes"] = [
        {"source_radio": "local", "target_radio": "link"},
        {"source_radio": "link", "target_radio": "local"},
    ]
    rig = Rig(cfg)
    rig.handler.multi_acks = 1
    ack = b"ack2\x00\x19"
    p = f._direct(
        payload=(bytes([0x10 | PAYLOAD_TYPE_ACK]) if multipart else b"") + ack,
        payload_type=PAYLOAD_TYPE_MULTIPART if multipart else PAYLOAD_TYPE_ACK,
        rx=rid,
    )
    try:
        assert await rig.handler(p, f._rx(rid))
        assert rig.air.order == [target, target]
        # All regenerated relay packets retain ingress, so the final fabric
        # guard cannot mistake a redundancy copy for a local reply.
        extras, _ = rig.handler._multi_ack_extras(p, 0, 0)
        assert extras and all(e._rx_radio_id == rid for e, _ in extras)
        assert all(e._reply_to_radio_id is None for e, _ in extras)
        outputs = []
        for _, raw in rig.air.frames:
            out = f.Packet()
            out.read_from(raw)
            outputs.append(out)
        assert all(bytes(out.path) == bytes([0xCC, 0xDD]) for out in outputs)
        assert sum(out.get_payload_type() == PAYLOAD_TYPE_ACK for out in outputs) == 1
    finally:
        rig.close()
