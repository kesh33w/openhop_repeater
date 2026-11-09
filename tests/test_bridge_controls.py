# ruff: noqa: F811
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repeater.channel_bridge import BridgeSettings
from repeater.web.bridge_controls import update, view
from repeater.web.bridge_settings import bridge_candidate
from tests.test_bridge_settings_api import config  # noqa: F401


def engine(config):
    return SimpleNamespace(channel_bridge=BridgeSettings(config), _bridge_pending=2)


def test_running_snapshot_does_not_follow_saved_edits_or_leak_keys(config):
    running = engine(config)
    result = view(config, running)
    assert result["running"] and result["restart_required"] is False
    assert result["pending_crossings"] == 2
    before = copy.deepcopy(result["active_settings"])
    config["channel_bridge"]["channels"]["Public"] = "56" * 16
    result = view(config, running)
    assert result["restart_required"] is True
    assert result["active_settings"] == before
    for key in ("12" * 16, "34" * 16, "56" * 16):
        assert key not in str(result)


def test_defaults_compare_equal_and_missing_runtime_is_explicit(config):
    running = engine(config)
    config["channel_bridge"].update(paused=False, disabled_channels=[], protocols={"trace": False})
    config["channel_bridge"]["mappings"][0]["enabled"] = True
    assert not view(config, running)["restart_required"]
    assert view(config, None)["restart_required"]
    assert not view(config, None)["controls_available"]
    config["channel_bridge"]["enabled"] = False
    assert not view(config, None)["restart_required"]


@pytest.mark.asyncio
async def test_pause_is_persisted_before_apply_without_applying_other_saved_changes(config):
    running = engine(config)
    bridge = running.channel_bridge
    bridge.set_paused = Mock(side_effect=lambda value: setattr(bridge, "paused", value))
    config["channel_bridge"]["mappings"] = []
    manager = Mock()

    def persist(updates, live_update):
        assert bridge.paused is False and live_update is False
        assert updates["channel_bridge"]["mappings"] == []
        assert updates["channel_bridge"]["paused"] is True
        return {"success": True}

    manager.update_and_save.side_effect = persist
    api = SimpleNamespace(
        config=config,
        daemon_instance=SimpleNamespace(repeater_handler=running),
        config_manager=manager,
    )
    assert await update(api, {"paused": True})
    bridge.set_paused.assert_called_once_with(True)
    assert len(bridge.config_snapshot["mappings"]) == 1
    manager.update_and_save.return_value = {"success": False}
    manager.update_and_save.side_effect = None
    assert not await update(api, {"paused": False})
    assert bridge.paused is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"paused": "true"}, {"paused": True, "enabled": True}, {}, None]
)
async def test_invalid_live_controls_never_write(config, payload):
    api = SimpleNamespace(
        config=config,
        daemon_instance=SimpleNamespace(repeater_handler=engine(config)),
        config_manager=Mock(),
    )
    with pytest.raises(ValueError):
        await update(api, payload)
    api.config_manager.update_and_save.assert_not_called()


@pytest.mark.parametrize(
    "updates",
    [
        {"paused": "yes"},
        {"disabled_channels": ["missing"]},
        {"disabled_channels": ["Public", "Public"]},
        {"protocols": {"trace": 1}},
        {"protocols": {"adverts": True}},
        {
            "mappings": [
                {
                    "source_radio": "a",
                    "target_radio": "b",
                    "source_channel": "Public",
                    "target_channel": "#test",
                    "enabled": "false",
                }
            ]
        },
        {"direct_routes": [{"source_radio": "a", "target_radio": "b", "enabled": 0}]},
    ],
)
def test_invalid_switches_are_rejected(config, updates):
    with pytest.raises((ValueError, TypeError)):
        bridge_candidate(config, updates)


def test_direction_and_channel_switches_preserve_configured_rows(config):
    original = copy.deepcopy(config["channel_bridge"]["mappings"][0])
    for updates in (
        {"disabled_channels": ["Public"]},
        {"mappings": [{**original, "enabled": False}]},
    ):
        section = bridge_candidate(config, updates)
        bridge = BridgeSettings({**config, "channel_bridge": section})
        assert not bridge.planner.crossings[0].enabled
        assert len(bridge.planner.crossings) == 1
        assert section["channels"] == config["channel_bridge"]["channels"]
    section = bridge_candidate(
        config, {"direct_routes": [{"source_radio": "a", "target_radio": "b", "enabled": False}]}
    )
    assert BridgeSettings({**config, "channel_bridge": section}).direct_routes == {}
    assert len(section["direct_routes"]) == 1


def test_stale_settings_save_cannot_undo_a_live_pause(config, monkeypatch):
    import cherrypy

    from repeater.web.api_endpoints import APIEndpoints

    config["channel_bridge"]["paused"] = True
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config
    api._config_path = "/tmp/config.yaml"
    api._set_cors_headers = Mock()
    api.config_manager = Mock()
    api.config_manager.update_and_save.return_value = {"success": True}
    monkeypatch.setattr(cherrypy.request, "method", "POST")
    monkeypatch.setattr(cherrypy.request, "json", {"paused": False}, raising=False)
    assert api.update_bridge_settings()["success"]
    assert api.config_manager.update_and_save.call_args.args[0]["channel_bridge"]["paused"] is True


@pytest.mark.parametrize(
    "updates",
    [
        {"paused": "false"},
        {"disabled_channels": ["unknown"]},
        {"protocols": {"trace": 1}},
        {"traffic_limits": {"per_minute": 0}},
        {"reception_evidence": {"enabled": "yes"}},
    ],
)
def test_disabled_empty_drafts_still_validate_controls(updates):
    with pytest.raises((ValueError, TypeError)):
        bridge_candidate({"channel_bridge": {"enabled": False}}, updates)


def test_explicit_default_traffic_limits_match_running_defaults(config):
    running = engine(config)
    config["channel_bridge"]["traffic_limits"] = {
        "per_minute": 12,
        "burst": 3,
        "max_pending_per_direction": 16,
    }
    assert not view(config, running)["restart_required"]
