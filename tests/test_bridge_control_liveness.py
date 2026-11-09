"""Real persistence and lifecycle tests; no radios or network are used."""

# ruff: noqa: F811
import asyncio
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import cherrypy
import pytest
import yaml

from repeater.channel_bridge import BridgeSettings
from repeater.config_manager import _CONFIG_WRITE_LOCK, ConfigManager
from repeater.web.api_endpoints import APIEndpoints
from repeater.web.bridge_controls import inspect, update, view
from repeater.web.bridge_settings import bridge_candidate, bridge_view
from tests.test_bridge_settings_api import config  # noqa: F401


def api_fixture(config, tmp_path):
    manager = ConfigManager(str(tmp_path / "config.yaml"), config)
    assert manager.save_to_file()
    bridge = BridgeSettings(config)
    return SimpleNamespace(
        config=config,
        config_manager=manager,
        _config_path=manager.config_path,
        daemon_instance=SimpleNamespace(
            repeater_handler=SimpleNamespace(channel_bridge=bridge, _bridge_pending=0)
        ),
    )


@pytest.mark.asyncio
async def test_blocked_real_disk_write_does_not_block_loop_and_publishes_after_commit(
    config, tmp_path, monkeypatch
):
    api = api_fixture(config, tmp_path)
    bridge = api.daemon_instance.repeater_handler.channel_bridge
    entered, release = threading.Event(), threading.Event()
    original = api.config_manager._persist_config
    loop_thread = threading.get_ident()
    seen_threads = []
    publication_threads = []
    original_set_paused = bridge.set_paused

    def publish(value):
        publication_threads.append(threading.get_ident())
        assert (
            yaml.safe_load((tmp_path / "config.yaml").read_text())["channel_bridge"]["paused"]
            is value
        )
        original_set_paused(value)

    monkeypatch.setattr(bridge, "set_paused", publish)

    def blocked(candidate):
        seen_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return original(candidate)

    monkeypatch.setattr(api.config_manager, "_persist_config", blocked)
    operation = asyncio.create_task(update(api, {"paused": True}))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        tick = time.monotonic()
        await asyncio.wait_for(asyncio.sleep(0.01), 0.5)
        assert time.monotonic() - tick < 0.5
        assert seen_threads == [seen_threads[0]] and seen_threads[0] != loop_thread
        assert bridge.paused is False
        assert (
            yaml.safe_load((tmp_path / "config.yaml").read_text())["channel_bridge"].get(
                "paused", False
            )
            is False
        )
        with pytest.raises(ValueError, match="already pending"):
            await update(api, {"paused": False})
        release.set()
        assert await asyncio.wait_for(operation, 2)
        assert bridge.paused is True and bridge.pause_generation == 1
        assert publication_threads == [loop_thread]
        assert (
            yaml.safe_load((tmp_path / "config.yaml").read_text())["channel_bridge"]["paused"]
            is True
        )
        assert api._bridge_control_pending is False
    finally:
        release.set()
        await asyncio.gather(operation, return_exceptions=True)


