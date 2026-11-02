"""Radio names must resolve identically before hardware and in API choices."""

import copy
from unittest.mock import Mock

import pytest

from repeater.channel_bridge import BridgeSettings
from repeater.config import _merge_radio_entry, build_radio_stack, validate_fabric_config
from repeater.radio_ids import canonical_radio_id, configured_radio_ids
from repeater.web.bridge_settings import bridge_view


@pytest.mark.parametrize(
    "entry",
    [
        {"id": "a"},
        {"radio_id": "a"},
        {"id": None, "radio_id": "a"},
        {"id": "", "radio_id": "a"},
        {"id": "a", "radio_id": None},
        {"id": "a", "radio_id": ""},
        {"id": "a", "radio_id": "a"},
    ],
)
def test_aliases_share_one_canonical_name(entry):
    assert canonical_radio_id(entry) == "a"
    assert _merge_radio_entry({"radio_type": "modem_usb"}, entry)["_radio_id"] == "a"


@pytest.mark.parametrize(
    "entry, message",
    [
        ({}, "requires"),
        ({"id": None, "radio_id": ""}, "requires"),
        ({"id": "a", "radio_id": "b"}, "conflicting"),
        ({"id": 1, "radio_id": "a"}, "string"),
        ({"id": False, "radio_id": "a"}, "string"),
        ({"id": "a", "radio_id": []}, "string"),
        ({"id": " a"}, "string"),
        ({"id": " "}, "string"),
        ("a", "mapping"),
    ],
)
def test_invalid_names_are_actionable(entry, message):
    with pytest.raises(ValueError, match=message):
        canonical_radio_id(entry)


@pytest.mark.parametrize(
    "radios",
    [
        [{"id": "a"}, {"radio_id": "a"}],
        [{"id": "a"}, {"id": "b", "radio_id": "c"}],
        [{"id": "a"}, {}],
        {"id": "a"},
    ],
)
def test_invalid_stack_is_rejected_before_any_radio_opens(monkeypatch, radios):
    factory = Mock(side_effect=AssertionError("must not open hardware"))
    monkeypatch.setattr("repeater.config.get_radio_for_board", factory)
    config = {"radios": radios}
    with pytest.raises(ValueError):
        build_radio_stack(config)
    with pytest.raises(ValueError):
        validate_fabric_config(config)
    factory.assert_not_called()


@pytest.mark.parametrize("config", [{}, {"radios": None}, {"radios": []}])
def test_legacy_radio_configuration_has_no_explicit_names(config):
    assert configured_radio_ids(config) == ()


def test_bridge_and_web_choices_resolve_aliases_without_mutating_config():
    config = {
        "radios": [{"id": None, "radio_id": "a"}, {"radio_id": "b"}],
        "channel_bridge": {
            "enabled": True,
            "origin_home": "a",
            "echo_database": "/tmp/test-bridge.sqlite",
            "channels": {"Public": "12" * 16},
            "mappings": [],
        },
    }
    before = copy.deepcopy(config)
    assert BridgeSettings.from_config(config).radio_ids == ("a", "b")
    assert bridge_view(config)["radio_ids"] == ["a", "b"]
    assert config == before


def test_airtime_budget_uses_same_alias_resolution():
    from repeater.airtime import AirtimeBudgets

    budgets = AirtimeBudgets.__new__(AirtimeBudgets)
    budgets.config = {
        "radios": [
            {"id": None, "radio_id": "a", "duty_cycle": {"max_airtime_per_minute": 60000}},
            {"radio_id": "b", "duty_cycle": {"max_airtime_per_minute": 1200}},
        ]
    }
    assert budgets._budget_for("a") == 60000
    assert budgets._budget_for("b") == 1200
    assert budgets._budget_for("missing") is None


@pytest.mark.parametrize("conflict", [False, True])
def test_radio_api_alias_target_and_conflict_rejection(monkeypatch, conflict):
    from types import SimpleNamespace

    import cherrypy

    from repeater.web.api_endpoints import APIEndpoints

    monkeypatch.setattr(
        cherrypy,
        "request",
        SimpleNamespace(method="POST", json={"radio_id": "b", "bandwidth": 62500}),
        raising=False,
    )
    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = {
        "radio": {"bandwidth": 500000},
        "radios": [
            {"id": None, "radio_id": "a", "radio": {"bandwidth": 500000}},
            {"id": "wrong" if conflict else "", "radio_id": "b", "radio": {"bandwidth": 500000}},
        ],
    }
    before = copy.deepcopy(api.config)
    api.daemon_instance = None
    api.config_manager = Mock()
    api.config_manager.update_and_save.return_value = {
        "success": True,
        "saved": True,
        "live_updated": False,
    }
    api._config_path = "/tmp/test-radio-alias.yaml"
    result = api.update_radio_config()
    if conflict:
        assert result["success"] is False
        assert "conflicting" in result["error"]
        assert api.config == before
        api.config_manager.update_and_save.assert_not_called()
    else:
        assert result["success"] is True
        assert api.config["radios"][0]["radio"]["bandwidth"] == 500000
        assert api.config["radios"][1]["radio"]["bandwidth"] == 62500
