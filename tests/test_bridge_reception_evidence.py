"""Offline independent evidence checks. Public IDs are synthetic fixtures."""

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from repeater.bridge_evidence import EvidenceSettings, Observer, ReceptionEvidence
from repeater.bridge_traffic import DirectionalTrafficGuard, TrafficLimits
from repeater.data_acquisition.bridge_observer_feed import BridgeObserverFeed
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher
from tests.test_channel_bridge import packet
from tests.test_mqtt_radio_feeds import PUB, Identity, config

EXTERNAL = "CD" * 32
OBSERVER = Observer("SFO feed", "SFO", EXTERNAL, "bay")


def evidence(*, capacity=5):
    now = [1700000000.0]
    obj = ReceptionEvidence(
        EvidenceSettings(True, 120, capacity, (OBSERVER,)),
        clock=lambda: now[0],
        monotonic=lambda: now[0],
    )
    return obj, now


def report(now, pkt=None, **updates):
    return json.dumps(
        {
            "origin_id": EXTERNAL,
            "direction": "rx",
            "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "raw": (pkt or packet()).write_to().hex(),
            **updates,
        }
    ).encode()


def ingest(obj, now, pkt=None, **updates):
    return obj.ingest(OBSERVER.broker, OBSERVER.topic, report(now, pkt, **updates))


def latest(obj):
    return obj.status()["recent"][0]


def test_tx_confirmation_is_not_reception_and_timeout_is_not_delivery_failure():
    obj, now = evidence()
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    assert latest(obj)["tx_state"] == "tx_confirmed"
    assert latest(obj)["reception_state"] == "waiting"
    now[0] += 121
    assert latest(obj)["reception_state"] == "unconfirmed"
    assert latest(obj)["tx_state"] == "tx_confirmed"


def test_independent_destination_rx_confirms_and_deduplicates_without_raw_data():
    obj, now = evidence()
    token = obj.begin(packet(), "host-rf", "bay")
    assert ingest(obj, now[0])
    assert latest(obj)["reception_state"] == "waiting"  # modem completion may race report
    obj.complete(token, "tx_confirmed")
    assert latest(obj)["reception_state"] == "observed"
    assert ingest(obj, now[0])
    assert len(latest(obj)["observers"]) == 1
    rendered = json.dumps(obj.status())
    assert packet().write_to().hex() not in rendered
    assert bytes(packet().payload).hex() not in rendered
    assert "bench only" not in rendered
    assert obj.status()["counts"]["observed"] == 1


@pytest.mark.parametrize(
    "outcome,expected", [("tx_failed", "not_applicable"), ("tx_uncertain", "observed")]
)
def test_uncertain_tx_and_known_failed_tx_stay_distinct(outcome, expected):
    obj, now = evidence()
    token = obj.begin(packet(), "host-rf", "bay")
    ingest(obj, now[0])
    obj.complete(token, outcome)
    assert latest(obj)["reception_state"] == expected
    assert latest(obj)["tx_state"] == outcome
    obj.complete(token, "tx_confirmed")  # completion is idempotent
    assert latest(obj)["tx_state"] == outcome


@pytest.mark.parametrize(
    "updates",
    [
        {"direction": "tx"},
        {"origin_id": "AB" * 32},
        {"timestamp": "2023-11-14T22:13:20"},
        {"timestamp": "bad"},
        {"timestamp": "2023-11-14T22:00:00Z"},
        {"timestamp": "2023-11-15T22:13:20Z"},
        {"raw": "00"},
        {"raw": "zz"},
        {"raw": "01ff"},
        {"raw": "0100" + "00" * 256},
    ],
)
def test_malformed_stale_or_non_rx_reports_do_not_confirm(updates):
    obj, now = evidence()
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    assert not ingest(obj, now[0], **updates)
    assert latest(obj)["reception_state"] == "waiting"


def test_retained_unlisted_and_self_reports_are_excluded():
    obj, now = evidence()
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    assert not obj.ingest(OBSERVER.broker, OBSERVER.topic, report(now[0]), retained=True)
    assert not obj.ingest("other broker", OBSERVER.topic, report(now[0]))
    obj.exclude_identities([EXTERNAL.lower()])
    assert obj.topics(OBSERVER.broker) == []
    assert not ingest(obj, now[0])
    assert latest(obj)["reception_state"] == "waiting"


