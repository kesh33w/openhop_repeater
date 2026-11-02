import copy
from unittest.mock import Mock
import cherrypy
import pytest
from repeater.web.bridge_settings import bridge_candidate, bridge_view, redact_bridge, REDACTED
from repeater.web.api_endpoints import APIEndpoints


@pytest.fixture
def config():
    return {
        "radios": [{"id": "a"}, {"id": "b"}],
        "channel_bridge": {
            "enabled": True,
            "origin_home": "a",
            "channels": {"Public": "12" * 16, "#test": "34" * 16},
            "echo_database": "/tmp/bridge.sqlite",
            "mappings": [
                {
                    "source_radio": "a",
                    "target_radio": "b",
                    "source_channel": "Public",
                    "target_channel": "#test",
                }
            ],
        },
    }


def test_redaction_and_retention(config):
    before = copy.deepcopy(config)
    view = bridge_view(config)
    assert "12" * 16 not in str(view) and "34" * 16 not in str(view)
    assert (
        bridge_candidate(config, view["settings"])["channels"]
        == config["channel_bridge"]["channels"]
    )
    assert config == before


@pytest.mark.parametrize(
    "update",
    [
        {"enabled": "yes"},
        {"channels": {"Public": "bad"}},
        {"channels": {"unknown": REDACTED}},
        {
            "mappings": [
                {
                    "source_radio": "unknown",
                    "target_radio": "b",
                    "source_channel": "Public",
                    "target_channel": "#test",
                }
            ]
        },
        {
            "mappings": [
                {
                    "source_radio": "a",
                    "target_radio": "a",
                    "source_channel": "Public",
                    "target_channel": "#test",
                }
            ]
        },
        {
            "mappings": [
                {
                    "source_radio": "a",
                    "target_radio": "b",
                    "source_channel": "missing",
                    "target_channel": "#test",
                }
            ]
        },
        {"queue_ttl_seconds": 500},
        {"echo_database": "relative"},
        {"unexpected": True},
    ],
)
def test_invalid_candidate_is_atomic(config, update):
    before = copy.deepcopy(config)
    with pytest.raises((ValueError, TypeError)):
        bridge_candidate(config, update)
    assert config == before


def test_replace_remove_and_disabled_default(config):
    assert bridge_view({})["settings"]["enabled"] is False
    updated = bridge_candidate(config, {"channels": {"Public": "56" * 16}, "mappings": []})
    assert updated["channels"] == {"Public": "56" * 16}
    assert bridge_candidate(config, {"enabled": False})["enabled"] is False


def test_duplicate_mapping_rejected(config):
    with pytest.raises(ValueError):
        bridge_candidate(config, {"mappings": config["channel_bridge"]["mappings"] * 2})


def test_endpoint_redacts_export_even_full(config, monkeypatch):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api._config_path = "/tmp/config"
    api._config_path = "/tmp/config"
    api._set_cors_headers = Mock()
    monkeypatch.setattr(cherrypy.request, "method", "GET")
    for full in ("false", "true"):
        result = api.config_export(full)
        assert result["success"]
        assert "12" * 16 not in str(result)


def test_save_restart_only_and_failure_does_not_echo_secret(config, monkeypatch):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api._config_path = "/tmp/config"
    api._set_cors_headers = Mock()
    api.config_manager = Mock()
    api.config_manager.update_and_save.return_value = {"success": True}
    monkeypatch.setattr(cherrypy.request, "method", "POST")
    monkeypatch.setattr(cherrypy.request, "json", {"enabled": False}, raising=False)
    assert api.update_bridge_settings()["data"]["restart_required"] is True
    assert api.config_manager.update_and_save.call_args.kwargs == {"live_update": False}
    monkeypatch.setattr(
        cherrypy.request, "json", {"channels": {"Public": "SECRET-INVALID"}}, raising=False
    )
    result = api.update_bridge_settings()
    assert not result["success"] and "SECRET-INVALID" not in str(result)


@pytest.mark.parametrize("endpoint", ["/api/bridge_settings", "/api/update_bridge_settings"])
def test_bridge_uses_existing_auth_rejection(endpoint, monkeypatch):
    from repeater.web.auth.cherrypy_tool import check_auth

    jwt = Mock()
    jwt.verify_jwt.return_value = None
    token = Mock()
    token.verify_token.return_value = None
    monkeypatch.setitem(cherrypy.config, "jwt_handler", jwt)
    monkeypatch.setitem(cherrypy.config, "token_manager", token)
    monkeypatch.setattr(cherrypy.request, "path_info", endpoint)
    monkeypatch.setattr(
        cherrypy.request, "method", "GET" if endpoint.endswith("/bridge_settings") else "POST"
    )
    monkeypatch.setattr(cherrypy.request, "headers", {})
    monkeypatch.setattr(cherrypy.request, "params", {})
    with pytest.raises(cherrypy.HTTPError) as error:
        check_auth()
    assert error.value.status == 401


def test_redaction_does_not_change_running_key(config):
    candidate = copy.deepcopy(config)
    redact_bridge(candidate)
    assert candidate["channel_bridge"]["channels"]["Public"] == REDACTED
    assert config["channel_bridge"]["channels"]["Public"] == "12" * 16


def test_nonhex_and_database_path_change_rejected(config):
    for updates in (
        {"channels": {"Public": "x" * 32}},
        {"echo_database": "/tmp/elsewhere"},
        {"channels": {" Public ": "12" * 16}},
    ):
        with pytest.raises(ValueError):
            bridge_candidate(config, updates)


def test_validation_yaml_error_never_echoes_key_line(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text('channel_bridge:\n  channels:\n    Public: "SECRET-CHANNEL-KEY\\q"\n')
    api = APIEndpoints.__new__(APIEndpoints)
    api._config_path = str(path)
    api._set_cors_headers = Mock()
    monkeypatch.setattr(cherrypy.request, "method", "GET")
    result = api.validate_config()
    assert "SECRET-CHANNEL-KEY" not in str(result)
    assert "YAML syntax error" in str(result)
