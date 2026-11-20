"""Per-antenna comparison statistics from the stored history.

Answers "what does each antenna add?" for nodes with several radios, using
tables the repeater already writes:

* ``packets``: which radios heard each packet (by ``packet_hash``), with RSSI and
  SNR, and which repeater relayed it to us (``upstream_hash``);
* ``noise_floor`` and ``crc_errors``: per-radio channel health samples;
* ``packet_egress``: per-radio sends, failures and listen-before-talk holds.

Radios are compared within their topology network (antennas on the same
frequency). Rows from radios that are no longer configured are ignored.
"""

from __future__ import annotations

from itertools import combinations
from typing import Dict, Iterable, List, Optional

# One row per (packet, radio): the strongest copy that radio heard.
RECEPTION_QUERY = (
    "SELECT packet_hash, rx_radio_id, MAX(rssi) AS rssi, MAX(snr) AS snr FROM packets "
    "WHERE timestamp >= ? AND timestamp <= ? AND rx_radio_id IS NOT NULL "
    "AND packet_hash IS NOT NULL GROUP BY packet_hash, rx_radio_id"
)

RADIO_RX_QUERY = (
    "SELECT rx_radio_id, COUNT(*) AS n, SUM(is_duplicate) AS duplicates, "
    "AVG(rssi) AS rssi, AVG(snr) AS snr FROM packets "
    "WHERE timestamp >= ? AND timestamp <= ? AND rx_radio_id IS NOT NULL GROUP BY rx_radio_id"
)

UPSTREAM_QUERY = (
    "SELECT rx_radio_id, upstream_hash, COUNT(*) AS n FROM packets "
    "WHERE timestamp >= ? AND timestamp <= ? AND rx_radio_id IS NOT NULL "
    "AND upstream_hash IS NOT NULL GROUP BY rx_radio_id, upstream_hash"
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
    groups: Dict[str, List[str]],
    roles: Dict[str, Optional[str]],
    hours: int,
) -> dict:
    """Fold query rows into the ``/antenna_stats`` payload.

    ``groups`` maps network name -> radio ids on it, in display order.
    """
    network_of = {rid: net for net, rids in groups.items() for rid in rids}
    radios = {
        rid: {
            "radio_id": rid,
            "network": network_of[rid],
            "role": roles.get(rid),
            "received": 0,
            "duplicates": 0,
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
            r["rssi_avg"] = _round(row["rssi"])
            r["snr_avg"] = _round(row["snr"])

    # Which radios heard each packet, per network.
    heard: Dict[str, Dict[str, dict]] = {net: {} for net in groups}
    for row in reception:
        rid = row["rx_radio_id"]
        net = network_of.get(rid)
        if net is None:
            continue
        heard[net].setdefault(row["packet_hash"], {})[rid] = (row["rssi"], row["snr"])

    networks = []
    for net, rids in groups.items():
        combos: Dict[tuple, int] = {}
        gaps = {pair: [0.0, 0.0, 0, 0] for pair in combinations(rids, 2)}
        for copies in heard[net].values():
            key = tuple(rid for rid in rids if rid in copies)
            combos[key] = combos.get(key, 0) + 1
            if len(key) == 1:
                radios[key[0]]["unique_packets"] += 1
            for a, b in combinations(key, 2):
                (ra, sa), (rb, sb) = copies[a], copies[b]
                gap = gaps[(a, b)]
                if ra is not None and rb is not None:
                    gap[0] += ra - rb
                    gap[2] += 1
                if sa is not None and sb is not None:
                    gap[1] += sa - sb
                    gap[3] += 1
        networks.append(
            {
                "network": net,
                "radios": list(rids),
                "packets": len(heard[net]),
                "heard_by": [
                    {"radios": list(key), "packets": n}
                    for key, n in sorted(combos.items(), key=lambda kv: (-len(kv[0]), -kv[1]))
                ],
                "signal_gaps": [
                    {
                        "a": a,
                        "b": b,
                        "packets": max(g[2], g[3]),
                        "rssi_db": _round(g[0] / g[2]) if g[2] else None,
                        "snr_db": _round(g[1] / g[3]) if g[3] else None,
                    }
                    for (a, b), g in gaps.items()
                ],
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

    return {"hours": hours, "radios": list(radios.values()), "networks": networks}