def test_wrong_direction_and_changed_payload_or_path_do_not_match():
    obj, now = evidence()
    token = obj.begin(packet(), "bay", "host-rf")
    obj.complete(token, "tx_confirmed")
    assert ingest(obj, now[0])
    assert latest(obj)["reception_state"] == "waiting"
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    assert ingest(obj, now[0], packet(message="different"))
    changed_path = packet()
    changed_path.path[0] ^= 1
    assert ingest(obj, now[0], changed_path)
    assert latest(obj)["reception_state"] == "waiting"


def test_downstream_flood_prefix_matches_but_unrelated_route_does_not():
    obj, now = evidence()
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    downstream = packet()
    downstream.path.append(0x33)
    downstream.path_len += 1
    assert ingest(obj, now[0], downstream)
    assert latest(obj)["observers"][0]["match"] == "downstream_flood_path"


def test_capacity_does_not_evict_active_attempt_and_expiry_releases_space():
    obj, now = evidence(capacity=1)
    assert obj.begin(packet(), "host-rf", "bay")
    assert obj.begin(packet(message="other"), "host-rf", "bay") is None
    now[0] += 121
    assert obj.begin(packet(message="other"), "host-rf", "bay")
    assert len(obj.status()["recent"]) == 1


def test_evidence_expiry_uses_monotonic_not_wall_time():
    wall, ticks = [1700000000.0], [100.0]
    obj = ReceptionEvidence(
        EvidenceSettings(True, 120, 5, (OBSERVER,)),
        clock=lambda: wall[0],
        monotonic=lambda: ticks[0],
    )
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    wall[0] -= 100000
    ticks[0] += 121
    assert latest(obj)["reception_state"] == "unconfirmed"


def test_disabled_evidence_does_not_subscribe_or_track():
    obj = ReceptionEvidence()
    assert obj.begin(packet(), "host-rf", "bay") is None
    assert obj.topics("SFO feed") == []
    assert obj.status()["recent"] == []


@pytest.mark.parametrize(
    "cfg",
    [
        None,
        {"enabled": 1},
        {"window_seconds": True},
        {"capacity": 0},
        {"enabled": True},
        {"unknown": 1},
        {"observers": [{"broker": "x", "region": "+", "public_key": EXTERNAL, "radio_id": "bay"}]},
    ],
)
def test_evidence_config_fails_closed(cfg):
    settings = config()
    settings["channel_bridge"] = {"reception_evidence": cfg}
    with pytest.raises(ValueError):
        EvidenceSettings.from_config(settings)


def test_evidence_settings_exact_topics_preserve_case():
    settings = config()
    settings["channel_bridge"] = {
        "reception_evidence": {"enabled": True, "observers": [OBSERVER.__dict__]}
    }
    obj = ReceptionEvidence(EvidenceSettings.from_config(settings))
    assert obj.topics("SFO feed") == [OBSERVER.topic]


def test_flood_limits_are_directional_direct_exempt_and_queue_bounded():
    now = [0.0]
    guard = DirectionalTrafficGuard(TrafficLimits(12, 1, 2), clock=lambda: now[0])
    assert guard.admit("a", "b", is_flood=True) is None
    assert guard.admit("a", "b", is_flood=True) == "Bridge direction flood rate limited"
    assert guard.admit("b", "a", is_flood=True) is None
    assert guard.admit("a", "b", is_flood=False) is None
    assert guard.admit("a", "b", is_flood=False) == "Bridge direction queue full"
    guard.release("a", "b")
    now[0] += 5
    assert guard.admit("a", "b", is_flood=True) is None
    assert guard.status()["directions"][0]["pending"] == 2
    assert guard.admit("c", "d", is_flood=False) == "Bridge direction unavailable"


def test_backwards_clock_does_not_create_tokens():
    now = [100.0]
    guard = DirectionalTrafficGuard(TrafficLimits(12, 1, 2), clock=lambda: now[0])
    guard.admit("a", "b", is_flood=True)
    guard.release("a", "b")
    now[0] = 50
    assert guard.admit("a", "b", is_flood=True)
    now[0] = 101
    assert guard.admit("a", "b", is_flood=True)
    now[0] = 105
    assert guard.admit("a", "b", is_flood=True) is None


