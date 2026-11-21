"""/relay_stats: which copy of a double-sent packet neighbours relayed."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import cherrypy
import pytest

from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.relay_stats import RELAY_RECEPTIONS_QUERY, packet_hash_of
from repeater.web.api_endpoints import APIEndpoints

GROUPS = {"smf909": ["g3909", "rak909"], "bay910": ["g3910", "rak910"]}


def flood(payload: str, *hops: str) -> str:
    """A GRP_TXT flood with 2-byte hops, as stored (hex)."""
    return "15" + f"{0x40 | len(hops):02x}" + "".join(hops) + payload


def send(h, payload, legs, ts):
    """One logical packet sent as ordered legs [(radio, last hop code, success)]."""
    pid = h.store_packet(
        {
            "timestamp": ts,
            "type": 5,
            "route": 1,
            "length": 40,
            "transmitted": True,
            "is_duplicate": False,
            "rx_radio_id": "g3910",
        }
    )
    h.store_packet_egress(
        pid,
        ts,
        [
            {"radio_id": radio, "success": ok, "raw_packet": flood(payload, "aaaa", code)}
            for radio, code, ok in legs
        ],
    )
    return packet_hash_of(flood(payload, "aaaa", legs[0][1]))


def heard(h, packet_hash, path, radio="g3909", ts=None):
    h.store_packet(
        {
            "timestamp": ts or time.time() - 30,
            "type": 5,
            "route": 1,
            "length": 40,
            "transmitted": False,
            "is_duplicate": True,
            "rx_radio_id": radio,
            "packet_hash": packet_hash,
            "original_path": path,
            "drop_reason": "Duplicate",
        }
    )


@pytest.fixture
def db(tmp_path):
    h = SQLiteHandler(tmp_path)
    now = time.time() - 120
    # P1: g3909 first (2855), rak909 second (8859). 94 relayed the first copy,
    # heard on both our radios (counted once); 7A relayed the second copy.
    p1 = send(h, "01" * 20, [("g3909", "2855", True), ("rak909", "8859", True)], now)
    heard(h, p1, ["AAAA", "2855", "0094"], radio="g3909")
    heard(h, p1, ["AAAA", "2855", "0094"], radio="rak909")
    heard(h, p1, ["AAAA", "8859", "007A"])
    # A second-hop relay still credits the copy that reached 94 first.
    heard(h, p1, ["AAAA", "2855", "0094", "0033"])
    # P2: rak909 first. 94 relayed it.
    p2 = send(h, "02" * 20, [("rak909", "8859", True), ("g3909", "2855", True)], now + 1)
    heard(h, p2, ["AAAA", "8859", "0094"])
    # P3: identity codes off; both copies identical, so not attributable.
    p3 = send(h, "03" * 20, [("g3909", "bd6e", True), ("rak909", "bd6e", True)], now + 2)
    heard(h, p3, ["AAAA", "BD6E", "0094"])
    # P4: second leg failed, so it was not a double send.
    send(h, "04" * 20, [("g3909", "2855", True), ("rak909", "8859", False)], now + 3)
    # P5: 910 pair, nobody relayed it.
    send(h, "05" * 20, [("g3910", "273e", True), ("rak910", "a71e", True)], now + 4)
    return h


def _stats(db):
    now = time.time()
    return db.get_relay_stats(now - 3600, now, groups=GROUPS)


def test_attributes_each_relay_to_the_copy_and_position_it_came_from(db):
    smf = {n["network"]: n for n in _stats(db)["networks"]}["smf909"]

    assert (smf["double_sent"], smf["tagged"], smf["untagged"], smf["relayed"]) == (3, 2, 1, 2)
    radios = {r["radio_id"]: r for r in smf["radios"]}
    g3, rak = radios["g3909"], radios["rak909"]
    assert (g3["code"], rak["code"]) == ("2855", "8859")
    assert (g3["sent_first"], rak["sent_first"]) == (2, 1)
    assert (g3["relayed"], g3["relayed_when_first"], g3["relayed_when_second"]) == (1, 1, 0)
    assert (rak["relayed"], rak["relayed_when_first"], rak["relayed_when_second"]) == (2, 1, 1)
    neighbours = {n["neighbour"]: n for n in smf["neighbours"]}
    assert neighbours["0094"] == {
        "neighbour": "0094",
        "relays": 2,
        "by_radio": {"g3909": 1, "rak909": 1},
    }
    assert neighbours["007A"]["by_radio"] == {"rak909": 1}
    assert "0033" not in neighbours


def test_network_with_no_relays(db):
    bay = {n["network"]: n for n in _stats(db)["networks"]}["bay910"]
    assert (bay["double_sent"], bay["tagged"], bay["relayed"], bay["neighbours"]) == (1, 1, 0, [])


def test_radio_without_tagged_sends_has_only_the_documented_fields(tmp_path):
    h = SQLiteHandler(tmp_path)
    send(h, "06" * 20, [("g3909", "bd6e", True), ("rak909", "bd6e", True)], time.time() - 60)
    now = time.time()
    smf = h.get_relay_stats(now - 3600, now, groups=GROUPS)["networks"][0]
    assert smf["radios"] == [
        {
            "radio_id": "g3909",
            "sent_first": 1,
            "relayed": 0,
            "relayed_when_first": 0,
            "relayed_when_second": 0,
            "code": None,
        }
    ]


def test_unreadable_history_is_an_error(tmp_path, monkeypatch):
    h = SQLiteHandler(tmp_path)

    def broken():
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(h, "_connect", broken)
    now = time.time()
    assert h.get_relay_stats(now - 3600, now, groups=GROUPS) is None


def test_reception_query_uses_the_hash_index(db):
    with db._connect() as conn:
        plan = str(
            conn.execute(
                "EXPLAIN QUERY PLAN " + RELAY_RECEPTIONS_QUERY,
                {"start": 0, "end": time.time(), "hashes": '["AB"]'},
            ).fetchall()
        )
    assert "idx_packets_hash" in plan


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


def test_endpoint_groups_radios_and_reports_settings(cherrypy_ctx):
    storage = SimpleNamespace(get_relay_stats=MagicMock(return_value={"hours": 24, "networks": []}))
    topology = SimpleNamespace(
        members={"g3909": ("smf909", "transceiver"), "rak909": ("smf909", "receiver")},
        tx_groups={"g3909": ("g3909", "rak909")},
        tx_group_orders={"g3909": "random"},
        network=lambda rid: "smf909",
    )
    handler = SimpleNamespace(
        storage=storage,
        network_topology=topology,
        tx_identities=SimpleNamespace(enabled=True, mode="tx"),
    )
    result = _api(handler).relay_stats(hours="900")

    assert result["success"] is True
    data = result["data"]
    assert (data["identity_codes"], data["identity_mode"]) == (True, "tx")
    assert data["random_order"] == {"smf909": True}
    args, kwargs = storage.get_relay_stats.call_args
    assert args[1] - args[0] == 168 * 3600
    assert kwargs["groups"] == {"smf909": ["g3909", "rak909"]}


def test_endpoint_errors_and_unconfigured(cherrypy_ctx):
    assert _api(SimpleNamespace()).relay_stats(hours="x")["success"] is False
    unconfigured = _api(SimpleNamespace(network_topology=None)).relay_stats()
    assert unconfigured["data"]["configured"] is False
    storage = SimpleNamespace(get_relay_stats=MagicMock(return_value=None))
    topology = SimpleNamespace(members={"a": ("n", "transceiver")}, tx_groups={})
    handler = SimpleNamespace(storage=storage, network_topology=topology)
    assert _api(handler).relay_stats()["success"] is False


def flood1(payload: str, *hops: str) -> str:
    """A GRP_TXT flood with 1-byte hops, as stored (hex)."""
    return "15" + f"{len(hops):02x}" + "".join(hops) + payload


def test_one_byte_codes_are_attributed_even_when_an_upstream_hop_shares_the_byte(tmp_path):
    h = SQLiteHandler(tmp_path)
    ts = time.time() - 120
    pid = h.store_packet(
        {
            "timestamp": ts,
            "type": 5,
            "route": 1,
            "length": 40,
            "transmitted": True,
            "is_duplicate": False,
            "rx_radio_id": "g3910",
        }
    )
    # Upstream hop 12 happens to equal rak909's code; our code is the last one.
    legs = [("g3909", "d4", True), ("rak909", "12", True)]
    h.store_packet_egress(
        pid,
        ts,
        [
            {"radio_id": r, "success": ok, "raw_packet": flood1("06" * 20, "12", c)}
            for r, c, ok in legs
        ],
    )
    packet_hash = packet_hash_of(flood1("06" * 20, "12", "d4"))
    heard(h, packet_hash, ["12", "D4", "94"])
    smf = {n["network"]: n for n in _stats(h)["networks"]}["smf909"]
    assert (smf["tagged"], smf["relayed"]) == (1, 1)
    radios = {r["radio_id"]: r for r in smf["radios"]}
    assert (radios["g3909"]["relayed"], radios["rak909"]["relayed"]) == (1, 0)
    assert {n["neighbour"] for n in smf["neighbours"]} == {"94"}
