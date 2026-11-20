"""/bridge_stats: bridged vs local traffic per topology network, from stored history."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.api_endpoints import APIEndpoints

NETWORKS = {"smf909": "g3909", "bay910": "g3910"}
MEMBERS = {"g3909": "smf909", "rak909": "smf909", "g3910": "bay910", "rak910": "bay910"}


def _store(handler, egress=(), **overrides):
    record = {
        "timestamp": time.time() - 60,
        "type": 1,
        "route": 1,
        "length": 40,
        "transmitted": False,
        "is_duplicate": False,
    }
    record.update(overrides)
    packet_id = handler.store_packet(record)
    handler.store_packet_egress(
        packet_id,
        record["timestamp"],
        [{"radio_id": rid, "success": ok} for rid, ok in egress],
    )


@pytest.fixture
def db(tmp_path):
    handler = SQLiteHandler(tmp_path)
    # Heard on rak910, bridged to 909 by the g3909 group (two antennas).
    _store(
        handler,
        rx_radio_id="rak910",
        transmitted=True,
        tx_radio_id="g3909",
        tx_radio_ids=["g3909"],
        egress=[("rak909", True), ("g3909", True)],
    )
    # Heard on g3910, sent on both networks; the rak910 leg failed.
    _store(
        handler,
        rx_radio_id="g3910",
        transmitted=True,
        tx_radio_id="g3910",
        tx_radio_ids=["g3910", "g3909"],
        egress=[("rak910", False), ("g3910", True), ("g3909", True)],
    )
    # Heard on 909, repeated locally.
    _store(
        handler,
        rx_radio_id="g3909",
        transmitted=True,
        tx_radio_id="g3909",
        tx_radio_ids=["g3909"],
        egress=[("g3909", True)],
    )
    # Heard on 909, not sent; plus a duplicate that must not count as heard.
    _store(handler, rx_radio_id="rak909", drop_reason="Channel bridge echo")
    _store(handler, rx_radio_id="g3909", is_duplicate=True, drop_reason="Duplicate")
    # Originated by this node on 910.
    _store(
        handler,
        rx_radio_id=None,
        transmitted=True,
        tx_radio_id="g3910",
        tx_radio_ids=["g3910"],
        egress=[("g3910", True)],
    )
    # A radio from before a rename is unattributed, not guessed.
    _store(
        handler,
        rx_radio_id="bay",
        transmitted=True,
        tx_radio_id="g3909",
        egress=[("g3909", True)],
    )
    # Outside the window.
    _store(handler, timestamp=time.time() - 3 * 3600, rx_radio_id="g3910")
    return handler


def _stats(db, aliases=None):
    now = time.time()
    return db.get_bridge_stats(
        now - 3600, now, 300, networks=NETWORKS, members=MEMBERS, aliases=aliases
    )


def test_networks_split_bridged_local_and_not_sent(db):
    nets = {n["network"]: n for n in _stats(db)["networks"]}

    smf, bay = nets["smf909"], nets["bay910"]
    assert (smf["heard"], smf["repeated_local"], smf["bridged_out"], smf["not_sent"]) == (
        2,
        1,
        0,
        1,
    )
    assert smf["reasons"] == [{"reason": "Channel bridge echo", "count": 1}]
    assert (bay["heard"], bay["repeated_local"], bay["bridged_out"], bay["not_sent"]) == (
        2,
        1,
        2,
        0,
    )
    assert bay["originated"] == 1
    assert set(smf["radios"]) == {"g3909", "rak909"} and smf["tx_radio"] == "g3909"


def test_directions_count_packets_not_antenna_legs(db):
    stats = _stats(db)
    directions = {(d["from"], d["to"]): d for d in stats["directions"]}

    assert directions[("bay910", "smf909")]["packets"] == 2
    assert directions[("smf909", "bay910")]["packets"] == 0
    series = directions[("bay910", "smf909")]["buckets"]
    assert sum(b["count"] for b in series) == 2
    assert all(b["timestamp"] % 300 == 0 for b in series)
    assert {lo["network"]: lo["packets"] for lo in stats["local"]} == {"smf909": 1, "bay910": 1}


def test_radio_pairs_count_physical_sends_with_antenna_codes(db):
    aliases = {("rak910", "rak909"): {"prefix": "9c5b", "crossing_prefix": "1ff7"}}
    pairs = {(p["rx_radio"], p["tx_radio"]): p for p in _stats(db, aliases)["radio_pairs"]}

    assert pairs[("rak910", "rak909")]["sent"] == 1
    assert pairs[("rak910", "rak909")]["bridged"] is True
    assert pairs[("rak910", "rak909")]["antenna_code"] == "9c5b"
    assert (pairs[("g3910", "rak910")]["sent"], pairs[("g3910", "rak910")]["failed"]) == (0, 1)
    assert pairs[("g3910", "rak910")]["bridged"] is False
    assert pairs[("g3910", "rak910")]["antenna_code"] is None
    assert pairs[(None, "g3910")]["rx_network"] is None


def test_retired_radio_ids_are_unattributed(db):
    stats = _stats(db)
    assert stats["unattributed"] == {"packets": 1, "transmissions": 1}


def test_week_window_uses_indexes(db):
    from repeater.bridge_stats import BRIDGE_EGRESS_QUERY, BRIDGE_PACKETS_QUERY

    now = time.time()
    with db._connect() as conn:
        plan = str(
            conn.execute(
                "EXPLAIN QUERY PLAN " + BRIDGE_PACKETS_QUERY, (3600, 3600, now - 604800, now)
            ).fetchall()
        )
        egress_plan = str(
            conn.execute(
                "EXPLAIN QUERY PLAN " + BRIDGE_EGRESS_QUERY, (now - 604800, now)
            ).fetchall()
        )
    assert "idx_packets_" in plan and "SCAN packets" not in plan
    assert "idx_packet_egress_time_radio" in egress_plan


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    monkeypatch.setattr(
        cherrypy, "request", SimpleNamespace(method="GET", params={}), raising=False
    )
    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )


def _api(handler):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = {}
    api.daemon_instance = SimpleNamespace(repeater_handler=handler)
    return api


def test_endpoint_passes_topology_and_aliases(cherrypy_ctx):
    storage = SimpleNamespace(get_bridge_stats=MagicMock(return_value={"configured": True}))
    topology = SimpleNamespace(
        networks=NETWORKS,
        members={"g3909": ("smf909", "transceiver"), "rak910": ("bay910", "receiver")},
    )
    identities = SimpleNamespace(
        table=lambda: [
            {"prefix": "9c5b", "crossing_prefix": "1ff7", "rx": "rak910", "tx": "rak909"},
            {"prefix": "8859", "crossing_prefix": "52be", "rx": None, "tx": "rak909"},
        ]
    )
    api = _api(
        SimpleNamespace(storage=storage, network_topology=topology, tx_identities=identities)
    )

    result = api.bridge_stats(hours="500")

    assert result["success"] is True and result["data"]["hours"] == 168
    args, kwargs = storage.get_bridge_stats.call_args
    assert args[1] - args[0] == 168 * 3600 and args[2] == 3600
    assert kwargs["members"] == {"g3909": "smf909", "rak910": "bay910"}
    assert list(kwargs["aliases"]) == [("rak910", "rak909")]


def test_endpoint_without_topology_reports_unconfigured(cherrypy_ctx):
    storage = SimpleNamespace(get_bridge_stats=MagicMock())
    api = _api(SimpleNamespace(storage=storage, network_topology=None, tx_identities=None))

    result = api.bridge_stats(hours="2")

    assert result["success"] is True
    assert result["data"]["configured"] is False and result["data"]["bucket_seconds"] == 300
    storage.get_bridge_stats.assert_not_called()


def test_endpoint_rejects_bad_hours(cherrypy_ctx):
    api = _api(SimpleNamespace(storage=None, network_topology=None))
    assert api.bridge_stats(hours="x")["success"] is False
