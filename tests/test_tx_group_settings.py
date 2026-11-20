import copy
from types import SimpleNamespace

import pytest

from repeater.web.tx_group_settings import tx_group_candidate, tx_group_view


def config():
    return {
        "radio": {
            "frequency": 909875000,
            "bandwidth": 250000,
            "spreading_factor": 7,
            "coding_rate": 5,
            "cad": {"enabled": False},
            "tx_power": 22,
        },
        "radios": [
            {"id": "host", "network": "smf", "role": "transceiver"},
            {
                "id": "bay",
                "network": "sfo",
                "role": "transceiver",
                "radio": {"frequency": 910525000},
            },
            {"id": "rak", "network": "sfo", "role": "receiver", "radio": {"frequency": 910525000}},
        ],
        "fabric": {
            "default_radio": "host",
            "topology": {
                "networks": {"smf": "host", "sfo": "bay"},
                "enable_receiver_forwarding": True,
            },
        },
    }


def runtime():
    return {
        "bay": SimpleNamespace(lbt_enabled=False, _cad_mode_explicit=False),
        "rak": SimpleNamespace(lbt_enabled=False, _cad_mode_explicit=False),
    }


def test_promote_reorder_restore_preserves_power_and_input():
    cfg = config()
    before = copy.deepcopy(cfg)
    patch = tx_group_candidate(
        cfg, {"primary_radio_id": "bay", "radio_ids": ["rak", "bay"]}, runtime()
    )
    assert cfg == before
    staged = {**cfg, **patch}
    assert staged["radios"] == before["radios"]
    assert staged["radio"]["tx_power"] == 22
    assert staged["fabric"]["topology"]["tx_groups"]["bay"] == ["rak", "bay"]
    patch = tx_group_candidate(staged, {"primary_radio_id": "bay", "radio_ids": ["bay"]}, {})
    assert "radios" not in patch
    assert patch["fabric"]["topology"]["tx_groups"]["bay"] == ["bay"]


@pytest.mark.parametrize(
    "ids", [["rak"], ["bay", "bay"], ["host", "bay"], [], ["bay", "rak", "host"]]
)
def test_invalid_order(ids):
    with pytest.raises(ValueError):
        tx_group_candidate(config(), {"primary_radio_id": "bay", "radio_ids": ids}, runtime())


def test_both_radios_must_be_available():
    with pytest.raises(ValueError, match="Both radios must be available"):
        tx_group_candidate(config(), {"primary_radio_id": "bay", "radio_ids": ["bay", "rak"]}, {})


@pytest.mark.parametrize("cad", [True, False, None])
def test_existing_cad_and_power_are_unchanged(cad):
    cfg = config()
    if cad is None:
        cfg["radio"].pop("cad")
    else:
        cfg["radio"]["cad"]["enabled"] = cad
    before = copy.deepcopy(cfg)
    # No CAD status properties are needed on the physical adapters.
    patch = tx_group_candidate(
        cfg,
        {"primary_radio_id": "bay", "radio_ids": ["rak", "bay"]},
        {"bay": object(), "rak": object()},
    )
    assert cfg == before
    assert set(patch) == {"fabric"}
    assert patch["fabric"]["topology"]["tx_groups"]["bay"] == ["rak", "bay"]


def test_view_has_saved_and_running_order_without_secrets():
    cfg = config()
    cfg["radio"]["password"] = "secret"
    view = tx_group_view(cfg, SimpleNamespace(tx_groups={}))
    assert view["restart_required"] is False
    assert view["groups"][1]["radio_ids"] == ["bay"]
    assert "secret" not in str(view)


def test_api_stages_without_live_updates(monkeypatch):
    import cherrypy
    from unittest.mock import Mock
    from repeater.web.api_endpoints import APIEndpoints
    import repeater.web.api_endpoints as endpoints

    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config()
    api.daemon_instance = SimpleNamespace()
    api.config_manager = Mock()
    api.config_manager.update_and_save.return_value = {"success": True}
    api._require_post = Mock()
    api._success = lambda data: data
    monkeypatch.setattr(endpoints, "physical_radios", lambda _: (runtime(), "bay"))
    monkeypatch.setattr(
        cherrypy,
        "request",
        SimpleNamespace(json={"primary_radio_id": "bay", "radio_ids": ["rak", "bay"]}),
    )
    result = api.update_tx_group()
    assert result["restart_required"] is True
    assert api.config_manager.update_and_save.call_args.kwargs == {"live_update": False}
    assert api.config["radios"][2]["role"] == "receiver"


