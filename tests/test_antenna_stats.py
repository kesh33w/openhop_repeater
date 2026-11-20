"""/antenna_stats: compare the antennas on each network from stored history."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.api_endpoints import APIEndpoints

GROUPS = {"smf909": ["g3909", "rak909"], "bay910": ["g3910", "rak910"]}
ROLES = {"g3909": "transceiver", "rak909": "receiver", "g3910": "transceiver", "rak910": "receiver"}


def _rx(handler, radio, packet_hash, rssi, snr, *, duplicate=False, upstream=None, ts=None):
    return handler.store_packet(
        {
            "timestamp": ts or time.time() - 60,
            "type": 1,
            "route": 1,
            "length": 40,
            "transmitted": False,
            "is_duplicate": duplicate,
            "rx_radio_id": radio,
            "packet_hash": packet_hash,
            "rssi": rssi,
            "snr": snr,
            "upstream_hash": upstream,
        }
    )


@pytest.fixture
def db(tmp_path):
    h = SQLiteHandler(tmp_path)
    # 909: P1 heard by both (RAK 6 dB stronger), P2 only by the RAK, P3 only by the G3.
    _rx(h, "g3909", "P1", -100, -2.0, upstream="aa")
    _rx(h, "rak909", "P1", -94, 3.0, duplicate=True, upstream="aa")
    _rx(h, "rak909", "P2", -110, -8.0, upstream="bb")
    _rx(h, "rak909", "P2", -105, -6.0, duplicate=True, upstream="cc")
    _rx(h, "g3909", "P3", -90, 5.0, upstream="dd")
    # 910: one packet heard by the G3 only; a retired radio id is ignored.
    _rx(h, "g3910", "P4", -80, 8.0)
    _rx(h, "bay", "P5", -80, 8.0, upstream="ee")
    # Outside the window.
    _rx(h, "rak909", "OLD", -90, 1.0, ts=time.time() - 3 * 3600)
    now = time.time() - 30
    for radio, value in (("rak909", -84.0), ("rak909", -82.0), ("g3909", -100.0), ("bay", -50.0)):
        h.store_noise_floor({"timestamp": now, "noise_floor_dbm": value, "radio_id": radio})
    h.store_crc_errors({"timestamp": now, "count": 6, "radio_id": "rak909"})
    pid = _rx(h, "g3909", "P6", -95, 1.0)
    h.store_packet_egress(
        pid,
        now,
        [
            {
                "radio_id": "rak909",
                "success": True,
                "lbt_channel_busy": True,
                "lbt_backoff_ms_total": 400,
            },
            {"radio_id": "g3909", "success": False, "lbt_backoff_ms_total": 0},
        ],
    )
    return h


def _stats(db):
    now = time.time()
    return db.get_antenna_stats(now - 3600, now, groups=GROUPS, roles=ROLES)


def test_heard_by_combinations_and_unique_packets(db):
    stats = _stats(db)
    smf = next(n for n in stats["networks"] if n["network"] == "smf909")
    combos = {tuple(c["radios"]): c["packets"] for c in smf["heard_by"]}

    assert combos == {("g3909", "rak909"): 1, ("rak909",): 1, ("g3909",): 2}
    assert smf["packets"] == 4
    radios = {r["radio_id"]: r for r in stats["radios"]}
    assert radios["rak909"]["unique_packets"] == 1
    assert radios["g3909"]["unique_packets"] == 2
    assert "bay" not in radios


def test_signal_gap_uses_packets_both_heard(db):
    smf = next(n for n in _stats(db)["networks"] if n["network"] == "smf909")
    gap = smf["signal_gaps"][0]
    assert (gap["a"], gap["b"], gap["packets"]) == ("g3909", "rak909", 1)
    assert gap["rssi_db"] == -6.0 and gap["snr_db"] == -5.0


def test_per_radio_reception_noise_crc_and_tx(db):
    r = {x["radio_id"]: x for x in _stats(db)["radios"]}

    rak = r["rak909"]
    assert (rak["received"], rak["duplicates"]) == (3, 2)
    assert rak["noise"] == {"avg": -83.0, "p10": -84.0, "p90": -82.0, "samples": 2}
    assert rak["crc_errors"] == 6 and rak["crc_errors_per_hour"] == 6.0
    assert (rak["sends"], rak["busy_holds"], rak["backoff_avg_ms"]) == (1, 1, 400.0)
    assert r["g3909"]["send_failures"] == 1
    assert r["g3910"]["noise"]["avg"] is None
    assert r["rak909"]["role"] == "receiver" and r["rak909"]["network"] == "smf909"


def test_upstream_repeaters_unique_to_an_antenna(db):
    r = {x["radio_id"]: x for x in _stats(db)["radios"]}
    # rak909 heard aa, bb, cc; g3909 heard aa, dd.
    assert (r["rak909"]["upstream_repeaters"], r["rak909"]["unique_upstream_repeaters"]) == (3, 2)
    assert (r["g3909"]["upstream_repeaters"], r["g3909"]["unique_upstream_repeaters"]) == (2, 1)


@pytest.fixture
def cherrypy_ctx(monkeypatch):
    monkeypatch.setattr(
        cherrypy, "request", SimpleNamespace(method="GET", params={}), raising=False
    )
    monkeypatch.setattr(
        cherrypy, "response", SimpleNamespace(headers={}, status=200), raising=False
    )


def _api(handler, config=None):
    api = APIEndpoints.__new__(APIEndpoints)
    api.config = config or {}
    api.daemon_instance = SimpleNamespace(repeater_handler=handler)
    return api


def test_endpoint_groups_radios_by_topology(cherrypy_ctx):
    storage = SimpleNamespace(get_antenna_stats=MagicMock(return_value={"hours": 168}))
    topology = SimpleNamespace(
        members={
            "g3909": ("smf909", "transceiver"),
            "g3910": ("bay910", "transceiver"),
            "rak910": ("bay910", "receiver"),
        }
    )
    result = _api(SimpleNamespace(storage=storage, network_topology=topology)).antenna_stats(
        hours="900"
    )

    assert result["success"] is True
    args, kwargs = storage.get_antenna_stats.call_args
    assert args[1] - args[0] == 168 * 3600
    assert kwargs["groups"] == {"smf909": ["g3909"], "bay910": ["g3910", "rak910"]}
    assert kwargs["roles"]["rak910"] == "receiver"


def test_endpoint_without_topology_uses_configured_radios(cherrypy_ctx, monkeypatch):
    storage = SimpleNamespace(get_antenna_stats=MagicMock(return_value={"hours": 24}))
    api = _api(SimpleNamespace(storage=storage, network_topology=None))
    monkeypatch.setattr(
        api, "_active_radio_profiles", lambda: [{"radio_id": "a"}, {"radio_id": "b"}]
    )

    api.antenna_stats()

    assert storage.get_antenna_stats.call_args.kwargs["groups"] == {"all": ["a", "b"]}


def test_endpoint_errors(cherrypy_ctx):
    assert _api(SimpleNamespace()).antenna_stats(hours="x")["success"] is False
    storage = SimpleNamespace(get_antenna_stats=MagicMock(return_value=None))
    topology = SimpleNamespace(members={"a": ("n", "transceiver")})
    result = _api(SimpleNamespace(storage=storage, network_topology=topology)).antenna_stats()
    assert result["success"] is False
