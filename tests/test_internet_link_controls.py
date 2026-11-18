"""Internet Link service control must not stay pending when the runtime hangs."""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from repeater.web import internet_link


@pytest.fixture
def private_link_stub(monkeypatch):
    daemon_module = types.ModuleType("private_link.daemon")
    daemon_module._configuration = lambda ns: (None, "parsed", 4)

    async def install_from_config(daemon):
        await asyncio.Event().wait()  # Never finishes (e.g. a stuck TLS bind).

    daemon_module.install_from_config = install_from_config
    monkeypatch.setitem(sys.modules, "private_link", types.ModuleType("private_link"))
    monkeypatch.setitem(sys.modules, "private_link.daemon", daemon_module)
    monkeypatch.setattr(internet_link, "CONTROL_TIMEOUT_SECONDS", 0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_hung_runtime_change_reports_error_and_clears_pending(private_link_stub, enabled):
    async def hang():
        await asyncio.Event().wait()

    binding = SimpleNamespace(closed=False, paused=True, pause=hang, resume=lambda *a: hang())
    api = SimpleNamespace(
        config={"private_island": {"enabled": not enabled, "transport": {"enabled": True}}},
        config_manager=Mock(update_and_save=Mock(return_value={"success": True})),
        daemon_instance=SimpleNamespace(private_island=None if enabled else binding),
    )
    result = await asyncio.wait_for(internet_link.update(api, {"enabled": enabled}), timeout=2)
    assert result["saved"] and result["service_error"]
    assert api._internet_link_control_pending is False


@pytest.mark.asyncio
async def test_limit_save_runs_off_loop_and_holds_enrollment_lock(private_link_stub, monkeypatch):
    import threading

    lock = asyncio.Lock()
    loop_thread = threading.get_ident()
    seen = {}

    def save(updates, live_update):
        seen["thread"] = threading.get_ident()
        seen["locked"] = lock.locked()
        return {"success": True}

    monkeypatch.setattr(
        internet_link, "candidate", lambda config, updates: ({"enabled": True}, "parsed", 8)
    )
    adapter = SimpleNamespace(config=None)
    binding = SimpleNamespace(
        closed=False,
        paused=False,
        adapter=adapter,
        limit=1,
        service=SimpleNamespace(enrollment=SimpleNamespace(lock=lock)),
    )
    api = SimpleNamespace(
        config={"private_island": {"enabled": True}},
        config_manager=Mock(update_and_save=Mock(side_effect=save)),
        daemon_instance=SimpleNamespace(private_island=binding),
    )
    assert await internet_link.update(api, {"max_inflight": 8})
    assert seen["thread"] != loop_thread and seen["locked"]
    assert adapter.config == "parsed" and binding.limit == 8
    assert api._internet_link_control_pending is False
