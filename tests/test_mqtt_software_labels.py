"""Observer presentation keeps full build metadata without changing identity."""

from unittest.mock import Mock
import pytest
from repeater.data_acquisition.mqtt_handler import MeshCoreToMqttPusher


@pytest.mark.parametrize("version,short", [("1.2.3+build123", "1.2.3"), ("1.2.3", "1.2.3")])
def test_status_labels_preserve_identity_radio_and_full_build(version, short):
    p = MeshCoreToMqttPusher.__new__(MeshCoreToMqttPusher)
    p.app_version = version
    p.config = {"repeater": {"mode": "forward"}}
    p.stats_provider = None
    p.node_name = "observer"
    p.public_key = "test-public-identity"
    p.radio_config = "910.525,62.5,7,5"
    p._radio_map = lambda: []
    p.publish = Mock(return_value=True)
    assert p.publish_status() is True
    topic, status = p.publish.call_args.args
    assert topic == "status" and p.publish.call_args.kwargs == {"retain": True, "qos": 1}
    assert status["model"] == "OpenHop HydraHop"
    assert status["firmware_version"] == f"OpenHop {short}"
    assert status["client_version"] == f"💧🍴 OpenHop HydraHop v{short}"
    assert status["software_build"] == version
    assert status["origin"] == "observer" and status["origin_id"] == "test-public-identity"
    assert status["radio"] == p.radio_config and status["repeat"] == "on"
