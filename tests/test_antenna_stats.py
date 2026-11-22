"""/antenna_stats: compare the antennas on each network from stored history."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.web.api_endpoints import APIEndpoints

GROUPS = {"smf909": ["g3909", "rak909"], "bay910": ["g3910", "rak910", "v3910"]}
ROLES = {
    "g3909": "transceiver",
    "rak909": "receiver",
    "g3910": "transceiver",
    "rak910": "receiver",
    "v3910": "shadow",
}
TX_SCOPES = {"g3909": "all", "rak909": "crossings", "g3910": "all", "rak910": "all"}


def _rx(
    handler, radio, packet_hash, rssi, snr, *, duplicate=False, upstream=None, ts=None, reason=None
):
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
            "drop_reason": reason,
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
    # P7: both heard it, but from different relays; coverage yes, signal no.
    _rx(h, "g3909", "P7", -60, 9.0, upstream="xx")
    _rx(h, "rak909", "P7", -120, -12.0, upstream="yy")
    # P8: a neighbour repeating a packet we bridged; left out of coverage and signal.
    echo = "Channel bridge echo"
    _rx(h, "g3909", "P8", -70, 6.0, upstream="aa", reason=echo)
    _rx(h, "rak909", "P8", -40, 12.0, upstream="aa", reason=echo)
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
    return db.get_antenna_stats(now - 3600, now, groups=GROUPS, roles=ROLES, tx_scopes=TX_SCOPES)


def test_heard_by_combinations_and_unique_packets(db):
    stats = _stats(db)
    smf = next(n for n in stats["networks"] if n["network"] == "smf909")
    combos = {tuple(c["radios"]): c["packets"] for c in smf["heard_by"]}

    assert combos == {("g3909", "rak909"): 2, ("rak909",): 1, ("g3909",): 2}
    assert smf["packets"] == 5
    radios = {r["radio_id"]: r for r in stats["radios"]}
    assert radios["rak909"]["unique_packets"] == 1
    assert radios["g3909"]["unique_packets"] == 2
    assert "bay" not in radios


def test_signal_gap_compares_only_the_same_frame(db):
    stats = _stats(db)
    smf = next(n for n in stats["networks"] if n["network"] == "smf909")
    gap = smf["signal_gaps"][0]
    # Only P1 (same relay, same moment); P7's two relays and P8's echoes are left out.
    assert (gap["a"], gap["b"], gap["frames"]) == ("g3909", "rak909", 1)
    assert gap["rssi_db"] == -6.0 and gap["snr_db"] == -5.0
    assert stats["match_window_seconds"] == 1.0
    bay = next(n for n in stats["networks"] if n["network"] == "bay910")
    assert all(g["frames"] == 0 and g["rssi_db"] is None for g in bay["signal_gaps"])


def test_same_frame_needs_matching_time(tmp_path):
    h = SQLiteHandler(tmp_path)
    now = time.time() - 60
    _rx(h, "g3909", "Q", -100, 0.0, upstream="aa", ts=now)
    _rx(h, "rak909", "Q", -90, 0.0, upstream="aa", ts=now + 5)
    stats = h.get_antenna_stats(now - 60, now + 60, groups=GROUPS, roles=ROLES)
    smf = next(n for n in stats["networks"] if n["network"] == "smf909")
    assert smf["signal_gaps"][0]["frames"] == 0
    assert smf["heard_by"] == [{"radios": ["g3909", "rak909"], "packets": 1}]


def test_radio_states_show_tx_membership_and_missing_data(db):
    r = {x["radio_id"]: x for x in _stats(db)["radios"]}
    assert (r["g3909"]["transmits"], r["rak909"]["transmits"]) == ("all", "crossings")
    assert r["v3910"]["transmits"] is None
    assert r["v3910"]["recorded"] is False and r["g3909"]["recorded"] is True
    assert r["rak910"]["has_data"] is False and r["g3910"]["has_data"] is True


def test_per_radio_reception_noise_crc_and_tx(db):
    r = {x["radio_id"]: x for x in _stats(db)["radios"]}

    rak = r["rak909"]
    assert (rak["received"], rak["duplicates"], rak["bridge_echoes"]) == (5, 2, 1)
    # The -40 dBm echo is not part of the average.
    assert rak["rssi_avg"] < -100
    assert rak["noise"] == {"avg": -83.0, "p10": -84.0, "p90": -82.0, "samples": 2}
    assert rak["crc_errors"] == 6 and rak["crc_errors_per_hour"] == 6.0
    assert (rak["sends"], rak["busy_holds"], rak["backoff_avg_ms"]) == (1, 1, 400.0)
    assert r["g3909"]["send_failures"] == 1
    assert r["g3910"]["noise"]["avg"] is None
    assert r["rak909"]["role"] == "receiver" and r["rak909"]["network"] == "smf909"


def test_upstream_repeaters_unique_to_an_antenna(db):
    r = {x["radio_id"]: x for x in _stats(db)["radios"]}
    # rak909 heard aa, bb, cc, yy; g3909 heard aa, dd, xx (echoes excluded).
    assert (r["rak909"]["upstream_repeaters"], r["rak909"]["unique_upstream_repeaters"]) == (4, 3)
    assert (r["g3909"]["upstream_repeaters"], r["g3909"]["unique_upstream_repeaters"]) == (3, 2)


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
        networks={"smf909": "g3909", "bay910": "g3910"},
        members={
            "g3909": ("smf909", "transceiver"),
            "rak909": ("smf909", "receiver"),
            "g3910": ("bay910", "transceiver"),
            "rak910": ("bay910", "receiver"),
        },
        tx_groups={"g3909": ("g3909", "rak909"), "g3910": ("g3910",)},
        tx_group_scopes={"g3909": "crossings"},
    )
    result = _api(SimpleNamespace(storage=storage, network_topology=topology)).antenna_stats(
        hours="900"
    )

    assert result["success"] is True and result["data"]["topology_source"] == "running"
    args, kwargs = storage.get_antenna_stats.call_args
    assert args[1] - args[0] == 168 * 3600
    assert kwargs["groups"] == {"smf909": ["g3909", "rak909"], "bay910": ["g3910", "rak910"]}
    assert kwargs["roles"]["rak910"] == "receiver"
    # rak910 is a receiver outside any TX group: it must not be reported as sending.
    assert kwargs["tx_scopes"] == {"g3909": "all", "g3910": "all", "rak909": "crossings"}


def test_endpoint_without_topology_uses_configured_radios(cherrypy_ctx, monkeypatch):
    storage = SimpleNamespace(get_antenna_stats=MagicMock(return_value={"hours": 24}))
    api = _api(SimpleNamespace(storage=storage, network_topology=None))
    monkeypatch.setattr(
        api, "_active_radio_profiles", lambda: [{"radio_id": "a"}, {"radio_id": "b"}]
    )

    api.antenna_stats()

    assert storage.get_antenna_stats.call_args.kwargs["groups"] == {"all": ["a", "b"]}
    assert storage.get_antenna_stats.call_args.kwargs["tx_scopes"] is None


def test_endpoint_errors(cherrypy_ctx):
    assert _api(SimpleNamespace()).antenna_stats(hours="x")["success"] is False
    storage = SimpleNamespace(get_antenna_stats=MagicMock(return_value=None))
    topology = SimpleNamespace(networks={"n": "a"}, members={"a": ("n", "transceiver")})
    result = _api(SimpleNamespace(storage=storage, network_topology=topology)).antenna_stats()
    assert result["success"] is False


def test_same_frame_query_uses_indexes(db):
    from repeater.antenna_stats import MATCHED_FRAMES_QUERY

    params = {"window": 1.0, "start": 0, "end": time.time(), "radios": '["g3909", "rak909"]'}
    with db._connect() as conn:
        plan = str(conn.execute("EXPLAIN QUERY PLAN " + MATCHED_FRAMES_QUERY, params).fetchall())
    assert "idx_packets_hash" in plan and "SCAN a" not in plan


def test_crc_after_sends_fits_errors_per_send():
    from repeater.antenna_stats import CRC_MIN_BUCKETS, crc_after_sends

    sampled = set(range(48))
    # 2 CRC errors per send plus 3 per bucket regardless of sends.
    sends = {b: b % 4 for b in sampled}
    crc = {b: 3 + 2 * sends[b] for b in sampled}
    est = crc_after_sends(crc, sends, sampled)
    assert est["per_send"] == 2.0
    assert est["estimated"] == 2 * sum(sends.values())
    assert est["share"] == round(est["estimated"] / sum(crc.values()), 2)
    assert est["other_per_hour"] == 36.0  # 3 per five minutes
    assert est["sampled_hours"] == 4.0

    # Buckets where the radio wasn't sampled are left out, not counted as zero.
    noisy_crc = {**crc, 100: 50, 101: 0}
    noisy_sends = {**sends, 100: 0, 101: 9}
    assert crc_after_sends(noisy_crc, noisy_sends, sampled)["per_send"] == 2.0
    # Too few buckets, no variation in sends, or no errors: no estimate.
    assert crc_after_sends(crc, sends, set(range(CRC_MIN_BUCKETS - 1))) is None
    assert crc_after_sends(crc, {b: 1 for b in sampled}, sampled) is None
    assert crc_after_sends({}, sends, sampled) is None
    # Errors that fall when we send less never give a negative estimate.
    inverse = {b: 9 - 2 * sends[b] for b in sampled}
    assert crc_after_sends(inverse, sends, sampled)["estimated"] == 0


def test_crc_after_sends_uses_network_sends(tmp_path):
    h = SQLiteHandler(tmp_path)
    end = time.time()
    start = end - 3 * 3600
    for i in range(36):  # 3 h of 5-minute buckets
        ts = start + i * 300 + 150
        sends = i % 3
        for radio in ("g3909", "rak909", "g3910"):
            h.store_noise_floor({"timestamp": ts, "noise_floor_dbm": -100.0, "radio_id": radio})
        # rak909 errors follow sends by g3909 (same network) only.
        h.store_crc_errors({"timestamp": ts + 1, "count": 1 + 2 * sends, "radio_id": "rak909"})
        h.store_crc_errors({"timestamp": ts + 1, "count": 4, "radio_id": "g3910"})
        for _ in range(sends):
            pid = _rx(h, "g3909", f"S{i}", -95, 1.0, ts=ts)
            h.store_packet_egress(pid, ts, [{"radio_id": "g3909", "success": True}])
    stats = h.get_antenna_stats(start, end, groups=GROUPS, roles=ROLES)
    r = {x["radio_id"]: x for x in stats["radios"]}
    assert stats["crc_bucket_seconds"] == 300
    assert r["rak909"]["crc_after_sends"]["per_send"] == 2.0
    assert r["rak909"]["crc_after_sends"]["other_per_hour"] == 12.0
    # 910 had no sends at all, so nothing to fit; rak910 has no samples.
    assert r["g3910"]["crc_after_sends"] is None
    assert r["rak910"]["crc_after_sends"] is None
