"""Bridge traffic statistics from the stored packet history.

Every stored packet already records the radio that heard it and the radios
that sent it on (and, per physical send, ``packet_egress``). Mapping those
radios onto the two topology networks answers the bridge questions directly,
with no extra tracking:

* packets heard on one network and sent on the other ("bridged"), per direction;
* packets repeated on the network they were heard on ("local");
* why heard packets were not sent at all;
* physical sends per (heard-on radio, sent-on radio) pair, which is exactly what
  the per-antenna TX identity aliases name on the air.

Counts are packets for the network figures (one row per packet, even when an
ordered TX group sent it on two antennas) and transmissions for the radio pairs.
Radios that are no longer configured (renamed or retired) are counted as
unattributed rather than guessed onto a current radio.
"""

from __future__ import annotations

import json
from typing import Dict, Iterable, Optional

# Grouped in SQL so a week of traffic comes back as a few rows per bucket.
# drop_reason only varies for rows that were not sent.
BRIDGE_PACKETS_QUERY = (
    "SELECT CAST(timestamp / ? AS INTEGER) * ? AS bucket_ts, rx_radio_id, transmitted, "
    "is_duplicate, tx_radio_id, tx_radio_ids, "
    "CASE WHEN transmitted THEN NULL ELSE drop_reason END AS reason, COUNT(*) AS n "
    "FROM packets WHERE timestamp >= ? AND timestamp <= ? "
    "GROUP BY bucket_ts, rx_radio_id, transmitted, is_duplicate, tx_radio_id, tx_radio_ids, reason"
)

# One row per physical send; idx_packet_egress_time_radio covers the range scan.
BRIDGE_EGRESS_QUERY = (
    "SELECT p.rx_radio_id AS rx_radio_id, e.radio_id AS tx_radio_id, e.success AS success, "
    "COUNT(*) AS n FROM packet_egress e JOIN packets p ON p.id = e.packet_id "
    "WHERE e.timestamp >= ? AND e.timestamp <= ? "
    "GROUP BY p.rx_radio_id, e.radio_id, e.success"
)

MAX_REASONS = 8


def _tx_ids(tx_radio_id, tx_radio_ids) -> list:
    if isinstance(tx_radio_ids, str):
        try:
            decoded = json.loads(tx_radio_ids)
        except ValueError:
            decoded = None
        if isinstance(decoded, list) and decoded:
            return [str(x) for x in decoded]
    return [str(tx_radio_id)] if tx_radio_id is not None else []


def fold_bridge_stats(
    packet_rows: Iterable,
    egress_rows: Iterable,
    *,
    networks: Dict[str, str],
    members: Dict[str, str],
    bucket_seconds: int,
    aliases: Optional[Dict[tuple, dict]] = None,
) -> dict:
    """Fold grouped query rows into the ``/bridge_stats`` payload.

    ``networks`` maps network name -> its TX (home) radio, in display order;
    ``members`` maps every configured radio id -> network name; ``aliases``
    maps (rx radio, tx radio) -> {"prefix", "crossing_prefix"}.
    """
    names = list(networks)
    per_net = {
        name: {
            "network": name,
            "tx_radio": networks[name],
            "radios": [rid for rid, net in members.items() if net == name],
            "heard": 0,
            "repeated_local": 0,
            "bridged_out": 0,
            "not_sent": 0,
            "originated": 0,
            "reasons": {},
        }
        for name in names
    }
    directions = {
        (a, b): {"from": a, "to": b, "packets": 0, "buckets": {}}
        for a in names
        for b in names
        if a != b
    }
    local = {name: {"network": name, "packets": 0, "buckets": {}} for name in names}
    unattributed = {"packets": 0, "transmissions": 0}

    def bump(series, bucket_ts, n):
        series["packets"] += n
        series["buckets"][bucket_ts] = series["buckets"].get(bucket_ts, 0) + n

    for row in packet_rows:
        n = int(row["n"])
        bucket_ts = int(row["bucket_ts"])
        rx = row["rx_radio_id"]
        rx_net = members.get(str(rx)) if rx is not None else None
        if not row["transmitted"]:
            if rx_net is None:
                unattributed["packets"] += n
                continue
            if row["is_duplicate"]:
                continue
            stats = per_net[rx_net]
            stats["heard"] += n
            stats["not_sent"] += n
            reason = row["reason"] or "Not recorded"
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + n
            continue

        tx_nets = {members.get(t) for t in _tx_ids(row["tx_radio_id"], row["tx_radio_ids"])}
        if None in tx_nets or (rx is not None and rx_net is None):
            unattributed["packets"] += n
            continue
        if rx is None:
            for net in tx_nets:
                per_net[net]["originated"] += n
            continue
        per_net[rx_net]["heard"] += n
        for net in tx_nets:
            if net == rx_net:
                per_net[rx_net]["repeated_local"] += n
                bump(local[net], bucket_ts, n)
            else:
                per_net[rx_net]["bridged_out"] += n
                bump(directions[(rx_net, net)], bucket_ts, n)

    pairs = {}
    for row in egress_rows:
        n = int(row["n"])
        rx, tx = row["rx_radio_id"], row["tx_radio_id"]
        rx_net = members.get(str(rx)) if rx is not None else None
        tx_net = members.get(str(tx)) if tx is not None else None
        if tx_net is None or (rx is not None and rx_net is None):
            unattributed["transmissions"] += n
            continue
        key = (rx, tx)
        pair = pairs.get(key)
        if pair is None:
            alias = (aliases or {}).get(key) or {}
            pair = pairs[key] = {
                "rx_radio": rx,
                "tx_radio": tx,
                "rx_network": rx_net,
                "tx_network": tx_net,
                "bridged": rx_net is not None and rx_net != tx_net,
                "sent": 0,
                "failed": 0,
                "antenna_code": alias.get("prefix"),
            }
        pair["sent" if row["success"] else "failed"] += n

    def series(entry):
        return dict(
            entry,
            buckets=[{"timestamp": ts, "count": c} for ts, c in sorted(entry["buckets"].items())],
        )

    order = {rid: i for i, rid in enumerate(members)}
    return {
        "configured": True,
        "bucket_seconds": bucket_seconds,
        "networks": [
            dict(
                stats,
                reasons=[
                    {"reason": reason, "count": count}
                    for reason, count in sorted(stats["reasons"].items(), key=lambda kv: -kv[1])[
                        :MAX_REASONS
                    ]
                ],
            )
            for stats in per_net.values()
        ],
        "directions": [series(d) for d in directions.values()],
        "local": [series(entry) for entry in local.values()],
        "radio_pairs": sorted(
            pairs.values(),
            key=lambda p: (
                p["rx_radio"] is None,
                order.get(p["rx_radio"], 99),
                order.get(p["tx_radio"], 99),
            ),
        ),
        "unattributed": unattributed,
    }


def empty_bridge_stats(bucket_seconds: int, configured: bool) -> dict:
    return {
        "configured": configured,
        "bucket_seconds": bucket_seconds,
        "networks": [],
        "directions": [],
        "local": [],
        "radio_pairs": [],
        "unattributed": {"packets": 0, "transmissions": 0},
    }
