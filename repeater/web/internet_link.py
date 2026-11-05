"""Internet Link status and bounded operational controls. Never return credentials."""

import copy
from types import SimpleNamespace

DEFAULTS = {"send_timeout": 1.0, "max_inflight": 4, "max_rx_tasks": 16}


def candidate(config, updates):
    if not isinstance(updates, dict) or not updates or set(updates) - DEFAULTS.keys():
        raise ValueError("Unknown Internet Link option")
    for key, value in updates.items():
        if key == "send_timeout":
            valid = type(value) in (int, float) and 0 < value <= 5
        else:
            valid = type(value) is int and 1 <= value <= 64
        if not valid:
            raise ValueError("Invalid Internet Link limit")
    section = copy.deepcopy(config.get("private_island") or {})
    if section.get("enabled") is not True:
        raise ValueError("Internet Link is not active")
    section.update(updates)
    from private_link.daemon import _configuration

    parsed = _configuration(SimpleNamespace(config={**config, "private_island": section}))
    return section, parsed[1], parsed[2]


def view(config, binding):
    section = config.get("private_island") or {}
    service = getattr(binding, "service", None)
    adapter = getattr(binding, "adapter", None)
    running = binding is not None and not binding.closed
    server = getattr(service, "_server", None)
    listening = bool(server is not None and server.is_serving())
    settings = {key: section.get(key, default) for key, default in DEFAULTS.items()}
    active = None
    if running:
        active = {"send_timeout": adapter.config.send_timeout,
                  "max_inflight": adapter.config.max_inflight, "max_rx_tasks": binding.limit}
    peers = []
    # Snapshot maps only on the daemon's event loop, where enrollment changes them.
    if service is not None:
        for peer, transport in sorted(service.transports.items()):
            peers.append({"id": peer, "connected": bool(transport.connected)})
    routes = []
    if adapter is not None:
        for (peer, radio), local in sorted(adapter.config.inbound.items()):
            routes.append({"direction": "inbound", "peer": peer, "radio": radio, "local_radio": local})
        for local, targets in sorted(adapter.config.outbound.items()):
            for peer in targets:
                routes.append({"direction": "outbound", "peer": peer, "local_radio": local})
    return {
        "enabled": section.get("enabled") is True,
        "running": running, "listening": listening,
        "listener_port": getattr(service, "port", None),
        "node_id": section.get("node_id"),
        "enrollment_available": listening and getattr(service, "enrollment", None) is not None,
        "settings": settings, "active_settings": active,
        "editable": running and listening,
        "packet_ttl_ms": adapter.config.ttl_ms if adapter is not None else section.get("ttl_ms", 15000),
        "peers": peers, "routes": routes,
        "pending_rf": getattr(binding, "peer_pending", 0),
        "dropped_tasks": getattr(binding, "dropped", 0),
        "transport_failures": getattr(service, "failures", 0),
    }


async def inspect(api):
    return view(api.config, getattr(api.daemon_instance, "private_island", None))


async def update(api, updates):
    binding = getattr(api.daemon_instance, "private_island", None)
    if binding is None or binding.closed or binding.service is None:
        raise ValueError("Internet Link is not running")
    # This coroutine does not yield. Enrollment also publishes configuration without
    # yielding, so a save cannot overwrite a newly admitted peer or its routes.
    from repeater.config_manager import _CONFIG_WRITE_LOCK

    with _CONFIG_WRITE_LOCK:
        section, parsed, limit = candidate(api.config, updates)
        result = api.config_manager.update_and_save({"private_island": section}, live_update=False)
        if not result.get("success"):
            return False
        binding.adapter.config = parsed
        binding.limit = limit
    return True
