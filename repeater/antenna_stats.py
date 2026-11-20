"""Per-antenna comparison statistics from the stored history.

Answers "what does each antenna add?" for nodes with several radios, using
tables the repeater already writes:

* ``packets``: which radios heard each packet (by ``packet_hash``), with RSSI and
  SNR, and which repeater relayed it to us (``upstream_hash``);
* ``noise_floor`` and ``crc_errors``: per-radio channel health samples;
* ``packet_egress``: per-radio sends, failures and listen-before-talk holds.

Radios are compared within their topology network (antennas on the same
frequency). Rows from radios that are no longer configured are ignored.

Two different questions, answered separately:

* **Coverage** counts distinct packets: which antennas heard each message at
  all, whichever relay they heard it from.
* **Signal** compares only the *same RF frame*: the same packet from the same
  relay, heard by both antennas within ``MATCH_WINDOW_SECONDS``. Comparing best
  readings of different relays would measure the relays, not the antennas.

Both leave out "Channel bridge echo" receptions: neighbours repeating a packet
this node bridged onto their network. Those are counted per radio instead.
"""

from __future__ import annotations

from itertools import combinations
from typing import Dict, Iterable, List, Optional

# Neighbours relaying a packet this node bridged; not mesh traffic for the network.
ECHO = "IFNULL(drop_reason, '') LIKE 'Channel bridge echo%'"
MATCH_WINDOW_SECONDS = 1.0

# Coverage: which radios heard each distinct packet.
RECEPTION_QUERY = (
    "SELECT DISTINCT packet_hash, rx_radio_id FROM packets "
    "WHERE timestamp >= ? AND timestamp <= ? AND rx_radio_id IS NOT NULL "
    f"AND packet_hash IS NOT NULL AND NOT {ECHO}"
)

RADIO_RX_QUERY = (
    "SELECT rx_radio_id, COUNT(*) AS n, SUM(is_duplicate) AS duplicates, "
    f"SUM({ECHO}) AS echoes, AVG(CASE WHEN {ECHO} THEN NULL ELSE rssi END) AS rssi, "
    f"AVG(CASE WHEN {ECHO} THEN NULL ELSE snr END) AS snr FROM packets "
    "WHERE timestamp >= ? AND timestamp <= ? AND rx_radio_id IS NOT NULL GROUP BY rx_radio_id"
)

UPSTREAM_QUERY = (
    "SELECT rx_radio_id, upstream_hash, COUNT(*) AS n FROM packets "
    "WHERE timestamp >= ? AND timestamp <= ? AND rx_radio_id IS NOT NULL "
    f"AND upstream_hash IS NOT NULL AND NOT {ECHO} GROUP BY rx_radio_id, upstream_hash"
)


def matched_frames_query(radio_count: int) -> str:
    """Pairs of receptions of the same RF frame on two radios, summed per radio pair.

    Parameters: match window seconds, start, end, then the radio ids twice.
    ``idx_packets_hash`` serves the join.
    """
    marks = ", ".join("?" * radio_count)
    echo_a, echo_b = (
        ECHO.replace("drop_reason", "a.drop_reason"),
        ECHO.replace("drop_reason", "b.drop_reason"),
    )
    return (
        "SELECT a.rx_radio_id AS a, b.rx_radio_id AS b, COUNT(*) AS n, "
        "SUM(a.rssi - b.rssi) AS rssi_sum, COUNT(a.rssi - b.rssi) AS rssi_n, "
        "SUM(a.snr - b.snr) AS snr_sum, COUNT(a.snr - b.snr) AS snr_n "
        "FROM packets a JOIN packets b ON b.packet_hash = a.packet_hash "
        "AND b.rx_radio_id > a.rx_radio_id "
        "AND UPPER(IFNULL(b.upstream_hash, '')) = UPPER(IFNULL(a.upstream_hash, '')) "
        "AND ABS(b.timestamp - a.timestamp) <= ? "
        "WHERE a.timestamp >= ? AND a.timestamp <= ? AND a.packet_hash IS NOT NULL "
        f"AND a.rx_radio_id IN ({marks}) AND b.rx_radio_id IN ({marks}) "
        f"AND NOT {echo_a} AND NOT {echo_b} "
        "GROUP BY a.rx_radio_id, b.rx_radio_id"
    )


NOISE_QUERY = (
    "SELECT radio_id, noise_floor_dbm FROM noise_floor "
    "WHERE timestamp >= ? AND timestamp <= ? AND radio_id IS NOT NULL"
)

CRC_QUERY = (
    "SELECT radio_id, SUM(count) AS n FROM crc_errors "
    "WHERE timestamp >= ? AND timestamp <= ? AND radio_id IS NOT NULL GROUP BY radio_id"
)

EGRESS_QUERY = (
    "SELECT radio_id, COUNT(*) AS n, SUM(success = 0) AS failed, "
    "SUM(lbt_channel_busy) AS busy, AVG(lbt_backoff_ms_total) AS backoff "
    "FROM packet_egress WHERE timestamp >= ? AND timestamp <= ? AND radio_id IS NOT NULL "
    "GROUP BY radio_id"
)


def _round(value, digits=1):
    return None if value is None else round(float(value), digits)


