"""Observer configuration validation never opens key files."""
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.config import validate_observer_identity_config
from repeater.config_manager import ConfigManager
from repeater.web.api_endpoints import APIEndpoints, _redact_mqtt_broker_passwords


def settings():
    return {
        "radios": [{"id": "left"}, {"id": "right"}],
        "mqtt_brokers": {
            "separate_observer_identities": True,
            "observer_identities": {"left": {"name": "Left observer", "private_key_file": "/keys/left.key"}},
            "brokers": [],
        },
    }


def api_fixture(monkeypatch, config):
    request = SimpleNamespace(method="POST", params={}, json={})
    monkeypatch.setattr(cherrypy, "request", request)
    monkeypatch.setattr(cherrypy, "response", SimpleNamespace(headers={}, status=200))
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api.daemon_instance = None
    api.config_manager = MagicMock()
    api.config_manager.update_and_save.return_value = {"success": True, "saved": True}
    return api, request


def test_reference_validation_does_not_read_keys(monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("must not read key files"))
    validate_observer_identity_config(settings())
    validate_observer_identity_config({})


@pytest.mark.parametrize("value", ["false", 0, 1, None, [], {}])
def test_mode_requires_real_boolean(value):
    cfg = settings()
    cfg["mqtt_brokers"]["separate_observer_identities"] = value
    with pytest.raises(ValueError, match="boolean"):
        validate_observer_identity_config(cfg)


@pytest.mark.parametrize("identity", [None, [], {}, {"name": "Left", "private_key": "secret"},
    {"name": "Left", "private_key_file": "relative.key"},
    {"name": "", "private_key_file": "/keys/key"},
    {"name": "Left\nobserver", "private_key_file": "/keys/key"},
    {"name": "Left", "private_key_file": "/keys/key\0"},
    {"name": "Left", "private_key_file": "/keys/key", "identity_key": "secret"}])
def test_reject_invalid_identity(identity):
    cfg = settings()
    cfg["mqtt_brokers"]["observer_identities"]["left"] = identity
    with pytest.raises(ValueError):
        validate_observer_identity_config(cfg)


def test_reject_unknown_radio():
    cfg = settings()
    cfg["mqtt_brokers"]["observer_identities"]["unknown"] = cfg["mqtt_brokers"]["observer_identities"].pop("left")
    with pytest.raises(ValueError, match="configured radio"):
        validate_observer_identity_config(cfg)


def test_older_ui_save_preserves_mode_and_map(monkeypatch):
    cfg = settings()
    api, request = api_fixture(monkeypatch, cfg)
    request.json = {"email": "owner@example.test", "brokers": []}
    assert api.update_mqtt_config()["success"]
    call = api.config_manager.update_and_save.call_args.kwargs
    assert call["live_update"] is False
    candidate = copy.deepcopy(cfg)
    ConfigManager._apply_updates(candidate, call["updates"])
    assert candidate["mqtt_brokers"]["observer_identities"] == cfg["mqtt_brokers"]["observer_identities"]
    assert candidate["mqtt_brokers"]["separate_observer_identities"] is True


def test_api_can_save_mode_and_references_without_loading_keys(monkeypatch):
    api, request = api_fixture(monkeypatch, settings())
    request.json = {"separate_observer_identities": False, "observer_identities": {}}
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("must not read key files"))
    result = api.update_mqtt_config()
    assert result["success"] and result["data"]["restart_required"]
    assert api.config_manager.update_and_save.call_args.kwargs["updates"]["mqtt_brokers"] == request.json


def test_api_rejects_inline_key_without_saving(monkeypatch):
    api, request = api_fixture(monkeypatch, settings())
    request.json = {"observer_identities": {"left": {"name": "Left", "private_key": "secret"}}}
    result = api.update_mqtt_config()
    assert not result["success"] and "secret" not in str(result)
    api.config_manager.update_and_save.assert_not_called()


def test_config_redaction_strips_inline_key_material():
    cfg = settings()["mqtt_brokers"]
    cfg["observer_identities"]["left"]["private_key"] = "secret"
    safe = _redact_mqtt_broker_passwords(cfg)
    assert "secret" not in str(safe)
    assert safe["observer_identities"]["left"]["private_key_file"] == "/keys/left.key"
    assert cfg["observer_identities"]["left"]["private_key"] == "secret"


def test_status_exposes_only_connection_public_identity(monkeypatch):
    api, _ = api_fixture(monkeypatch, settings())
    conn = SimpleNamespace(enabled=False, broker={"name": "feed", "host": "broker"},
        is_connected=lambda: False, has_pending_reconnect=lambda: False,
        format="letsmesh", radio_id="left", observer_identity=True,
        node_name="Left", public_key="AB" * 32, identity_error="Key unavailable",
        local_identity=SimpleNamespace(private_key="secret"))
    handler = SimpleNamespace(connections=[conn], separate_observer_identities=True)
    api._get_storage = lambda: SimpleNamespace(mqtt_handler=handler)
    result = api.mqtt_status()
    assert result["success"]
    assert "secret" not in str(result)
    broker = result["data"]["brokers"][0]
    assert broker["public_key"] == "AB" * 32
    assert broker["identity_error"] == "Key unavailable"


def test_one_explicit_repeater_identity_is_valid_and_serializes():
    cfg = settings()
    cfg["mqtt_brokers"]["observer_identities"]["right"] = {
        "name": "Existing observer", "use_repeater_identity": True}
    validate_observer_identity_config(cfg)
    assert _redact_mqtt_broker_passwords(cfg["mqtt_brokers"])["observer_identities"]["right"] == {
        "name": "Existing observer", "use_repeater_identity": True}


def test_two_radios_cannot_use_repeater_identity():
    cfg = settings()
    cfg["mqtt_brokers"]["observer_identities"] = {
        radio: {"name": radio, "use_repeater_identity": True} for radio in ("left", "right")}
    with pytest.raises(ValueError, match="Only one"):
        validate_observer_identity_config(cfg)


@pytest.mark.parametrize("choice", [
    {"use_repeater_identity": True, "private_key_file": "/keys/left.key"},
    {"use_repeater_identity": False}, {"use_repeater_identity": "true"},
    {"use_repeater_identity": 1}, {},
])
def test_identity_choice_must_be_explicit_and_unambiguous(choice):
    cfg = settings()
    cfg["mqtt_brokers"]["observer_identities"]["left"] = {"name": "Left", **choice}
    with pytest.raises(ValueError):
        validate_observer_identity_config(cfg)


def test_malformed_identity_map_cannot_leak_inline_text():
    assert _redact_mqtt_broker_passwords({"observer_identities": "secret"}) == {
        "observer_identities": {}}


def test_import_rejects_inline_key_before_mutating_config(monkeypatch):
    cfg = settings()
    api, request = api_fixture(monkeypatch, cfg)
    original = copy.deepcopy(cfg)
    request.json = {"config": {"mqtt_brokers": {
        "observer_identities": {"left": {"name": "Left", "private_key": "secret"}}}}}
    result = api._config_import_locked()
    assert not result["success"]
    assert "secret" not in str(result)
    assert cfg == original
