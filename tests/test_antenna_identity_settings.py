"""Antenna identity mode setting: view, validation and staged saves."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repeater.tx_identities import TxIdentities
from repeater.web.antenna_identity_settings import (
    antenna_identity_candidate,
    antenna_identity_view,
)

KEY = "11" * 32  # a valid 32-byte public key, used as a per-transmitter alias


def config(section=None):
    cfg = {"radios": [{"id": "host-rf"}, {"id": "bay"}]}
    if section is not None:
        cfg["tx_identity_experiment"] = section
    return cfg


@pytest.mark.parametrize(
    "section,mode",
    [
        (None, "off"),
        ({"enabled": False, "mode": "rx_tx"}, "off"),
        ({"enabled": True}, "rx_tx"),
        ({"enabled": True, "mode": "tx", "radios": {"bay": KEY}}, "tx"),
    ],
)
def test_view_reports_saved_mode(section, mode):
    assert antenna_identity_view(config(section))["saved"] == mode


def test_view_compares_saved_with_running():
    cfg = config({"enabled": True, "mode": "rx_tx"})
    running = TxIdentities(config({"enabled": False}), bytes(32), radio_ids=["host-rf", "bay"])
    view = antenna_identity_view(cfg, SimpleNamespace(tx_identities=running))
    assert view["running"] == "off" and view["restart_required"] is True
    assert antenna_identity_view(cfg, SimpleNamespace())["running"] is None


def test_off_keeps_the_section_so_learned_routes_still_resolve():
    section = {"enabled": True, "mode": "rx_tx", "radios": {"bay": KEY}}
    update = antenna_identity_candidate(config(section), {"mode": "off"})
    assert update["tx_identity_experiment"] == {**section, "enabled": False}


def test_enabling_from_nothing_creates_the_section():
    update = antenna_identity_candidate(config(), {"mode": "rx_tx"})
    assert update == {"tx_identity_experiment": {"enabled": True, "mode": "rx_tx"}}


def test_tx_mode_needs_per_transmitter_keys():
    with pytest.raises(ValueError, match="per-transmitter keys"):
        antenna_identity_candidate(config({"enabled": False}), {"mode": "tx"})
    update = antenna_identity_candidate(config({"radios": {"bay": KEY}}), {"mode": "tx"})
    assert update["tx_identity_experiment"]["mode"] == "tx"


@pytest.mark.parametrize("body", [{"mode": "both"}, {"mode": "rx_tx", "extra": 1}, {}, "rx_tx"])
def test_invalid_requests_are_rejected(body):
    with pytest.raises(ValueError):
        antenna_identity_candidate(config(), body)


def test_endpoint_stages_the_change_for_restart(monkeypatch):
    import cherrypy

    from repeater.web.api_endpoints import APIEndpoints

    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config({"enabled": True, "mode": "rx_tx"})
    api.config_manager = Mock(update_and_save=Mock(return_value={"success": True}))
    api._require_post = Mock()
    monkeypatch.setattr("repeater.web.bridge_controls.saved_snapshot", lambda api: api.config)
    monkeypatch.setattr(cherrypy, "request", SimpleNamespace(json={"mode": "off"}), raising=False)
    result = api.update_antenna_identities()
    assert result["success"] and result["data"]["restart_required"] is True
    saved = api.config_manager.update_and_save.call_args
    assert saved.args[0]["tx_identity_experiment"]["enabled"] is False
    assert saved.kwargs == {"live_update": False}


def test_one_byte_codes_pass_settings_validation():
    from repeater.web.antenna_identity_settings import antenna_identity_candidate

    config = {
        "radios": [{"id": "g3909"}, {"id": "rak909"}],
        "tx_identity_experiment": {
            "enabled": False,
            "mode": "rx_tx",
            "one_byte": {"g3909": ["d4", "14"], "rak909": ["12", "d3"]},
        },
    }
    out = antenna_identity_candidate(config, {"mode": "rx_tx"})
    assert out["tx_identity_experiment"]["one_byte"]["g3909"] == ["d4", "14"]