@pytest.mark.asyncio
async def test_waiting_for_config_lock_keeps_loop_responsive_and_status_reads_coherent(
    config, tmp_path
):
    api = api_fixture(config, tmp_path)
    held, release = threading.Event(), threading.Event()

    def hold_lock():
        with _CONFIG_WRITE_LOCK:
            held.set()
            assert release.wait(3)

    holder = asyncio.create_task(asyncio.to_thread(hold_lock))
    assert await asyncio.wait_for(asyncio.to_thread(held.wait, 1), 1.5)
    operation = asyncio.create_task(update(api, {"paused": True}))
    reads = [asyncio.create_task(inspect(api)) for _ in range(3)]
    try:
        await asyncio.wait_for(asyncio.sleep(0.02), 0.5)
        assert not operation.done() and all(not task.done() for task in reads)
        assert api._bridge_snapshot_task is not None
        release.set()
        assert await asyncio.wait_for(operation, 2)
        await asyncio.gather(*reads)
        latest = await inspect(api)
        assert latest["paused"] is True and not latest["restart_required"]
    finally:
        release.set()
        await asyncio.gather(holder, operation, *reads, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancelled_caller_does_not_cancel_accepted_persistence(config, tmp_path, monkeypatch):
    api = api_fixture(config, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = api.config_manager._persist_config

    def blocked(candidate):
        entered.set()
        assert release.wait(3)
        return original(candidate)

    monkeypatch.setattr(api.config_manager, "_persist_config", blocked)
    caller = asyncio.create_task(update(api, {"paused": True}))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert api._bridge_control_pending
        release.set()
        assert await asyncio.wait_for(api._bridge_control_task, 2)
        assert api.daemon_instance.repeater_handler.channel_bridge.paused
        assert yaml.safe_load((tmp_path / "config.yaml").read_text())["channel_bridge"]["paused"]
    finally:
        release.set()
        await asyncio.gather(api._bridge_control_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_settings_writer_cannot_undo_pause_or_host_allowlist(
    config, tmp_path, monkeypatch
):
    api = api_fixture(config, tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = api.config_manager._persist_config
    first = True

    def blocked(candidate):
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(3)
        return original(candidate)

    monkeypatch.setattr(api.config_manager, "_persist_config", blocked)
    operation = asyncio.create_task(update(api, {"paused": True}))
    writer = None

    def stale_save():
        # Same locking/preservation sequence used by update_bridge_settings.
        with _CONFIG_WRITE_LOCK:
            candidate = bridge_candidate(api.config, {"paused": False, "queue_ttl_seconds": 25})
            candidate["paused"] = api.config["channel_bridge"].get("paused", False)
            return api.config_manager.update_and_save(
                {"channel_bridge": candidate}, live_update=False
            )

    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 1.5)
        writer = asyncio.create_task(asyncio.to_thread(stale_save))
        await asyncio.sleep(0)
        release.set()
        assert await operation
        assert (await writer)["success"]
        saved = yaml.safe_load((tmp_path / "config.yaml").read_text())["channel_bridge"]
        assert saved["paused"] is True and saved["queue_ttl_seconds"] == 25
        assert api.daemon_instance.repeater_handler.channel_bridge.paused
    finally:
        release.set()
        await asyncio.gather(operation, *([writer] if writer else []), return_exceptions=True)


def test_host_observer_section_cannot_be_written_or_stale_round_tripped(config):
    config["channel_bridge"]["reception_evidence"] = {"enabled": False, "capacity": 32}
    before = copy.deepcopy(config)
    public = bridge_view(config)["settings"]["reception_evidence"]
    assert public == before["channel_bridge"]["reception_evidence"]
    public["capacity"] = 16
    assert config == before
    active = view(config, SimpleNamespace(channel_bridge=BridgeSettings(config)))
    assert (
        active["active_settings"]["reception_evidence"]
        == before["channel_bridge"]["reception_evidence"]
    )
    with pytest.raises(ValueError, match="Unknown bridge setting"):
        bridge_candidate(config, {"reception_evidence": {"enabled": False, "capacity": 16}})
    assert config == before
    assert (
        bridge_candidate(config, {"queue_ttl_seconds": 25})["reception_evidence"]
        == before["channel_bridge"]["reception_evidence"]
    )


def test_equivalent_evidence_defaults_do_not_require_restart(config):
    engine = SimpleNamespace(channel_bridge=BridgeSettings(config))
    config["channel_bridge"]["reception_evidence"] = {
        "enabled": False,
        "window_seconds": 120,
        "capacity": 256,
        "observers": [],
    }
    assert not view(config, engine)["restart_required"]


def test_settings_options_does_not_start_inspection(monkeypatch):
    api = APIEndpoints.__new__(APIEndpoints)
    api._set_cors_headers = Mock()
    monkeypatch.setattr(cherrypy.request, "method", "OPTIONS")
    assert api.bridge_settings() == ""


def test_timed_out_control_has_explicit_unknown_outcome_and_is_not_cancelled(monkeypatch):
    api = APIEndpoints.__new__(APIEndpoints)
    api._set_cors_headers = Mock()
    api.event_loop = Mock()
    api.event_loop.is_running.return_value = True
    future = Mock()
    future.result.side_effect = TimeoutError()

    def submit(coroutine, loop):
        coroutine.close()
        return future

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    monkeypatch.setattr(cherrypy.request, "method", "POST")
    monkeypatch.setattr(cherrypy.request, "json", {"paused": True}, raising=False)
    result = api.update_bridge_controls()
    assert result["success"] is False and result["outcome"] == "unknown"
    assert "may still complete" in result["error"]
    future.result.assert_called_once_with(timeout=5)
    future.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_failed_real_persistence_never_publishes_pause(config, tmp_path, monkeypatch):
    api = api_fixture(config, tmp_path)
    before = (tmp_path / "config.yaml").read_bytes()
    monkeypatch.setattr(api.config_manager, "_persist_config", lambda candidate: False)
    assert await update(api, {"paused": True}) is False
    assert api.daemon_instance.repeater_handler.channel_bridge.paused is False
    assert (tmp_path / "config.yaml").read_bytes() == before
    assert not api._bridge_control_pending


@pytest.mark.asyncio
async def test_other_lock_holder_can_wait_for_loop_while_pause_waits_for_lock(config, tmp_path):
    """Exercise the worst-case cross-thread wait graph, not just RLock recursion."""
    api = api_fixture(config, tmp_path)
    loop = asyncio.get_running_loop()
    held, allow_probe = threading.Event(), threading.Event()

    async def loop_probe():
        await asyncio.sleep(0)
        return "responsive"

    def other_writer():
        with _CONFIG_WRITE_LOCK:
            held.set()
            assert allow_probe.wait(3)
            return asyncio.run_coroutine_threadsafe(loop_probe(), loop).result(timeout=1)

    holder = asyncio.create_task(asyncio.to_thread(other_writer))
    assert await asyncio.wait_for(asyncio.to_thread(held.wait, 1), 1.5)
    operation = asyncio.create_task(update(api, {"paused": True}))
    try:
        await asyncio.wait_for(asyncio.sleep(0.01), 0.5)
        assert not operation.done()
        allow_probe.set()
        assert await asyncio.wait_for(holder, 2) == "responsive"
        assert await asyncio.wait_for(operation, 2)
    finally:
        allow_probe.set()
        await asyncio.gather(holder, operation, return_exceptions=True)
