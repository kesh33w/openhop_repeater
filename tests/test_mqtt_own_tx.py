"""Publishing this node's own transmissions as MC2MQTT ``direction: tx`` records.

Opt-in per broker (``publish_tx``). Each successful physical send goes to the
feed of the radio that sent it, with the bytes that radio put on air and no
signal fields, so consumers that honour ``direction`` (CoreScope #667) never
count it as a reception.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from repeater.data_acquisition.storage_collector import StorageCollector
from repeater.data_acquisition.storage_utils import PacketRecord
from tests.test_channel_bridge import C1, C2, packet

RAW_A = packet(C1).write_to().hex()
RAW_B = packet(C2).write_to().hex()


def _conn(radio_id=None, publish_tx=True, members=(), identity=False):
    return SimpleNamespace(
        radio_id=radio_id,
        publish_tx=publish_tx,
        logical_observer_members=members,
        observer_identity=identity,
        public_key="CC" * 32,
        node_name="obs-name",
        enabled=True,
    )


def _tx(radio_id):
    return PacketRecord.own_transmission(
        RAW_A, radio_id, 1700000000.0, origin="node", origin_id="AB" * 32
    )


def test_record_is_direction_tx_without_signal_fields():
    record = _tx("g3909")
    assert record["direction"] == "tx"
    assert record["raw"] == RAW_A
    assert record["len"] == str(len(RAW_A) // 2)
    assert record["tx_radio_id"] == "g3909" and record["tx_radio_ids"] == ["g3909"]
    for absent in ("SNR", "RSSI", "score", "duration", "rx_radio_id"):
        assert absent not in record
    assert len(record["hash"]) == 16 and record["hash"] == record["hash"].upper()


def test_translated_copy_has_its_own_hash():
    a = _tx("g3909")
    b = PacketRecord.own_transmission(RAW_B, "g3910", 1700000000.0, origin="n", origin_id="x")
    assert a["hash"] != b["hash"]


def test_unparseable_bytes_publish_nothing():
    assert PacketRecord.own_transmission("01", "g3909", 0, origin="n", origin_id="x") is None


def test_routing_needs_opt_in_and_matching_radio():
    route = MeshCoreToMqttPusher._payload_for_connection
    payload = _tx("g3909")
    assert route(_conn("g3909", publish_tx=False), "packets", payload) is None
    assert route(_conn("g3910"), "packets", payload) is None
    sent = route(_conn("g3909"), "packets", payload)
    assert sent["observer_radio_id"] == "g3909" and sent["direction"] == "tx"
    # A node-wide feed takes every send.
    assert route(_conn(None), "packets", payload) is payload


def test_logical_feed_takes_sends_from_any_member():
    route = MeshCoreToMqttPusher._payload_for_connection
    conn = _conn("g3909", members=("g3909", "rak909"))
    assert route(conn, "packets", _tx("rak909"))["observer_radio_id"] == "g3909"
    assert route(conn, "packets", _tx("g3910")) is None


def test_observer_identity_replaces_origin():
    sent = MeshCoreToMqttPusher._payload_for_connection(
        _conn("g3927", identity=True), "packets", _tx("g3927")
    )
    assert sent["origin_id"] == "CC" * 32 and sent["origin"] == "obs-name"


def test_rx_routing_is_unchanged_for_tx_brokers():
    # A received packet still follows the rx rules (here: wrong radio -> skipped).
    rx = {"direction": "rx", "rx_radio_id": "g3910", "raw": RAW_A}
    assert MeshCoreToMqttPusher._payload_for_connection(_conn("g3909"), "packets", rx) is None


def _collector(tx_brokers=True):
    collector = object.__new__(StorageCollector)
    collector.config = {"repeater": {"node_name": "test-node"}}
    collector.mqtt_handler = MagicMock()
    collector.mqtt_handler.public_key = "AB" * 32
    collector.mqtt_handler.has_tx_brokers.return_value = tx_brokers
    return collector, collector.mqtt_handler


EGRESS = [
    {"radio_id": "g3909", "success": True, "raw_packet": RAW_A, "timestamp": 1700000001.0},
    {"radio_id": "g3910", "success": True, "raw_packet": RAW_B},
    {"radio_id": "g3927", "success": False, "raw_packet": RAW_B},
    {"radio_id": "rak909", "success": True},
]


def test_collector_publishes_one_record_per_successful_send():
    collector, handler = _collector()
    collector._publish_own_tx_to_mqtt({"timestamp": 1700000000.0}, EGRESS)
    sent = [call.args[0] for call in handler.publish_packet.call_args_list]
    assert [(r["tx_radio_id"], r["raw"]) for r in sent] == [("g3909", RAW_A), ("g3910", RAW_B)]
    assert sent[0]["timestamp"].startswith("2023-11-14T22:13:21")


def test_collector_does_nothing_without_tx_brokers():
    collector, handler = _collector(tx_brokers=False)
    collector._publish_own_tx_to_mqtt({"timestamp": 0}, EGRESS)
    handler.publish_packet.assert_not_called()


def test_record_path_skips_tx_for_invalid_packets():
    collector, handler = _collector()
    collector.sqlite_handler = MagicMock()
    collector.sqlite_handler.store_packet.return_value = 1
    collector.rrd_handler = None
    collector._publish_packet_sync = MagicMock()
    collector._record_packet_blocking({"timestamp": 0}, True, EGRESS)
    handler.publish_packet.assert_not_called()
    collector._record_packet_blocking({"timestamp": 0}, False, EGRESS)
    assert handler.publish_packet.call_count == 2


def test_has_tx_brokers():
    pusher = object.__new__(MeshCoreToMqttPusher)
    pusher.connections = [_conn("g3909", publish_tx=False)]
    assert not pusher.has_tx_brokers()
    pusher.connections.append(_conn("g3910"))
    assert pusher.has_tx_brokers()
