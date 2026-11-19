"""Restart-only setting for antenna identity aliases (tx_identity_experiment).

Three choices, mapped onto the existing config section:

* ``off``   -> ``enabled: false``. The section, and any per-transmitter keys in
  it, are kept, so routes other nodes learned through an alias still resolve.
* ``tx``    -> ``enabled: true, mode: tx``. Needs per-transmitter keys in
  ``radios``.
* ``rx_tx`` -> ``enabled: true, mode: rx_tx``. Pair aliases are derived from
  the node key, so no keys are needed.
"""

import copy

from repeater.radio_ids import configured_radio_ids

MODES = ("off", "tx", "rx_tx")
SECTION = "tx_identity_experiment"


def _section(config):
    section = config.get(SECTION)
    return section if isinstance(section, dict) else {}


def saved_mode(config):
    section = _section(config)
    if section.get("enabled") is not True:
        return "off"
    mode = section.get("mode", "rx_tx")
    return mode if mode in ("tx", "rx_tx") else "rx_tx"


def running_mode(handler):
    """The mode the running engine uses, or None when the engine is unavailable."""
    identities = getattr(handler, "tx_identities", None)
    if identities is None:
        return None
    return identities.mode if identities.enabled else "off"


def antenna_identity_view(config, handler=None):
    saved = saved_mode(config)
    running = running_mode(handler) if handler is not None else None
    return {
        "saved": saved,
        "running": running,
        "restart_required": running is not None and running != saved,
        # Transmitting-antenna codes need per-transmitter keys in the config.
        "tx_available": bool(_section(config).get("radios")),
    }


def antenna_identity_candidate(config, update):
    """Config updates for one mode change; raises ValueError when invalid."""
    if not isinstance(update, dict) or set(update) != {"mode"}:
        raise ValueError("Specify mode only")
    mode = update["mode"]
    if mode not in MODES:
        raise ValueError("mode must be off, tx or rx_tx")
    section = copy.deepcopy(_section(config))
    if mode == "off":
        section["enabled"] = False
    else:
        if mode == "tx" and not section.get("radios"):
            raise ValueError(
                "Transmitting-antenna codes need per-transmitter keys; "
                "use receiving + transmitting codes instead"
            )
        section["enabled"] = True
        section["mode"] = mode
        # Fail before saving if the engine would reject the section at startup.
        from repeater.tx_identities import TxIdentities

        candidate = {**config, SECTION: section}
        if not TxIdentities(candidate, bytes(32), radio_ids=configured_radio_ids(config)).enabled:
            raise ValueError("Antenna identity settings are invalid; see the repeater log")
    return {SECTION: section}