def test_api_failed_save_does_not_claim_success(monkeypatch):
    import cherrypy
    from unittest.mock import Mock
    from repeater.web.api_endpoints import APIEndpoints
    import repeater.web.api_endpoints as endpoints

    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config()
    api.daemon_instance = SimpleNamespace()
    api.config_manager = Mock()
    api.config_manager.update_and_save.return_value = {"success": False}
    api._require_post = Mock()
    api._error = lambda message: {"error": message}
    monkeypatch.setattr(endpoints, "physical_radios", lambda _: (runtime(), "bay"))
    monkeypatch.setattr(
        cherrypy,
        "request",
        SimpleNamespace(json={"primary_radio_id": "bay", "radio_ids": ["rak", "bay"]}),
    )
    result = api.update_tx_group()
    assert "could not be saved" in result["error"]
    assert api.config["radios"][2]["role"] == "receiver"


def test_scope_round_trips_and_is_dropped_for_single_radio():
    cfg = config()
    patch = tx_group_candidate(
        cfg,
        {"primary_radio_id": "bay", "radio_ids": ["rak", "bay"], "scope": "crossings"},
        runtime(),
    )
    staged = {**cfg, **patch}
    assert staged["fabric"]["topology"]["tx_group_scopes"] == {"bay": "crossings"}
    row = next(r for r in tx_group_view(staged)["groups"] if r["primary_radio_id"] == "bay")
    assert row["scope"] == "crossings" and row["active_scope"] is None
    # Back to a single transmitter: the scope has nothing to apply to.
    patch = tx_group_candidate(
        staged, {"primary_radio_id": "bay", "radio_ids": ["bay"], "scope": "crossings"}, {}
    )
    assert "tx_group_scopes" not in patch["fabric"]["topology"]
    # Omitting scope means "all".
    patch = tx_group_candidate(
        cfg, {"primary_radio_id": "bay", "radio_ids": ["rak", "bay"]}, runtime()
    )
    assert "tx_group_scopes" not in patch["fabric"]["topology"]


@pytest.mark.parametrize("scope", ["bridged", True, None])
def test_invalid_scope_is_rejected(scope):
    with pytest.raises(ValueError):
        tx_group_candidate(
            config(),
            {"primary_radio_id": "bay", "radio_ids": ["rak", "bay"], "scope": scope},
            runtime(),
        )


def test_running_scope_change_requires_restart():
    from repeater.network_topology import topology_from_config

    cfg = config()
    cfg["fabric"]["topology"]["tx_groups"] = {"bay": ["rak", "bay"]}
    for entry in cfg["radios"]:
        entry.setdefault("radio", {}).setdefault("frequency", 909875000)
    running = topology_from_config(cfg)
    cfg["fabric"]["topology"]["tx_group_scopes"] = {"bay": "crossings"}
    view = tx_group_view(cfg, running)
    assert view["restart_required"] is True


def test_shadow_radios_are_not_offered_or_accepted_as_transmitters():
    cfg = config()
    cfg["radios"][2]["role"] = "shadow"
    row = next(r for r in tx_group_view(cfg)["groups"] if r["primary_radio_id"] == "bay")
    assert [m["id"] for m in row["members"]] == ["bay"]
    with pytest.raises(ValueError, match="Shadow"):
        tx_group_candidate(cfg, {"primary_radio_id": "bay", "radio_ids": ["bay", "rak"]}, runtime())


def test_gap_is_kept_when_resaving_a_pair_and_dropped_for_a_single_radio():
    cfg = config()
    cfg["fabric"]["topology"]["tx_groups"] = {"bay": ["bay", "rak"]}
    cfg["fabric"]["topology"]["tx_group_gaps_ms"] = {"bay": [300, 1200]}
    patch = tx_group_candidate(
        cfg, {"primary_radio_id": "bay", "radio_ids": ["rak", "bay"]}, runtime()
    )
    assert patch["fabric"]["topology"]["tx_group_gaps_ms"] == {"bay": [300, 1200]}
    patch = tx_group_candidate(cfg, {"primary_radio_id": "bay", "radio_ids": ["bay"]}, {})
    assert "tx_group_gaps_ms" not in patch["fabric"]["topology"]
