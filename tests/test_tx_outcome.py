"""Suppressed destination claims are neither confirmed sends nor safe retries."""

import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest
from openhop_core.rf_fabric.fabric import RFFabric
from openhop_core.node.dispatcher import Dispatcher
from tests.test_logical_receiver import topology
from tests.test_channel_bridge import packet
from tests.test_fabric_fanout import _rx
from tests.test_tx_lock import _make_handler
from repeater.channel_bridge import AsyncEchoLedger, EchoLedger


class InlineLedger(AsyncEchoLedger):
    """Real SQLite reserve/finish semantics, without inherited worker-loop fixture hang."""

    def __init__(self, path, **kwargs):
        self._ledger = EchoLedger(path, **kwargs)

    async def _run(self, operation):
        return operation()

    def close(self):
        self._ledger.close()


SUPPRESSED = dict(
    ok=False,
    suppressed=True,
    retryable=False,
    preserve_tx_claim=True,
    reason="destination duplicate",
    tx_outcome_uncertain=False,
    airtime_ms=0.0,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["pending", "confirmed", "uncertain"])
async def test_fabric_duplicate_retains_claim_without_confirming(prior):
    radios = {rid: AsyncMock() for rid in topology().members}
    for radio in radios.values():
        radio.is_connected = True
        radio.send.return_value = {"ok": True}
    fabric = RFFabric()
    fabric._radios.update(radios)
    fabric._default_radio_id = "dir909"
    fabric.configure_topology(topology())
    started, release = asyncio.Event(), asyncio.Event()

    async def sending(data):
        started.set()
        if prior == "pending":
            await release.wait()
        return None if prior == "uncertain" else {"ok": True}

    radios["dir909"].send.side_effect = sending
    first = asyncio.create_task(fabric.send(packet().write_to(), radio_id="dir909"))
    await started.wait()
    if prior != "pending":
        await first
    duplicate = await fabric.send(packet().write_to(), radio_id="dir909")
    assert duplicate == {**SUPPRESSED, "radio_id": "dir909"}
    assert radios["dir909"].send.await_count == 1
    assert len(fabric._network_tx_seen) == 1
    release.set()
    await first


@pytest.mark.asyncio
async def test_dispatcher_suppression_never_calls_sent_callback_or_debits():
    radio = MagicMock()
    radio.send = AsyncMock()
    radio.send.return_value = SUPPRESSED
    dispatcher = Dispatcher(radio)
    dispatcher.packet_sent_callback = AsyncMock()
    dispatcher._client_repeat_enabled = True
    dispatcher._debit_tx_budget = MagicMock()
    dispatcher._await_tx_budget = AsyncMock()
    dispatcher._tx_budget_wait_s = lambda: 0
    p = packet()
    assert await dispatcher.send_packet(p, wait_for_ack=False) is False
    assert p._tx_metadata["suppressed"]
    dispatcher.packet_sent_callback.assert_not_called()
    dispatcher._debit_tx_budget.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [False, True])
async def test_scheduler_never_retries_or_charges_suppression(sent):
    handler, p = _make_handler(), packet()

    async def suppress(*args, **kwargs):
        p._tx_metadata = dict(SUPPRESSED)
        return sent

    handler.dispatcher.send_packet = AsyncMock(side_effect=suppress)
    task = await handler.schedule_retransmit(p, 0, 100, retry_unsent=True)
    assert await task is False
    handler.dispatcher.send_packet.assert_awaited_once()
    handler._record_packet_sent.assert_not_called()
    handler.airtime_mgr.record_tx.assert_not_called()


@pytest.mark.asyncio
async def test_suppression_persists_reason_and_keeps_durable_reservation(rig_factory, monkeypatch):
    monkeypatch.setattr("repeater.engine.AsyncEchoLedger", InlineLedger)
    rig = rig_factory()
    rig.dispatcher._client_repeat_enabled = False
    rig.link.send = AsyncMock(return_value=dict(SUPPRESSED))
    await asyncio.wait_for(rig.handler(packet(), _rx("local")), 5)
    rig.link.send.assert_awaited_once()
    row = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"][1]
    assert row["success"] is False and row["failure_reason"] == "destination duplicate"
    assert await rig.handler.bridge_echoes.contains(packet())


@pytest.mark.asyncio
async def test_busy_reason_survives_existing_bounded_retry(rig_factory, monkeypatch):
    monkeypatch.setattr("repeater.engine.AsyncEchoLedger", InlineLedger)
    rig = rig_factory()
    rig.dispatcher._client_repeat_enabled = False
    rig.link.send = AsyncMock(
        return_value=dict(ok=False, reason="channel_busy", tx_outcome_uncertain=False)
    )
    await asyncio.wait_for(rig.handler(packet(), _rx("local")), 5)
    assert rig.link.send.await_count == 2
    row = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"][1]
    assert row["failure_reason"] == "channel_busy" and row["success"] is False
    assert not await rig.handler.bridge_echoes.contains(packet())


