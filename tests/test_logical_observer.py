import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from repeater.data_acquisition.logical_observer import LogicalObserver
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from tests.test_network_topology import Rig, config
from tests.test_channel_bridge import packet


def setup(tmp_path):
    cfg = config(tmp_path, receiver=True, repeat=False)
    rak = copy.deepcopy(cfg["radios"][3])
    rak.update(id="rak", role="shadow")
    cfg["radios"].append(rak)
    rig = Rig(cfg)
    conns = [
        SimpleNamespace(
            radio_id=rid, observer_identity=True, public_key="key-" + rid, node_name=rid
        )
        for rid in ("local", "link", "rak", "legacy")
    ]
    records = []

    def publish(payload):
        for conn in conns:
            selected = MeshCoreToMqttPusher._payload_for_connection(conn, "packets", payload)
            if selected is not None:
                records.append((conn.radio_id, selected))

    return rig, SimpleNamespace(connections=conns, publish_packet=publish), records


def wait_for(predicate):
    limit = time.monotonic() + 2
    while not predicate() and time.monotonic() < limit:
        time.sleep(0.005)
    assert predicate()


@pytest.mark.parametrize("first", ["local", "omni909"])
def test_first_source_shadow_observation_and_legacy_projection(tmp_path, first):
    rig, pusher, records = setup(tmp_path)
    worker = LogicalObserver(
        {"local": ["local", "omni909"], "link": ["link", "omni910"], "rak": ["rak"]},
        rig.topology,
        pusher,
    )
    rig.radio.fabric.add_observation_callback(worker.receive)
    try:
        raw = packet().write_to()
        other = "omni909" if first == "local" else "local"
        worker.receive(raw, -80, 4, rx_radio_id=first)
        variant = packet()
        variant.path.append(0x42)
        variant.path_len += 1
        worker.receive(variant.write_to(), -20, 10, rx_radio_id=other)
        worker.receive(raw, -85, 2, rx_radio_id="omni910")
        routing = []
        rig.radio.fabric.set_legacy_rx_callback(lambda *a, **kw: routing.append(a))
        rig.ears["rak"].rx_callback(raw, -90, 1)
        wait_for(lambda: len(records) == 3)
        assert not routing and not rig.air.frames
        local = next(row for rid, row in records if rid == "local")
        assert local["rx_radio_id"] == first and local["RSSI"] == "-80" and local["SNR"] == "4"
        assert local["raw"] == raw.hex() and local["observer_radio_id"] == "local"
        assert local["origin_id"] == "key-local" and "tx_radio_ids" not in local
        assert worker.snapshot()["duplicates"] == 1
        assert (
            MeshCoreToMqttPusher._payload_for_connection(
                pusher.connections[0], "packets", {"rx_radio_id": "local"}
            )
            is None
        )
        assert (
            MeshCoreToMqttPusher._payload_for_connection(
                pusher.connections[3], "packets", {"rx_radio_id": "legacy"}
            )["rx_radio_id"]
            == "legacy"
        )
    finally:
        worker.close()
        rig.close()


def test_concurrent_ears_once(tmp_path):
    rig, pusher, records = setup(tmp_path)
    worker = LogicalObserver({"local": ["local", "omni909"]}, rig.topology, pusher)
    try:
        raw = packet().write_to()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda i: worker.receive(
                        raw, -i, i, rx_radio_id="local" if i % 2 else "omni909"
                    ),
                    range(100),
                )
            )
        wait_for(lambda: len(records) == 1)
        assert worker.snapshot()["accepted"] == 1 and worker.snapshot()["duplicates"] == 99
    finally:
        worker.close()
        rig.close()


def test_capacity_expiry_and_invalid(tmp_path):
    rig, pusher, records = setup(tmp_path)
    now = [0.0]
    worker = LogicalObserver(
        {"local": ["local", "omni909"]}, rig.topology, pusher, capacity=1, clock=lambda: now[0]
    )
    try:
        a, b = packet(message="a").write_to(), packet(message="b").write_to()
        worker.receive(a, -80, 1, rx_radio_id="local")
        worker.receive(b, -70, 2, rx_radio_id="local")
        worker.receive(b"", -70, 2, rx_radio_id="local")
        assert worker.snapshot()["capacity_drops"] == 1 and worker.snapshot()["invalid"] == 1
        now[0] = 301
        worker.receive(b, -70, 2, rx_radio_id="omni909")
        wait_for(lambda: len(records) == 2)
        assert worker.snapshot()["cache_size"] == 1 and not rig.air.frames
    finally:
        worker.close()
        rig.close()