@pytest.mark.parametrize(
    "cfg",
    [None, {"per_minute": True}, {"burst": 0}, {"max_pending_per_direction": 1001}, {"unknown": 1}],
)
def test_invalid_traffic_limits(cfg):
    with pytest.raises(ValueError):
        TrafficLimits.from_config(cfg)


def make_feed(obj):
    client = Mock()
    client.subscribe.return_value = (0, 7)
    conn = SimpleNamespace(
        broker={"name": OBSERVER.broker}, client=client, tls={"enabled": True, "insecure": False}
    )
    return BridgeObserverFeed(conn, obj), client


def test_subscription_uses_exact_topics_retries_denial_and_cancels_on_close():
    obj, _now = evidence()
    feed, client = make_feed(obj)
    with patch("repeater.data_acquisition.bridge_observer_feed.threading.Timer") as timer:
        feed.connected()
        client.subscribe.assert_called_once_with([(OBSERVER.topic, 0)])
        feed.on_subscribe(client, None, 6, [0])
        assert obj.status()["feeds"][0]["state"] == "subscribing"
        feed.on_subscribe(client, None, 7, [128])
        assert obj.status()["feeds"][0]["state"] == "subscription_failed"
        feed._retry(feed._generation)
        assert client.subscribe.call_count == 2
        feed.on_subscribe(client, None, 7, [0])
        assert obj.status()["feeds"][0]["state"] == "connected"
        feed.disconnected()
        feed._retry(feed._generation - 1)
        assert client.subscribe.call_count == 2
        assert obj.status()["feeds"][0]["state"] == "disconnected"
        timer.return_value.cancel.assert_called()


def test_insecure_broker_never_subscribes_or_accepts_reports():
    obj, now = evidence()
    feed, client = make_feed(obj)
    feed.connection.tls["insecure"] = True
    feed.connected()
    client.subscribe.assert_not_called()
    feed.on_message(
        client, None, SimpleNamespace(topic=OBSERVER.topic, payload=report(now[0]), retain=False)
    )
    assert obj.status()["counts"] == {}
    assert obj.status()["feeds"][0]["state"] == "insecure_broker"


def test_publisher_excludes_own_id_and_passes_external_messages_without_rf_action():
    settings = config()
    obj, now = evidence()
    with patch("repeater.data_acquisition.mqtt_handler.mqtt.Client") as client:
        pusher = MeshCoreToMqttPusher(Identity(), settings, reception_evidence=obj)
    assert PUB.lower() in obj._excluded
    assert pusher.connections[0].reception_feed is None
    feed = pusher.connections[1].reception_feed
    assert feed is not None
    feed._active = True
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    feed.on_message(
        client, None, SimpleNamespace(topic=OBSERVER.topic, payload=report(now[0]), retain=False)
    )
    assert latest(obj)["reception_state"] == "observed"
    client.return_value.publish.assert_not_called()
    client.return_value.connect.assert_not_called()


