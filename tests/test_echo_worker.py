"""Durable echo operations remain ordered and cancellation-safe off the event loop."""

import asyncio
import sqlite3
import threading

import pytest

from repeater.channel_bridge import AsyncEchoLedger, EchoLedger, LedgerBusyError
from tests.test_channel_bridge import packet
from tests.test_channel_bridge_engine import rig_factory  # noqa: F401


@pytest.mark.asyncio
async def test_worker_preserves_full_pragmas_and_restart_state(tmp_path):
    path = tmp_path / "echo.sqlite"
    worker = AsyncEchoLedger(path)
    try:
        pragmas = await worker._test_operation(
            lambda ledger: (
                ledger.db.execute("PRAGMA journal_mode").fetchone()[0],
                ledger.db.execute("PRAGMA synchronous").fetchone()[0],
                threading.get_ident(),
            )
        )
        assert pragmas[:2] == ("delete", 2)
        assert pragmas[2] != threading.get_ident()
        token = await worker.reserve(packet(), packet(message="translated"))
        assert token and await worker.contains(packet())
        await worker.finish(token, success=True)
    finally:
        await worker.aclose()
    reopened = EchoLedger(path)
    try:
        assert reopened.contains(packet())
        assert reopened.db.execute("SELECT DISTINCT state FROM echoes").fetchall() == [("sent",)]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_cancelled_reservation_completes_before_close_and_survives_restart(tmp_path):
    path = tmp_path / "echo.sqlite"
    worker = AsyncEchoLedger(path)
    entered, release = threading.Event(), threading.Event()

    def pause(ledger):
        entered.set()
        assert release.wait(2)

    blocker = asyncio.create_task(worker._test_operation(pause))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    reservation = asyncio.create_task(worker.reserve(packet(), packet()))
    await asyncio.sleep(0)
    reservation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reservation
    closing = asyncio.create_task(worker.aclose())
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(RuntimeError, match="closing"):
        await worker.contains(packet())
    release.set()
    await blocker
    await closing
    reopened = EchoLedger(path)
    try:
        assert reopened.contains(packet())
        assert reopened.db.execute("SELECT DISTINCT state FROM echoes").fetchall() == [("pending",)]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_worker_queue_saturates_without_blocking_event_loop(tmp_path):
    worker = AsyncEchoLedger(tmp_path / "echo.sqlite", max_pending=1)
    entered, release = threading.Event(), threading.Event()

    def pause(ledger):
        entered.set()
        assert release.wait(2)

    task = asyncio.create_task(worker._test_operation(pause))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        with pytest.raises(LedgerBusyError, match="queue full"):
            await worker.reserve(packet(), packet())
        # Reaching this assertion while the worker is blocked is the liveness check.
        assert not release.is_set()
    finally:
        release.set()
        await task
        await worker.aclose()


@pytest.mark.asyncio
async def test_failed_commit_is_reported_without_claiming_sent(tmp_path):
    worker = AsyncEchoLedger(tmp_path / "echo.sqlite")
    try:
        token = await worker.reserve(packet(), packet())
        await worker._test_operation(
            lambda ledger: ledger.db.set_authorizer(
                lambda action, *args: (
                    sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_UPDATE else sqlite3.SQLITE_OK
                )
            )
        )
        with pytest.raises(sqlite3.DatabaseError):
            await worker.finish(token, success=True)
        await worker._test_operation(lambda ledger: ledger.db.set_authorizer(None))
        assert await worker.contains(packet())
        assert await worker._test_operation(
            lambda ledger: ledger.db.execute("SELECT DISTINCT state FROM echoes").fetchall()
        ) == [("pending",)]
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_ordered_known_failure_releases_and_corrupt_startup_fails(tmp_path):
    worker = AsyncEchoLedger(tmp_path / "echo.sqlite")
    token = await worker.reserve(packet(), packet())
    await worker.finish(token, success=False)
    assert not await worker.contains(packet())
    await worker.aclose()
    broken = tmp_path / "broken.sqlite"
    broken.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        AsyncEchoLedger(broken)


@pytest.mark.asyncio
async def test_runtime_claims_crossing_slot_before_awaiting_ledger(rig_factory):  # noqa: F811
    from tests.test_fabric_fanout import _rx

    rig = rig_factory(max_pending_crossings=1)
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.handler.bridge_echoes.reserve

    async def pause(source, outgoing):
        entered.set()
        await release.wait()
        return await original(source, outgoing)

    rig.handler.bridge_echoes.reserve = pause
    first = asyncio.create_task(rig.handler(packet(message="one"), _rx("local")))
    await entered.wait()
    assert rig.handler._bridge_pending == 1
    await rig.handler(packet(message="two"), _rx("local"))
    assert rig.air.order.count("link") == 0
    assert rig.handler._bridge_pending == 1
    release.set()
    assert await first
    assert rig.air.order.count("link") == 1
    assert rig.handler._bridge_pending == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [sqlite3.OperationalError, LedgerBusyError])
async def test_runtime_preserves_confirmed_tx_when_completion_commit_fails(rig_factory, error):  # noqa: F811
    from tests.test_fabric_fanout import _rx

    rig = rig_factory()

    async def fail(*args, **kwargs):
        raise error("fixture completion failure")

    rig.handler.bridge_echoes.finish = fail
    assert await rig.handler(packet(), _rx("local"))
    assert rig.air.order == ["local", "link"]
    assert await rig.handler.bridge_echoes.contains(packet())
    row = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"][1]
    assert row["success"] is True
    assert rig.handler._bridge_pending == 0


@pytest.mark.asyncio
async def test_daemon_drains_router_before_ordered_worker_close():
    from types import SimpleNamespace

    from repeater.main import RepeaterDaemon

    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    daemon._shutdown_started = False
    daemon.config = {}
    for name in (
        "dispatcher",
        "http_server",
        "neighbors_publisher",
        "glass_handler",
        "sensor_manager",
        "gps_service",
        "radio",
    ):
        setattr(daemon, name, None)
    events = []

    async def router_stop():
        await asyncio.sleep(0)
        events.append("router_drained")

    async def ledger_close():
        assert events == ["router_drained"]
        events.append("ledger_closed")

    async def step(name, awaitable, timeout=None):
        if name == "router":
            assert timeout == 10.0  # Exceeds router's five-second inner drain.
        await awaitable

    daemon.router = SimpleNamespace(stop=router_stop)
    daemon.repeater_handler = SimpleNamespace(aclose_channel_bridge=ledger_close, storage=None)
    daemon._shutdown_step = step
    daemon._report_lingering_threads = lambda: None
    daemon._arm_exit_watchdog = lambda: None
    await daemon._shutdown()
    assert events == ["router_drained", "ledger_closed"]


@pytest.mark.asyncio
async def test_runtime_releases_slot_if_reservation_is_cancelled(rig_factory):  # noqa: F811
    from repeater.channel_bridge import Destination, PlannedSend

    rig = rig_factory(max_pending_crossings=1)
    entered = asyncio.Event()

    async def pause(*args):
        entered.set()
        await asyncio.Event().wait()

    rig.handler.bridge_echoes.reserve = pause
    source = packet()
    task = await rig.handler._schedule_bridge_plan(
        source, 0, [PlannedSend(Destination("radio", "link"), source, True)]
    )
    await entered.wait()
    assert rig.handler._bridge_pending == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.handler._bridge_pending == 0
    assert rig.air.order == []
