"""Secret-free saved/running state and an atomic live crossing pause."""

import asyncio
import copy
from dataclasses import asdict

from repeater.bridge_traffic import TrafficLimits
from repeater.web.bridge_settings import bridge_view


def _snapshot(section):
    """Normalize optional defaults before comparing private snapshots."""
    section = copy.deepcopy(section or {})
    for key, default in {
        "enabled": False,
        "paused": False,
        "channels": {},
        "mappings": [],
        "direct_routes": [],
        "disabled_channels": [],
        "protocols": {},
        "peer_routing": "legacy",
        "max_pending_crossings": 64,
        "queue_ttl_seconds": 30,
        "echo_ttl_seconds": 300,
        "echo_capacity": 1000,
        "local_policy": {},
        "crossing_policy": {},
    }.items():
        section.setdefault(key, default)
    section["protocols"] = {
        key: section["protocols"].get(key, False)
        for key in ("discovery", "repeater_admin", "trace")
    }
    for key in ("mappings", "direct_routes"):
        for rule in section[key]:
            rule.setdefault("enabled", True)
    section["traffic_limits"] = asdict(TrafficLimits.from_config(section.get("traffic_limits", {})))
    evidence = section.setdefault("reception_evidence", {})
    for key, value in {
        "enabled": False,
        "window_seconds": 120,
        "capacity": 256,
        "observers": [],
    }.items():
        evidence.setdefault(key, value)
    section["disabled_channels"] = sorted(section["disabled_channels"])
    return section


def view(config, engine, config_path=None):
    result = bridge_view(config, config_path)
    bridge = getattr(engine, "channel_bridge", None)
    running = bridge is not None
    result.update(
        {
            "running": running,
            "paused": bool(bridge.paused) if running else False,
            "controls_available": running,
            "pending_crossings": getattr(engine, "_bridge_pending", 0) if engine else 0,
            "active_settings": None,
            "crossing_route_id": (
                bridge.crossing_identity.hex()
                if running
                and bridge.peer_routing == "distinct"
                and bridge.crossing_identity is not None
                else None
            ),
        }
    )
    if running:
        snapshot = copy.deepcopy(bridge.config_snapshot)
        snapshot["paused"] = bridge.paused
        active = bridge_view({**config, "channel_bridge": snapshot}, config_path)["settings"]
        result["active_settings"] = active
        # Compare keys only privately. A key rotation must require restart even
        # when both public views contain the same redacted marker.
        saved = copy.deepcopy(config.get("channel_bridge") or {})
        saved.setdefault("echo_database", result["settings"]["echo_database"])
        snapshot.setdefault("echo_database", active["echo_database"])
        result["restart_required"] = _snapshot(saved) != _snapshot(snapshot)
    else:
        result["restart_required"] = bool(result["settings"].get("enabled"))
    return result


def saved_snapshot(api):
    """Take one coherent saved copy; callers must not run this on the radio loop."""
    from repeater.config_manager import _CONFIG_WRITE_LOCK

    with _CONFIG_WRITE_LOCK:
        return copy.deepcopy(api.config)


async def _read_saved_snapshot(api):
    # Share one blocked snapshot read instead of adding a worker for each poll.
    task = getattr(api, "_bridge_snapshot_task", None)
    if task is None or task.done():
        task = asyncio.create_task(asyncio.to_thread(saved_snapshot, api))
        api._bridge_snapshot_task = task
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(task)


async def inspect(api):
    stable = False
    for _ in range(2):
        before = (
            getattr(api, "_bridge_control_task", None),
            bool(getattr(api, "_bridge_control_pending", False)),
        )
        config = await _read_saved_snapshot(api)
        after = (
            getattr(api, "_bridge_control_task", None),
            bool(getattr(api, "_bridge_control_pending", False)),
        )
        stable = before == after
        if stable or after[1]:
            break
    engine = getattr(getattr(api, "daemon_instance", None), "repeater_handler", None)
    result = view(config, engine, api._config_path)
    result["control_pending"] = bool(getattr(api, "_bridge_control_pending", False))
    result["controls_available"] = (
        result["controls_available"] and not result["control_pending"] and stable
    )
    if result["control_pending"] or not stable:
        # A pause completing during the disk snapshot is not a pending restart.
        result["restart_required"] = None
    return result


def _persist_pause(api, paused):
    from repeater.config_manager import _CONFIG_WRITE_LOCK

    with _CONFIG_WRITE_LOCK:
        # Snapshot only after obtaining the lock, so concurrent settings edits
        # cannot be lost. This worker never accesses or mutates the running bridge.
        section = copy.deepcopy(api.config.get("channel_bridge") or {})
        section["paused"] = paused
        result = api.config_manager.update_and_save({"channel_bridge": section}, live_update=False)
    return bool(result.get("success"))


async def _apply_pause(api, bridge, paused):
    try:
        saved = await asyncio.to_thread(_persist_pause, api, paused)
        if not saved:
            return False
        # Publication happens only on the owning loop, after the atomic file
        # replacement completed. Do not silently apply to a replacement engine.
        engine = getattr(getattr(api, "daemon_instance", None), "repeater_handler", None)
        if getattr(engine, "channel_bridge", None) is not bridge:
            raise RuntimeError("Bridge changed after pause persistence")
        bridge.set_paused(paused)
        return True
    finally:
        api._bridge_control_pending = False


async def update(api, updates):
    if (
        not isinstance(updates, dict)
        or set(updates) != {"paused"}
        or type(updates["paused"]) is not bool
    ):
        raise ValueError("Only a boolean pause control is accepted")
    engine = getattr(getattr(api, "daemon_instance", None), "repeater_handler", None)
    bridge = getattr(engine, "channel_bridge", None)
    if bridge is None:
        raise ValueError("Bridge is not running")
    if getattr(api, "_bridge_control_pending", False):
        raise ValueError("A bridge pause change is already pending")
    # Admission is serialized on the event loop. A disconnected or timed-out
    # HTTP caller must not cancel accepted persistence/publication work.
    api._bridge_control_pending = True
    task = asyncio.create_task(_apply_pause(api, bridge, updates["paused"]))
    api._bridge_control_task = task
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(task)
