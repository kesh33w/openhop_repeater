"""Which copy of a double-sent packet neighbours relayed, from stored history.

A TX group sends one packet through two radios. With per-radio identity codes
on (``tx_identity_experiment`` in ``tx`` mode), each copy carries its sending
radio's code as its last hop. A neighbour relays whichever copy it heard first
and drops the other, and its relay keeps that code in front of its own hop:
``... 2855 94`` is neighbour ``94`` relaying the copy sent as ``2855``.

This node stores the relays it hears back (as duplicates or bridge echoes) with
their path, and every physical send in ``packet_egress`` with the bytes sent and
in send order. Joining the two by packet hash tells, per neighbour, which radio's
copy it relayed and whether that copy went first or second.

Only packets whose two copies carry different codes can be told apart. Copies
are identical when identity codes are off, on 1-byte paths (too short for an
alias), and on direct routes (no trail to tag); those are counted as
``untagged``. A relay is counted once per (packet, neighbour), however many of
this node's radios heard it.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional

from repeater.bridge_stats import on_air_code

# Every physical send in the window with its bytes, in send order per packet.
RELAY_LEGS_QUERY = (
    "SELECT packet_id, id AS leg_id, radio_id, success, raw_packet AS raw "
    "FROM packet_egress WHERE timestamp >= ? AND timestamp <= ? "
    "AND radio_id IS NOT NULL ORDER BY packet_id, id"
)

# Receptions of those packets (by packet hash) heard after the window opened.
# Named parameters: start, end and hashes (a JSON array). idx_packets_hash serves it.
RELAY_RECEPTIONS_QUERY = (
    "SELECT packet_hash, original_path FROM packets "
    "WHERE timestamp >= :start AND timestamp <= :end AND original_path IS NOT NULL "
    "AND packet_hash IN (SELECT value FROM json_each(:hashes))"
)

# How long after the window closes a relay of a send inside it is still counted.
RELAY_GRACE_SECONDS = 60
MAX_NEIGHBOURS = 25


def packet_hash_of(raw) -> Optional[str]:
    """The stored ``packets.packet_hash`` form (16 upper-case hex) of sent bytes."""
    from openhop_core.protocol import Packet

    try:
        data = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
        packet = Packet()
        packet.read_from(data)
        return packet.calculate_packet_hash().hex().upper()[:16]
    except Exception:  # noqa: BLE001 - one unreadable row must not hide the rest
        return None


def _path(value) -> List[str]:
    try:
        hops = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return []
    return [str(h).lower() for h in hops or ()]


def double_sends(leg_rows: Iterable, network_of: Dict[str, str]) -> List[dict]:
    """Packets sent by two or more radios of one network, with each leg's code and position.

    Returns one entry per (packet, network): ``{"hash", "network", "legs":
    [{"radio_id", "code", "position"}]}`` with positions 1, 2 in send order.
    Only successful legs count.
    """
    by_packet: Dict[int, List] = defaultdict(list)
    for row in leg_rows:
        if row["success"] and row["radio_id"] in network_of:
            by_packet[row["packet_id"]].append(row)
    sends = []
    for legs in by_packet.values():
        per_net: Dict[str, List] = defaultdict(list)
        for row in legs:  # already in send order
            per_net[network_of[row["radio_id"]]].append(row)
        for network, net_legs in per_net.items():
            if len(net_legs) < 2:
                continue
            packet_hash = packet_hash_of(net_legs[0]["raw"]) if net_legs[0]["raw"] else None
            if packet_hash is None:
                continue
            sends.append(
                {
                    "hash": packet_hash,
                    "network": network,
                    "legs": [
                        {
                            "radio_id": row["radio_id"],
                            "code": on_air_code(row["raw"]),
                            "position": position,
                        }
                        for position, row in enumerate(net_legs, 1)
                    ],
                }
            )
    return sends


def fold_relay_stats(
    sends: List[dict],
    reception_rows: Iterable,
    *,
    groups: Dict[str, List[str]],
    hours: int,
) -> dict:
    """Fold double sends and the receptions of the same packets into ``/relay_stats``."""
    paths_by_hash: Dict[str, List[List[str]]] = defaultdict(list)
    for row in reception_rows:
        paths_by_hash[row["packet_hash"]].append(_path(row["original_path"]))

    networks = []
    for network, rids in groups.items():
        net_sends = [s for s in sends if s["network"] == network]
        radios = {
            rid: {
                "radio_id": rid,
                "codes": Counter(),
                "sent_first": 0,
                "relayed": 0,
                "relayed_when_first": 0,
                "relayed_when_second": 0,
            }
            for rid in rids
        }
        neighbours: Dict[str, Counter] = defaultdict(Counter)
        tagged = relayed = 0
        for send in net_sends:
            legs = send["legs"]
            first = radios.get(legs[0]["radio_id"])
            if first:
                first["sent_first"] += 1
            codes = {leg["code"]: leg for leg in legs if leg["code"]}
            if len(codes) < len(legs):
                continue  # copies identical: cannot tell which was relayed
            tagged += 1
            for leg in legs:
                if leg["radio_id"] in radios:
                    radios[leg["radio_id"]]["codes"][leg["code"].upper()] += 1
            # (neighbour -> leg) once per packet, whichever of our radios heard it.
            heard: Dict[str, dict] = {}
            for path in paths_by_hash.get(send["hash"], ()):
                for i, hop in enumerate(path[:-1]):
                    leg = codes.get(hop)
                    if leg is not None:
                        heard.setdefault(path[i + 1].upper(), leg)
                        break
            if heard:
                relayed += 1
            for leg in {id(leg): leg for leg in heard.values()}.values():
                r = radios.get(leg["radio_id"])
                if r:
                    r["relayed"] += 1
                    key = "relayed_when_first" if leg["position"] == 1 else "relayed_when_second"
                    r[key] += 1
            for neighbour, leg in heard.items():
                neighbours[neighbour][leg["radio_id"]] += 1
        for r in radios.values():
            r["code"] = r.pop("codes").most_common(1)[0][0] if r["codes"] else None
        networks.append(
            {
                "network": network,
                "radios": [r for r in radios.values() if r["code"] or r["sent_first"]],
                "double_sent": len(net_sends),
                "tagged": tagged,
                "untagged": len(net_sends) - tagged,
                "relayed": relayed,
                "neighbours": [
                    {"neighbour": n, "relays": sum(c.values()), "by_radio": dict(c)}
                    for n, c in sorted(neighbours.items(), key=lambda kv: -sum(kv[1].values()))[
                        :MAX_NEIGHBOURS
                    ]
                ],
            }
        )
    return {"hours": hours, "networks": networks}