def test_queue_backpressure(tmp_path):
    rig, pusher, records = setup(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def blocked(payload):
        entered.set()
        release.wait(3)

    pusher.publish_packet = blocked
    worker = LogicalObserver(
        {"local": ["local", "omni909"]}, rig.topology, pusher, queue_capacity=1
    )
    try:
        worker.receive(packet(message="a").write_to(), -80, 1, rx_radio_id="local")
        assert entered.wait(1)
        worker.receive(packet(message="b").write_to(), -80, 1, rx_radio_id="local")
        start = time.monotonic()
        worker.receive(packet(message="c").write_to(), -80, 1, rx_radio_id="local")
        assert time.monotonic() - start < 0.1 and worker.snapshot()["queue_drops"] == 1
    finally:
        release.set()
        worker.close()
        rig.close()


@pytest.mark.parametrize(
    "groups",
    [
        {"local": ["local", "omni910"]},
        {"local": ["local", "rak"]},
        {"local": ["local", "omni909"], "omni909": ["omni909"]},
        {"missing": ["missing"]},
    ],
)
def test_invalid_membership(tmp_path, groups):
    rig, pusher, _ = setup(tmp_path)
    try:
        with pytest.raises(ValueError):
            LogicalObserver(groups, rig.topology, pusher)
        assert all(not hasattr(c, "logical_observer_members") for c in pusher.connections)
    finally:
        rig.close()


def test_exact_local_tx_echo_suppressed_for_pair_and_rak(tmp_path):
    import hashlib

    rig, pusher, records = setup(tmp_path)
    worker = LogicalObserver({"link": ["link", "omni910"], "rak": ["rak"]}, rig.topology, pusher)
    rig.radio.fabric.add_observation_callback(worker.receive)
    rig.radio.fabric.set_legacy_rx_callback(lambda *a, **kw: None)
    try:
        raw = packet().write_to()
        frame = hashlib.sha256(raw).digest()
        network = rig.radio.fabric.comparison.start_tx("link", frame)
        for rid in ["omni910", "rak"]:
            rig.ears[rid].rx_callback(raw, -70, 3)
        assert worker.snapshot()["own_tx_suppressed"] == 2 and not records
        rig.radio.fabric.comparison.end_tx(network, frame)
        # Same content received with a different real RF path is not an exact echo.
        variant = packet()
        variant.path.append(0x41)
        variant.path_len += 1
        rig.ears["rak"].rx_callback(variant.write_to(), -70, 3)
        wait_for(lambda: len(records) == 1)
    finally:
        worker.close()
        rig.close()


def test_invalid_shapes_and_advert_signature(tmp_path):
    from openhop_core.protocol.constants import PAYLOAD_TYPE_ADVERT
    from nacl.signing import SigningKey

    rig, pusher, records = setup(tmp_path)
    worker = LogicalObserver({"local": ["local", "omni909"]}, rig.topology, pusher)
    rig.radio.fabric.add_observation_callback(worker.receive)
    rig.radio.fabric.set_legacy_rx_callback(lambda *a, **kw: None)
    try:
        rig.ears["local"].rx_callback(b"", -60, 3)
        empty = packet()
        empty.payload = bytearray()
        empty.payload_len = 0
        worker.receive(empty.write_to(), -60, 3, rx_radio_id="local")
        # Encoded width=2, 33 hops exceeds the 64-byte path bound.
        long = bytes([packet().header, 0x40 | 33]) + bytes(66) + bytes(packet().payload)
        worker.receive(long, -60, 3, rx_radio_id="local")
        advert = packet()
        advert.header = (PAYLOAD_TYPE_ADVERT << 2) | 1
        key = SigningKey(bytes(range(32)))
        pub = bytes(key.verify_key)
        timestamp = (123456).to_bytes(4, "little")
        app = b"\x01"
        signature = key.sign(pub + timestamp + app).signature
        advert.payload = bytearray(pub + timestamp + bytes(64) + app)
        advert.payload_len = len(advert.payload)
        worker.receive(advert.write_to(), -60, 3, rx_radio_id="local")
        wait_for(lambda: worker.snapshot().get("invalid") == 4)
        assert not records
        advert.payload = bytearray(pub + timestamp + signature + app)
        worker.receive(advert.write_to(), -60, 3, rx_radio_id="local")
        wait_for(lambda: len(records) == 1)
    finally:
        worker.close()
        rig.close()


def test_shadow_status_receive_only_omits_shared_tx_metrics(tmp_path):
    rig, pusher, _ = setup(tmp_path)
    worker = LogicalObserver({"local": ["local", "omni909"], "rak": ["rak"]}, rig.topology, pusher)
    try:
        metrics = dict(
            packets_sent=9,
            tx_air_secs=12,
            current_airtime_ms=25,
            utilization_percent=4,
            packets_received=50,
            errors=0,
        )
        payload = dict(
            repeat="on",
            stats={"uptime_secs": 123},
            radios=[dict(id=rid, radio="910.525", **metrics) for rid in ["local", "rak"]],
        )
        before = copy.deepcopy(payload)
        shadow = MeshCoreToMqttPusher._payload_for_connection(
            pusher.connections[2], "status", payload
        )
        pair = MeshCoreToMqttPusher._payload_for_connection(
            pusher.connections[0], "status", payload
        )
        assert shadow["repeat"] == "off" and shadow["observer_mode"] == "receive_only"
        for key in ["packets_sent", "tx_air_secs", "current_airtime_ms", "utilization_percent"]:
            assert key not in shadow["stats"] and key not in shadow["radios"][0]
            assert pair["stats"][key] == metrics[key]
        assert pair["repeat"] == "on" and "observer_mode" not in pair
        assert shadow["stats"]["uptime_secs"] == 123
        assert shadow["stats"]["packets_received"] == 0
        assert payload == before
    finally:
        worker.close()
        rig.close()
