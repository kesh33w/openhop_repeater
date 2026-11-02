"""Secret-free bridge views and validated, restart-only configuration edits."""

import copy
from repeater.channel_bridge import BridgeSettings
from repeater.config import resolve_storage_dir
from pathlib import Path

REDACTED = "*** REDACTED ***"
FIELDS = {
    "enabled",
    "channels",
    "mappings",
    "echo_database",
    "echo_ttl_seconds",
    "echo_capacity",
    "max_pending_crossings",
    "queue_ttl_seconds",
    "origin_home",
    "local_policy",
    "crossing_policy",
}


def redact_bridge(config):
    section = config.get("channel_bridge")
    if isinstance(section, dict) and isinstance(section.get("channels"), dict):
        section["channels"] = {name: REDACTED for name in section["channels"]}
    return config


def bridge_view(config, config_path=None):
    section = copy.deepcopy(
        config.get("channel_bridge") or {"enabled": False, "channels": {}, "mappings": []}
    )
    section.setdefault(
        "echo_database",
        str(Path(resolve_storage_dir(config, config_path=config_path)) / "bridge-echo.sqlite"),
    )
    section.setdefault("max_pending_crossings", 64)
    section.setdefault("queue_ttl_seconds", 30)
    redact_bridge({"channel_bridge": section})
    return {
        "settings": section,
        "radio_ids": [
            r["id"]
            for r in config.get("radios", [])
            if isinstance(r, dict) and isinstance(r.get("id"), str)
        ],
        "restart_required": True,
    }


def bridge_candidate(config, updates, config_path=None):
    if not isinstance(updates, dict) or set(updates) - FIELDS:
        raise ValueError("Unknown bridge setting")
    section = {**copy.deepcopy(config.get("channel_bridge") or {}), **copy.deepcopy(updates)}
    if type(section.get("enabled", False)) is not bool:
        raise ValueError("Bridge enabled must be a boolean")
    expected_path = bridge_view(config, config_path)["settings"]["echo_database"]
    if section.get("echo_database", expected_path) != expected_path:
        raise ValueError("Echo database path is managed by the host configuration")
    section["echo_database"] = expected_path
    channels = section.get("channels", {})
    if not isinstance(channels, dict):
        raise ValueError("Channels must be named keys")
    previous = config.get("channel_bridge", {}).get("channels", {})
    for name, value in list(channels.items()):
        if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 64:
            raise ValueError("Channel names must contain 1 to 64 characters")
        if value == REDACTED:
            if name not in previous:
                raise ValueError("A new channel requires a key")
            channels[name] = previous[name]
        elif not isinstance(value, str) or len(value) not in (32, 64):
            raise ValueError("Channel keys must contain 32 or 64 hexadecimal characters")
    # Validate configured drafts even when disabled; a bare disable remains possible.
    if section.get("enabled") or channels or section.get("mappings"):
        candidate = {**config, "channel_bridge": {**section, "enabled": True}}
        BridgeSettings.from_config(candidate)
    return section
