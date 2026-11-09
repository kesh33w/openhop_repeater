"""Durable echo operations remain ordered and cancellation-safe off the event loop."""

import asyncio
import sqlite3
import threading

import pytest

from repeater.channel_bridge import AsyncEchoLedger, EchoLedger, LedgerBusyError
from tests.test_channel_bridge import C1, C2, packet
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
    await asyncio.wait_for(entered.wait(), timeout=2)
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


def assert_echo_lookup_failure_recorded(rig, home="local"):
    assert not rig.air.frames
    assert rig.handler.rx_count == 1
    assert rig.handler.dropped_count == 1
    assert rig.handler._bridge_pending == 0
    rig.handler.storage.record_packet.assert_called_once()
    record = rig.records()[0]
    assert record["rx_radio_id"] == home
    assert record["transmitted"] is False
    assert record["drop_reason"] == "Channel bridge echo check unavailable"
    assert rig.handler.storage.record_packet.call_args.kwargs["skip_mqtt"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("home,channel", [("local", C1), ("link", C2)])
@pytest.mark.parametrize("mode", ["forward", "monitor", "no_tx"])
@pytest.mark.parametrize("error", [sqlite3.OperationalError, LedgerBusyError])
async def test_lookup_failure_preserves_rx_record_without_forwarding(  # noqa: F811
    rig_factory, mode, error, home, channel
):
    from unittest.mock import AsyncMock

    from tests.test_fabric_fanout import _rx

    rig = rig_factory()
    rig.handler.config["repeater"]["mode"] = mode
    rig.handler.bridge_echoes.contains = AsyncMock(side_effect=error("lookup failed"))
    rig.handler.bridge_echoes.reserve = AsyncMock()
    source = packet(channel)
    original = source.write_to()
    metadata = _rx(home)

    assert await rig.handler(source, metadata) is False

    assert_echo_lookup_failure_recorded(rig, home)
    assert metadata["_repeater_drop_reason"] == "Channel bridge echo check unavailable"
    assert source.write_to() == original
    rig.handler.bridge_echoes.reserve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("home", ["local", "link"])
@pytest.mark.parametrize("width", [1, 2, 3])
@pytest.mark.parametrize("error", [sqlite3.OperationalError, LedgerBusyError])
async def test_direct_lookup_failure_has_no_crossing_or_ingress_fallback(  # noqa: F811
    rig_factory, home, width, error
):
    from unittest.mock import AsyncMock

    from tests.test_direct_bridge import ROUTES, direct
    from tests.test_fabric_fanout import LOCAL_HASH, _rx

    rig = rig_factory(direct_routes=ROUTES, channels={}, mappings=[])
    rig.handler.local_hash_bytes = bytes([LOCAL_HASH, 0x12, 0x34])
    rig.handler.bridge_echoes.contains = AsyncMock(side_effect=error("lookup failed"))
    rig.handler.bridge_echoes.reserve = AsyncMock()
    source = direct(width)
    original = source.write_to()

    assert await rig.handler(source, _rx(home)) is False

    assert_echo_lookup_failure_recorded(rig, home)
    assert rig.handler.recv_direct_count == 1
    assert source.write_to() == original
    rig.handler.bridge_echoes.reserve.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("home,channel,other", [("local", C1, "link"), ("link", C2, "local")])
@pytest.mark.parametrize("mode", ["forward", "monitor", "no_tx"])
@pytest.mark.parametrize("error", [sqlite3.OperationalError, LedgerBusyError])
async def test_local_origin_does_not_use_rx_echo_lookup(  # noqa: F811
    rig_factory, home, channel, other, mode, error
):
    from unittest.mock import AsyncMock

    rig = rig_factory()
    rig.handler.config["repeater"]["mode"] = mode
    rig.handler.bridge_echoes.contains = AsyncMock(side_effect=error("lookup failed"))
    expected = [] if mode == "no_tx" else [home, other]

    assert await rig.handler(
        packet(channel), {"home_radio_id": home}, local_transmission=True
    ) is bool(expected)

    rig.handler.bridge_echoes.contains.assert_not_awaited()
    assert rig.air.order == expected
    assert rig.handler.rx_count == 0
    rig.handler.storage.record_packet.assert_called_once()
    assert rig.records()[0]["transmitted"] is bool(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["forward", "monitor", "no_tx"])
async def test_sqlite_read_denial_preserves_rx_record(rig_factory, mode):  # noqa: F811
    from tests.test_fabric_fanout import _rx

    rig = rig_factory()
    rig.handler.config["repeater"]["mode"] = mode
    ledger = rig.handler.bridge_echoes
    await ledger._test_operation(
        lambda db: db.db.set_authorizer(
            lambda action, *args: (
                sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ else sqlite3.SQLITE_OK
            )
        )
    )
    try:
        assert await rig.handler(packet(), _rx("local")) is False
    finally:
        await ledger._test_operation(lambda db: db.db.set_authorizer(None))

    assert_echo_lookup_failure_recorded(rig)


@pytest.mark.asyncio
async def test_lookup_failure_retains_known_echo_and_recovers(rig_factory):  # noqa: F811
    from unittest.mock import AsyncMock

    from tests.test_fabric_fanout import _rx

    rig = rig_factory()
    ledger = rig.handler.bridge_echoes
    echo = packet(message="known echo")
    token = await ledger.reserve(echo, echo)
    await ledger.finish(token, success=True)
    original_contains = ledger.contains
    ledger.contains = AsyncMock(side_effect=sqlite3.OperationalError("lookup failed"))
    assert await rig.handler(echo, _rx("local")) is False
    assert_echo_lookup_failure_recorded(rig)

    ledger.contains = original_contains
    assert await ledger.contains(echo)
    assert await rig.handler(echo, _rx("link")) is False
    assert rig.records()[-1]["drop_reason"] == "Channel bridge echo"
    assert not rig.air.frames

    assert await rig.handler(packet(message="new traffic"), _rx("local")) is True
    assert rig.air.order == ["local", "link"]
    assert len(rig.records()) == 3


@pytest.mark.asyncio
async def test_lookup_cancellation_propagates(rig_factory):  # noqa: F811
    from unittest.mock import AsyncMock

    from tests.test_fabric_fanout import _rx

    rig = rig_factory()
    rig.handler.bridge_echoes.contains = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await rig.handler(packet(), _rx("local"))
    assert not rig.air.frames
    rig.handler.storage.record_packet.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_drains_router_before_ordered_worker_close():
    from types import SimpleNamespace

    from repeater.main import RepeaterDaemon

    daemon = RepeaterDaemon.__new__(RepeaterDaemon)
    daemon._shutdown_started = False
    daemon.config = {}
    for name in (
        "dispatcher",
        "private_island",
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
        source, 0, [PlannedSend(Destination("radio", "link"), source, True)],
        source_home="local",
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    assert rig.handler._bridge_pending == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rig.handler._bridge_pending == 0
    assert rig.air.order == []
