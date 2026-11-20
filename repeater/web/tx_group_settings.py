"""Restart-only, secret-free configuration for ordered physical TX groups."""

import copy

from repeater.radio_ids import canonical_radio_id


def tx_group_view(config, active_topology=None):
    topology = (config.get("fabric") or {}).get("topology") or {}
    primaries = topology.get("networks") or {}
    groups = topology.get("tx_groups") or {}
    scopes = topology.get("tx_group_scopes") or {}
    rows = []
    for network, primary in primaries.items():
        members = []
        for entry in config.get("radios") or []:
            rid = canonical_radio_id(entry)
            # Shadow radios are receive-only and cannot join a TX group.
            if entry.get("network") == network and entry.get("role") != "shadow":
                members.append({"id": rid, "label": entry.get("name") or rid})
        saved = list(groups.get(primary, [primary]))
        active = (
            list(getattr(active_topology, "tx_groups", {}).get(primary, (primary,)))
            if active_topology is not None
            else None
        )
        active_scope = (
            active_topology.tx_group_scope(primary)
            if active_topology is not None and hasattr(active_topology, "tx_group_scope")
            else ("all" if active_topology is not None else None)
        )
        rows.append(
            {
                "network": network,
                "primary_radio_id": primary,
                "radio_ids": saved,
                "active_radio_ids": active,
                # "all": every send uses both radios; "crossings": bridged sends only.
                "scope": scopes.get(primary, "all"),
                "active_scope": active_scope,
                "members": members,
            }
        )
    return {
        "groups": rows,
        "restart_required": any(
            row["radio_ids"] != row["active_radio_ids"] or row["scope"] != row["active_scope"]
            for row in rows
        ),
    }


def tx_group_candidate(config, update, runtime_radios):
    if not isinstance(update, dict) or not (
        {"primary_radio_id", "radio_ids"}
        <= set(update)
        <= {"primary_radio_id", "radio_ids", "scope"}
    ):
        raise ValueError("Specify primary_radio_id, radio_ids and optionally scope")
    primary, ordered = update["primary_radio_id"], update["radio_ids"]
    scope = update.get("scope", "all")
    if scope not in ("all", "crossings"):
        raise ValueError("scope must be 'all' or 'crossings'")
    candidate = copy.deepcopy(config)
    topology = candidate.get("fabric", {}).get("topology", {})
    primaries = topology.get("networks", {})
    if not isinstance(primary, str) or primary not in primaries.values():
        raise ValueError("Unknown logical transmitter")
    if (
        not isinstance(ordered, list)
        or not 1 <= len(ordered) <= 2
        or any(not isinstance(r, str) for r in ordered)
        or len(set(ordered)) != len(ordered)
        or primary not in ordered
    ):
        raise ValueError("Select the primary alone or two distinct radios including the primary")
    entries = {canonical_radio_id(e): e for e in candidate.get("radios", [])}
    network = entries[primary]["network"]
    if any(r not in entries or entries[r].get("network") != network for r in ordered):
        raise ValueError("Both transmitters must belong to the same logical network")
    if len(ordered) > 1 and any(rid not in runtime_radios for rid in ordered):
        raise ValueError("Both radios must be available before enabling dual TX")
    topology.setdefault("tx_groups", {})[primary] = list(ordered)
    scopes = topology.setdefault("tx_group_scopes", {})
    if scope == "crossings" and len(ordered) > 1:
        scopes[primary] = "crossings"
    else:
        scopes.pop(primary, None)  # "all" is the default; a single radio has no group
    if not scopes:
        topology.pop("tx_group_scopes")
    if len(ordered) < 2:
        # Pause and order settings need two legs; keep them for a two-radio group.
        for key in ("tx_group_gaps_ms", "tx_group_orders"):
            per_group = topology.get(key) or {}
            per_group.pop(primary, None)
            if not per_group:
                topology.pop(key, None)
    from repeater.network_topology import topology_from_config

    topology_from_config(candidate)
    return {"fabric": candidate["fabric"]}