def test_sql_lbt_excludes_suppression_but_retains_busy_and_unknown_failures():
    import sqlite3
    from repeater.data_acquisition.sqlite_handler import (
        LBT_EGRESS_BUCKETS_QUERY,
        LBT_EGRESS_DISTRIBUTION_QUERY,
    )

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE packet_egress(timestamp,radio_id,success,lbt_attempts,lbt_channel_busy,failure_reason)"
    )
    db.execute("CREATE INDEX idx_packet_egress_time_radio ON packet_egress(timestamp,radio_id)")
    db.executemany(
        "INSERT INTO packet_egress VALUES(?,?,?,?,?,?)",
        [
            (100, "local", 1, 0, 0, None),
            (101, "local", 0, 2, 1, "channel_busy"),
            (102, "local", 0, 0, 0, "destination duplicate"),
            (103, "local", 0, 0, 0, "Native TX rejected, failed, or expired"),
        ],
    )
    row = db.execute(LBT_EGRESS_BUCKETS_QUERY, (60, 60, 0, 200, 4)).fetchone()
    assert row["transmissions"] == 3 and row["failed_transmissions"] == 2
    assert row["total_attempts"] == 5 and row["busy_channel_events"] == 1
    dist = db.execute(LBT_EGRESS_DISTRIBUTION_QUERY, (60, 60, 0, 200)).fetchall()
    assert sum(r["cnt"] for r in dist) == 3
    assert db.execute("SELECT count(*) FROM packet_egress").fetchone()[0] == 4
    db.close()


@pytest.mark.asyncio
async def test_receiver_destination_claim_survives_suppression(tmp_path, monkeypatch):
    from tests.test_network_topology import Rig, config

    monkeypatch.setattr("repeater.engine.AsyncEchoLedger", InlineLedger)
    rig = Rig(config(tmp_path, receiver=True))
    rig.dispatcher._client_repeat_enabled = False
    rig.ears["link"].send = AsyncMock(return_value=dict(SUPPRESSED))
    try:
        await asyncio.wait_for(rig.handler(packet(), _rx("omni909")), 5)
        assert any(network == "910" for network, digest in rig.handler._forward_destinations)
        rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
        assert rows[0]["failure_reason"] == "destination duplicate"
        assert (
            rig.handler.storage.record_packet.call_args.args[0]["drop_reason"]
            == "TX suppressed: destination duplicate"
        )
        await asyncio.wait_for(rig.handler(packet(), _rx("local")), 5)
        rig.ears["link"].send.assert_awaited_once()
    finally:
        rig.close()


@pytest.mark.asyncio
async def test_generic_fanout_persists_suppression_reason():
    from tests.test_fabric_fanout import _Rig, _flood

    rig = _Rig({"repeat_on_ingress": True})
    rig.dispatcher._client_repeat_enabled = False
    rig.link.send = AsyncMock(return_value=dict(SUPPRESSED))
    await asyncio.wait_for(rig.handler(_flood(rx="local"), _rx("local")), 5)
    rows = rig.handler.storage.record_packet.call_args.kwargs["tx_egress"]
    row = next(row for row in rows if row["radio_id"] == "link")
    assert row["failure_reason"] == "destination duplicate"
    assert row["success"] is False
    rig.link.send.assert_awaited_once()


def test_suppressed_evidence_cannot_claim_over_air_reception():
    from repeater.bridge_evidence import ReceptionEvidence

    evidence = ReceptionEvidence(monotonic=lambda: 1)
    evidence._pending["test"] = {
        "deadline": 5,
        "view": {
            "tx_state": "pending",
            "reception_state": "waiting",
            "observers": ["earlier copy"],
        },
    }
    evidence.complete("test", "tx_suppressed")
    view = evidence._pending["test"]["view"]
    assert view["tx_state"] == "tx_suppressed"
    assert view["reception_state"] == "not_applicable" and view["observers"] == []
    assert evidence._counts["observed"] == 0


@pytest.mark.asyncio
async def test_expired_attempt_gets_reason_without_dispatch():
    import time

    handler, p = _make_handler(), packet()
    p._tx_metadata = {"reason": "stale previous result"}
    task = await handler.schedule_retransmit(p, 0, deadline=time.monotonic() - 1)
    assert await task is False
    assert p._tx_metadata["reason"] == "deadline_expired"
    handler.dispatcher.send_packet.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("suppressed", [True, False])
async def test_injection_suppression_logs_info_without_success_or_loopback(caplog, suppressed):
    import logging
    from types import SimpleNamespace
    from tests.test_fabric_fanout import _router_for

    async def reject(packet, metadata, **kwargs):
        metadata["_repeater_drop_reason"] = (
            "TX suppressed: destination duplicate" if suppressed else "TX failed"
        )
        return False

    handler = AsyncMock(side_effect=reject)
    handler.network_topology = None
    handler.config = {}
    rig = SimpleNamespace(handler=handler, dispatcher=MagicMock())
    router = _router_for(rig)
    router.enqueue = AsyncMock()
    with caplog.at_level(logging.INFO):
        assert await router.inject_packet(packet()) is False
    router.enqueue.assert_not_called()
    messages = [record for record in caplog.records if "Injected packet" in record.message]
    assert len(messages) == 1
    assert messages[0].levelno == (logging.INFO if suppressed else logging.WARNING)
    if suppressed:
        assert "no new TX" in messages[0].message
