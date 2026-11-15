"""Validate the optional topology before any hardware is opened."""

from openhop_core.rf_fabric.topology import NetworkTopology
from repeater.radio_ids import configured_radio_ids


def topology_from_config(config):
    fabric = config.get("fabric") or {}
    if "topology" not in fabric:
        if any("network" in e or "role" in e for e in config.get("radios") or []):
            raise ValueError("Radio network/role requires fabric.topology")
        return None
    ids = configured_radio_ids(config)
    default = (
        fabric.get("default_radio")
        or fabric.get("default_radio_id")
        or (ids[0] if ids else None)
    )
    topology = NetworkTopology(config.get("radios") or [], fabric["topology"], default)
    topology.tx_mode = (
        str(fabric.get("tx_mode", "default")).strip().lower() or "default"
    )
    topology.repeat_on_ingress = fabric.get("repeat_on_ingress", False)
    # Receivers sharing a logical network must share all modulation settings.
    settings = {}
    for entry in config["radios"]:
        air = {**(config.get("radio") or {}), **(entry.get("radio") or {})}
        fields = (
            "frequency",
            "bandwidth",
            "spreading_factor",
            "coding_rate",
            "sync_word",
            "preamble_length",
        )
        signature = tuple(air.get(k) for k in fields)
        if any(air.get(k) is None for k in fields[:4]):
            raise ValueError(
                "Topology requires explicit frequency, bandwidth, spreading factor and coding rate"
            )
        network = entry["network"]
        if network in settings and settings[network] != signature:
            raise ValueError(
                "Radios on one logical network must have matching air settings"
            )
        settings[network] = signature
    if settings[next(iter(settings))][0] == settings[next(reversed(settings))][0]:
        raise ValueError("The two logical networks must have distinct frequencies")
    return topology


def bridge_radio_ids(config):
    topology = topology_from_config(config)
    return (
        tuple(topology.networks.values()) if topology else configured_radio_ids(config)
    )
