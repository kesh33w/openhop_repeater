"""Explicit origin routing validation shared by startup and configuration APIs."""

from repeater.radio_ids import configured_radio_ids


class BridgeHomeAssignmentError(ValueError):
    """An explicit home is missing or invalid; safe to classify in web errors."""


def validate_companion_home(config, settings, *, context="Companion"):
    enabled = bool((config.get("channel_bridge") or {}).get("enabled", False))
    if not isinstance(settings, dict):
        raise BridgeHomeAssignmentError(f"{context} settings must be an object")
    home = settings.get("home_radio_id")
    if home is None and not enabled:
        return
    if not isinstance(home, str) or home not in configured_radio_ids(config):
        suffix = " when channel_bridge is enabled" if enabled else ""
        raise BridgeHomeAssignmentError(
            f"{context} settings.home_radio_id must name a configured radio{suffix}"
        )


def validate_bridge_homes(config):
    section = config.get("channel_bridge") or {}
    if not section.get("enabled", False):
        return
    home = section.get("origin_home")
    if not isinstance(home, str) or home not in configured_radio_ids(config):
        raise BridgeHomeAssignmentError(
            "channel_bridge.origin_home must explicitly name a configured radio "
            "for daemon-originated transmissions when channel_bridge is enabled"
        )
    for index, companion in enumerate((config.get("identities") or {}).get("companions") or []):
        if not isinstance(companion, dict):
            raise BridgeHomeAssignmentError(f"identities.companions[{index}] must be an object")
        validate_companion_home(
            config, companion.get("settings") or {},
            context=f"Companion '{companion.get('name') or index}'",
        )