def test_trace_progress_requires_unchanged_route_and_snr_prefix():
    from openhop_core.protocol.constants import PAYLOAD_TYPE_TRACE, PH_TYPE_SHIFT
    from openhop_core.protocol.packet import Packet

    outgoing = Packet()
    outgoing.header = 2 | (PAYLOAD_TYPE_TRACE << PH_TYPE_SHIFT)
    outgoing.payload = bytearray(b"\x01\x00\x00\x00\x02\x00\x00\x00\x00\x11\x22\x33")
    outgoing.payload_len = len(outgoing.payload)
    outgoing.path = bytearray([4])
    outgoing.path_len = 1
    obj, now = evidence()
    token = obj.begin(outgoing, "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    downstream = copy.deepcopy(outgoing)
    downstream.path.append(8)
    downstream.path_len = 2
    assert ingest(obj, now[0], downstream)
    assert latest(obj)["observers"][0]["match"] == "trace_progress"
    assert latest(obj)["observers"][0]["trace_progress"] == 2
    downstream.path[0] ^= 1
    downstream.path.append(9)
    downstream.path_len = 3
    assert ingest(obj, now[0], downstream)
    assert latest(obj)["observers"][0]["trace_progress"] == 2


def test_direct_reception_requires_exact_outgoing_bytes():
    outgoing = packet()
    outgoing.header = (outgoing.header & ~3) | 2
    obj, now = evidence()
    token = obj.begin(outgoing, "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    downstream = copy.deepcopy(outgoing)
    downstream.path.pop(0)
    downstream.path_len -= 1
    assert ingest(obj, now[0], downstream)
    assert latest(obj)["reception_state"] == "waiting"
    assert ingest(obj, now[0], outgoing)
    assert latest(obj)["reception_state"] == "observed"


def test_diagnostics_endpoint_is_bounded_and_safe_when_bridge_is_disabled(monkeypatch):
    from repeater.web.api_endpoints import APIEndpoints

    api = APIEndpoints.__new__(APIEndpoints)
    api.daemon_instance = None
    api._set_cors_headers = Mock()
    result = api.bridge_diagnostics()
    assert result["success"]
    assert result["data"]["reception"]["enabled"] is False
    assert result["data"]["traffic"]["directions"] == []


def test_diagnostics_requires_existing_authentication(monkeypatch):
    import cherrypy

    from repeater.web.auth.cherrypy_tool import check_auth

    jwt = Mock()
    jwt.verify_jwt.return_value = None
    token = Mock()
    token.verify_token.return_value = None
    monkeypatch.setitem(cherrypy.config, "jwt_handler", jwt)
    monkeypatch.setitem(cherrypy.config, "token_manager", token)
    monkeypatch.setattr(cherrypy.request, "path_info", "/api/bridge_diagnostics")
    monkeypatch.setattr(cherrypy.request, "method", "GET")
    monkeypatch.setattr(cherrypy.request, "headers", {})
    with pytest.raises(cherrypy.HTTPError) as error:
        check_auth()
    assert error.value.status == 401


def test_real_paho_callback_contains_failures_and_processes_next_report(caplog):
    import paho.mqtt.client as mqtt

    obj, now = evidence()
    kwargs = {}
    if hasattr(mqtt, "CallbackAPIVersion"):
        kwargs["callback_api_version"] = mqtt.CallbackAPIVersion.VERSION2
    client = mqtt.Client(**kwargs)
    connection = SimpleNamespace(
        broker={"name": OBSERVER.broker},
        client=client,
        tls={"enabled": True, "insecure": False},
    )
    feed = BridgeObserverFeed(connection, obj)
    feed._active = True  # No network connection is needed to dispatch callbacks.
    token = obj.begin(packet(), "host-rf", "bay")
    obj.complete(token, "tx_confirmed")
    message = mqtt.MQTTMessage(topic=OBSERVER.topic.encode())
    message.payload = report(now[0])
    assert client.suppress_exceptions is False

    with patch.object(obj, "ingest", side_effect=RuntimeError("PRIVATE-REPORT-CONTENT")):
        client._handle_on_message(message)
        client._handle_on_message(message)

    malformed = mqtt.MQTTMessage(topic=b"meshcore/SFO/PRIVATE-TOPIC-\xff/packets")
    malformed.payload = b"PRIVATE-PAYLOAD"
    client._handle_on_message(malformed)
    assert feed._message_errors == 3
    assert latest(obj)["reception_state"] == "waiting"
    records = [row for row in caplog.records if "observer report processing failed" in row.message]
    assert len(records) == 1
    assert records[0].message == (
        "Independent observer report processing failed; further errors suppressed"
    )
    assert "PRIVATE-" not in caplog.text
    assert all(row.exc_info is None for row in records)

    client._handle_on_message(message)
    assert latest(obj)["reception_state"] == "observed"
    assert obj.status()["counts"]["accepted"] == 1
    assert client.suppress_exceptions is False


def test_observer_error_diagnostics_saturate_and_do_not_catch_process_interrupts():
    obj, _now = evidence()
    feed, client = make_feed(obj)
    feed._active = True
    feed._message_errors = 2**31 - 1
    message = SimpleNamespace(topic=OBSERVER.topic, payload=b"{}", retain=False)
    with patch.object(obj, "ingest", side_effect=RuntimeError("synthetic failure")):
        feed.on_message(client, None, message)
    assert feed._message_errors == 2**31 - 1
    with (
        patch.object(obj, "ingest", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        feed.on_message(client, None, message)
