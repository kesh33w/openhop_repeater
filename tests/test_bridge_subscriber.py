"""Offline evidence subscriber isolation and status trust checks."""

import json
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from repeater.data_acquisition.bridge_subscriber import (
    BridgeSubscriber,
    SubscriberSettings,
    _password,
)
from tests.test_bridge_reception_evidence import evidence, OBSERVER


def settings(path):
    return dict(
        broker=OBSERVER.broker,
        url="wss://observer.example/mqtt",
        username="fixture",
        password_file=str(path),
    )


@pytest.mark.parametrize(
    "url",
    [
        "ws://observer.example/mqtt",
        "mqtt://observer.example",
        "wss://user:pass@observer.example/mqtt",
        "wss://observer.example/mqtt?token=fixture",
    ],
)
def test_rejects_unverified_or_embedded_credential_endpoints(tmp_path, url):
    row = settings(tmp_path / "credential")
    row["url"] = url
    with pytest.raises(ValueError):
        SubscriberSettings.parse([row], [OBSERVER])


def test_credentials_reject_public_permissions_and_symlinks(tmp_path):
    p = tmp_path / "credential"
    p.write_text("synthetic-fixture\n")
    p.chmod(0o644)
    with pytest.raises(ValueError):
        _password(str(p))
    p.chmod(0o600)
    assert _password(str(p)) == "synthetic-fixture"
    link = tmp_path / "link"
    link.symlink_to(p)
    with pytest.raises(OSError):
        _password(str(link))


def test_subscriber_lifecycle_uses_verified_tls_without_publishing(tmp_path, monkeypatch):
    p = tmp_path / "credential"
    p.write_text("synthetic-fixture")
    p.chmod(0o600)
    obj, _ = evidence()
    client = Mock()
    monkeypatch.setattr(
        "repeater.data_acquisition.bridge_subscriber.mqtt.Client", Mock(return_value=client)
    )
    sub = BridgeSubscriber(SubscriberSettings.parse([settings(p)], [OBSERVER])[0], obj)
    sub.connect()
    sub.connect()
    client.connect_async.assert_called_once_with("observer.example", 443, keepalive=60)
    context = client.tls_set_context.call_args.args[0]
    assert context.check_hostname and context.verify_mode.name == "CERT_REQUIRED"
    client.publish.assert_not_called()
    sub.disconnect()
    client.disconnect.assert_called_once()
    client.loop_stop.assert_called_once()


def test_status_is_metadata_not_reception_and_rejects_wrong_identity():
    obj, now = evidence()
    topic = OBSERVER.topic.removesuffix("packets") + "status"
    row = dict(
        origin_id=OBSERVER.public_key,
        timestamp=datetime.fromtimestamp(now[0], timezone.utc).isoformat(),
        status="online",
        radio="910.525,62.5,7,5",
    )
    before = obj.status()["counts"].copy()
    assert obj.ingest_status(OBSERVER.broker, topic, json.dumps(row).encode(), retained=True)
    assert obj.status()["counts"] == before and obj.status()["recent"] == []
    assert obj.status()["receiver_status"][0]["retained"]
    row["origin_id"] = "AB" * 32
    assert not obj.ingest_status(OBSERVER.broker, topic, json.dumps(row).encode())


def test_refused_connection_reloads_rotated_password(tmp_path, monkeypatch):
    p = tmp_path / "credential"
    p.write_text("old-fixture")
    p.chmod(0o600)
    obj, _ = evidence()
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr("repeater.data_acquisition.bridge_subscriber.mqtt.Client", factory)
    sub = BridgeSubscriber(SubscriberSettings.parse([settings(p)], [OBSERVER])[0], obj)
    sub.connect()
    assert "callback_api_version" in factory.call_args.kwargs
    p.write_text("new-fixture")
    sub._connected(client, None, {}, 5)  # CONNACK: not authorised
    client.username_pw_set.assert_called_with("fixture", "new-fixture")
    sub._disconnected(client, None, {}, 0, None)  # v2 argument shape
    sub.disconnect()