def _percentile(sorted_values: List[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return round(sorted_values[index], 1)


def fold_antenna_stats(
    *,
    reception: Iterable,
    radio_rx: Iterable,
    upstream: Iterable,
    noise: Iterable,
    crc: Iterable,
    egress: Iterable,
    matched: Iterable = (),
    groups: Dict[str, List[str]],
    roles: Dict[str, Optional[str]],
    tx_scopes: Optional[Dict[str, Optional[str]]] = None,
    hours: int,
) -> dict:
    """Fold query rows into the ``/antenna_stats`` payload.

    ``groups`` maps network name -> radio ids on it, in display order.
    ``tx_scopes`` maps radio id -> "all" or "crossings" for radios the running
    topology transmits on; None (the whole mapping) means unknown.
    """
    network_of = {rid: net for net, rids in groups.items() for rid in rids}
    radios = {
        rid: {
            "radio_id": rid,
            "network": network_of[rid],
            "role": roles.get(rid),
            # Shadow radios are compared live but their receptions are not archived.
            "recorded": roles.get(rid) != "shadow",
            "transmits": tx_scopes.get(rid) if tx_scopes is not None else None,
            "has_data": False,
            "received": 0,
            "duplicates": 0,
            "bridge_echoes": 0,
            "rssi_avg": None,
            "snr_avg": None,
            "unique_packets": 0,
            "noise": {"avg": None, "p10": None, "p90": None, "samples": 0},
            "crc_errors": 0,
            "crc_errors_per_hour": 0.0,
            "upstream_repeaters": 0,
            "unique_upstream_repeaters": 0,
            "sends": 0,
            "send_failures": 0,
            "busy_holds": 0,
            "backoff_avg_ms": None,
        }
        for rid in network_of
    }

    for row in radio_rx:
        r = radios.get(row["rx_radio_id"])
        if r:
            r["received"] = int(row["n"])
            r["duplicates"] = int(row["duplicates"] or 0)
            r["bridge_echoes"] = int(row["echoes"] or 0)
            r["rssi_avg"] = _round(row["rssi"])
            r["snr_avg"] = _round(row["snr"])

    # Which radios heard each packet, per network.
    heard: Dict[str, Dict[str, set]] = {net: {} for net in groups}
    for row in reception:
        rid = row["rx_radio_id"]
        net = network_of.get(rid)
        if net is None:
            continue
        heard[net].setdefault(row["packet_hash"], set()).add(rid)

    # Same-frame signal differences; the query orders each pair by radio id.
    same_frame = {(row["a"], row["b"]): row for row in matched}

    def gap(a, b):
        row, sign = same_frame.get((a, b)), 1
        if row is None and (b, a) in same_frame:
            row, sign = same_frame[(b, a)], -1
        if row is None:
            return {"a": a, "b": b, "frames": 0, "rssi_db": None, "snr_db": None}
        return {
            "a": a,
            "b": b,
            "frames": int(row["n"]),
            "rssi_db": _round(sign * row["rssi_sum"] / row["rssi_n"]) if row["rssi_n"] else None,
            "snr_db": _round(sign * row["snr_sum"] / row["snr_n"]) if row["snr_n"] else None,
        }

    networks = []
    for net, rids in groups.items():
        combos: Dict[tuple, int] = {}
        for copies in heard[net].values():
            key = tuple(rid for rid in rids if rid in copies)
            combos[key] = combos.get(key, 0) + 1
            if len(key) == 1:
                radios[key[0]]["unique_packets"] += 1
        networks.append(
            {
                "network": net,
                "radios": list(rids),
                "packets": len(heard[net]),
                "heard_by": [
                    {"radios": list(key), "packets": n}
                    for key, n in sorted(combos.items(), key=lambda kv: (-len(kv[0]), -kv[1]))
                ],
                "signal_gaps": [gap(a, b) for a, b in combinations(rids, 2)],
            }
        )

    # Upstream repeaters: how many each antenna hears, and how many only it hears.
    upstream_sets: Dict[str, set] = {rid: set() for rid in radios}
    for row in upstream:
        rid = row["rx_radio_id"]
        if rid in upstream_sets:
            upstream_sets[rid].add(str(row["upstream_hash"]).lower())
    for rid, seen in upstream_sets.items():
        others = set().union(*(upstream_sets[o] for o in groups[network_of[rid]] if o != rid))
        radios[rid]["upstream_repeaters"] = len(seen)
        radios[rid]["unique_upstream_repeaters"] = len(seen - others)

    samples: Dict[str, List[float]] = {rid: [] for rid in radios}
    for row in noise:
        values = samples.get(row["radio_id"])
        if values is not None and row["noise_floor_dbm"] is not None:
            values.append(float(row["noise_floor_dbm"]))
    for rid, values in samples.items():
        values.sort()
        if values:
            radios[rid]["noise"] = {
                "avg": _round(sum(values) / len(values)),
                "p10": _percentile(values, 0.10),
                "p90": _percentile(values, 0.90),
                "samples": len(values),
            }

    for row in crc:
        r = radios.get(row["radio_id"])
        if r:
            r["crc_errors"] = int(row["n"] or 0)
            r["crc_errors_per_hour"] = _round(r["crc_errors"] / max(1, hours))

    for row in egress:
        r = radios.get(row["radio_id"])
        if r:
            r["sends"] = int(row["n"])
            r["send_failures"] = int(row["failed"] or 0)
            r["busy_holds"] = int(row["busy"] or 0)
            r["backoff_avg_ms"] = _round(row["backoff"], 0)

    for r in radios.values():
        r["has_data"] = bool(
            r["received"] or r["sends"] or r["noise"]["samples"] or r["crc_errors"]
        )
    return {
        "hours": hours,
        "match_window_seconds": MATCH_WINDOW_SECONDS,
        "radios": list(radios.values()),
        "networks": networks,
    }
