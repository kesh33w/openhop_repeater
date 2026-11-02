import copy
from types import SimpleNamespace
from unittest.mock import Mock

import cherrypy
import pytest

from repeater.bridge_homes import BridgeHomeAssignmentError, validate_bridge_homes
from repeater.channel_bridge import BridgeSettings
from repeater.main import RepeaterDaemon
from repeater.web.api_endpoints import APIEndpoints
from repeater.web.bridge_settings import bridge_candidate


@pytest.fixture
def config(tmp_path):
    return {
        "repeater": {"node_name": "test"},
        "radios": [{"radio_id": "a"}, {"id": None, "radio_id": "b"}],
        "channel_bridge": {
            "enabled": True, "origin_home": "a", "channels": {"Public": "12" * 16},
            "mappings": [], "echo_database": str(tmp_path / "echo.sqlite"),
        },
        "identities": {"companions": [{"name": "Bot", "identity_key": "12" * 32,
                                       "settings": {"home_radio_id": "b"}}]},
    }


@pytest.mark.parametrize("home", [None, "", "missing", 1, []])
def test_enabled_bridge_requires_origin_before_startup_or_save(config, home):
    config["channel_bridge"]["origin_home"] = home
    with pytest.raises(ValueError, match="origin_home"):
        BridgeSettings.from_config(config)
    with pytest.raises(ValueError, match="origin_home"):
        bridge_candidate(config, {})


@pytest.mark.parametrize("home", [None, "", "missing", 1, []])
def test_enabled_bridge_requires_companion_home(config, home):
    config["identities"]["companions"][0]["settings"]["home_radio_id"] = home
    with pytest.raises(BridgeHomeAssignmentError, match="home_radio_id"):
        BridgeSettings.from_config(config)
    with pytest.raises(BridgeHomeAssignmentError):
        RepeaterDaemon(config, radio=None)._configure_companion_routing(SimpleNamespace(), {"home_radio_id": home})


def test_disabled_bridge_allows_unassigned_draft_and_companion(config):
    config["channel_bridge"].update(enabled=False, origin_home=None)
    config["identities"]["companions"][0]["settings"] = {}
    validate_bridge_homes(config)
    assert bridge_candidate(config, {})["enabled"] is False
    RepeaterDaemon(config, radio=None)._configure_companion_routing(SimpleNamespace(), {})


def test_valid_alias_homes_do_not_use_default_radio(config):
    assert BridgeSettings.from_config(config).origin_home == "a"
    daemon = RepeaterDaemon(config, radio=None)
    daemon._configure_companion_routing(SimpleNamespace(), {"home_radio_id": "b"})


def api_for(config, monkeypatch, method, data):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api._config_path = "/tmp/fixture-config"
    api._set_cors_headers = Mock()
    api.config_manager = Mock()
    api.config_manager.save_to_file.return_value = True
    api.daemon_instance = None
    monkeypatch.setattr(cherrypy.request, "method", method)
    monkeypatch.setattr(cherrypy.request, "json", data, raising=False)
    return api


def test_bridge_home_error_is_classified_without_echoing_names(config, monkeypatch):
    config["identities"]["companions"][0].update(name="secret-like-name", settings={})
    api = api_for(config, monkeypatch, "POST", {})
    result = api.update_bridge_settings()
    assert result["error"] == "bridge_home_assignment_required"
    assert "secret-like-name" not in str(result)
    api.config_manager.update_and_save.assert_not_called()


@pytest.mark.parametrize("settings", [{"home_radio_id": "missing"}, None])
def test_invalid_update_does_not_rename_or_mutate(config, monkeypatch, settings):
    if settings is None:
        config["identities"]["companions"][0]["settings"] = {}
    data = {"name": "Bot", "type": "companion", "new_name": "Renamed"}
    if settings is not None:
        data["settings"] = settings
    before = copy.deepcopy(config)
    api = api_for(config, monkeypatch, "PUT", data)
    assert not api.update_identity()["success"]
    assert config == before
    api.config_manager.save_to_file.assert_not_called()


def test_create_and_update_preserve_explicit_home(config, monkeypatch):
    data = {"name": "NewBot", "type": "companion", "identity_key": "34" * 32,
            "settings": {"home_radio_id": "b"}}
    api = api_for(config, monkeypatch, "POST", data)
    result = api.create_identity()
    assert result["success"], result
    assert config["identities"]["companions"][-1]["settings"]["home_radio_id"] == "b"
    monkeypatch.setattr(cherrypy.request, "method", "PUT")
    monkeypatch.setattr(cherrypy.request, "json", {"name": "NewBot", "type": "companion", "settings": {"home_radio_id": "a"}})
    assert api.update_identity()["success"]
    assert config["identities"]["companions"][-1]["settings"]["home_radio_id"] == "a"


def test_invalid_home_rejected_before_key_generation(config, monkeypatch):
    keygen = Mock(side_effect=AssertionError("Key generation must not run"))
    monkeypatch.setattr("repeater.keygen.generate_meshcore_keypair", keygen)
    before = copy.deepcopy(config)
    api = api_for(config, monkeypatch, "POST", {"name": "New", "type": "companion", "settings": {}})
    assert not api.create_identity()["success"]
    keygen.assert_not_called()
    assert config == before


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("raises", [False, True])
def test_persistence_failure_restores_companion_config(config, monkeypatch, operation, raises):
    before = copy.deepcopy(config)
    data = {"name": "New" if operation == "create" else "Bot", "type": "companion",
            "identity_key": "34" * 32, "settings": {"home_radio_id": "a"}}
    if operation == "update":
        data["new_name"] = "Renamed"
    api = api_for(config, monkeypatch, "POST" if operation == "create" else "PUT", data)
    api.config_manager.save_to_file.return_value = False
    if raises:
        api.config_manager.save_to_file.side_effect = OSError("fixture persistence failure")
    result = api.create_identity() if operation == "create" else api.update_identity()
    assert not result["success"]
    assert config == before
