"""Local destination delivery preserves signals without multiplying reports."""

import json
import threading
from types import SimpleNamespace
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher


def test_one_unchanged_report_per_destination_and_separate_rak():
    pusher = MeshCoreToMqttPusher.__new__(MeshCoreToMqttPusher)
    pusher._lock = threading.Lock()
    received = []
    pusher.connections = []
    for rid, destinations in [
        ("bay", ["sacmesh", "wcmesh", "letsmesh", "gomesh"]),
        ("rak", ["sacmesh", "wcmesh"]),
    ]:
        for destination in destinations:
            conn = SimpleNamespace(
                radio_id=rid,
                logical_observer_members=(rid, "v3") if rid == "bay" else (rid,),
                observer_identity=True,
                public_key="key-" + rid,
                node_name=rid,
                enabled=True,
                disallowed_types=[],
                broker={"name": destination},
                is_connected=lambda: True,
            )
            conn.publish = lambda topic, payload, retain, qos, rid=rid, destination=destination: (
                received.append((rid, destination, json.loads(payload)))
            )
            pusher.connections.append(conn)
    packet = dict(
        logical_observer_id="bay", rx_radio_id="v3", hash="packet-a", RSSI=-103, SNR=2.5, score=0.8
    )
    pusher.publish("packets", packet)
    assert len(received) == 4
    assert {(r, d) for r, d, _ in received} == {
        ("bay", d) for d in ["sacmesh", "wcmesh", "letsmesh", "gomesh"]
    }
    assert all(
        payload["RSSI"] == -103
        and payload["SNR"] == 2.5
        and payload["score"] == 0.8
        and payload["rx_radio_id"] == "v3"
        and payload["observer_radio_id"] == "bay"
        for _, _, payload in received
    )
    assert packet == dict(
        logical_observer_id="bay", rx_radio_id="v3", hash="packet-a", RSSI=-103, SNR=2.5, score=0.8
    )
    received.clear()
    pusher.publish("packets", {**packet, "logical_observer_id": "rak", "rx_radio_id": "rak"})
    assert {(r, d) for r, d, _ in received} == {("rak", "sacmesh"), ("rak", "wcmesh")}
    assert len(received) == 2
