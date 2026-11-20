"""Real scheduler/dispatcher/fabric; simulated radios only, never RF."""

import pytest
from openhop_core.rf_fabric.topology import NetworkTopology
from repeater.engine import FanoutTxResult, RadioTxResult
from tests.test_fabric_fanout import _Rig, _FakeRadio, _flood


def rig(order, scope=None, gap=None):
    r = _Rig()
    rak = _FakeRadio("rak", r.air)
    r.radio.fabric.register_radio(rak, radio_id="rak")
    for radio in (r.local, r.link, rak):
        radio._cad_mode_explicit = False
        radio.lbt_enabled = False
    entries = [
        {"id": "local", "network": "SMF", "role": "transceiver"},
        {"id": "link", "network": "SFO", "role": "transceiver"},
        {"id": "rak", "network": "SFO", "role": "receiver"},
    ]
    t = NetworkTopology(
        entries,
        {
            "networks": {"SMF": "local", "SFO": "link"},
            "tx_groups": {"link": list(order)},
            "enable_receiver_forwarding": True,
            **({"tx_group_scopes": {"link": scope}} if scope else {}),
            **({"tx_group_gaps_ms": {"link": gap}} if gap else {}),
        },
        "local",
    )
    r.radio.fabric.configure_topology(t)
    r.handler.network_topology = t
    return r, rak


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("link", "rak"), ("rak", "link")])
async def test_full_stack_order_and_dedup(order):
    r, rak = rig(order)
    pkt = _flood()
    task = await r.handler.schedule_retransmit(pkt, 0, 0, preferred_tx_radio_id="link")
    assert await task
    assert r.air.order == list(order)
    assert r.air.frames[0][1] == r.air.frames[1][1]
    assert not r.air.overlapped
    assert len(pkt._tx_metadata["ordered_tx_results"]) == 2
    # Every leg is attributed to its own radio (airtime chart, per-radio rates).
    tx_result = FanoutTxResult([RadioTxResult("link", True, metadata=pkt._tx_metadata)])
    assert tx_result.successful_radio_ids == list(order)
    # A separate logical invocation cannot create two more physical sends.
    task = await r.handler.schedule_retransmit(_flood(), 0, 0, preferred_tx_radio_id="link")
    assert not await task
    assert r.air.order == list(order)


@pytest.mark.asyncio
async def test_unknown_first_result_stops_second():
    r, rak = rig(("rak", "link"))
    rak.fail = True
    task = await r.handler.schedule_retransmit(_flood(), 0, 0, preferred_tx_radio_id="link")
    assert not await task
    assert not r.air.frames
    assert r.sent_radio_ids == ["rak"]


@pytest.mark.asyncio
async def test_existing_cad_does_not_prevent_or_change_sequential_sends():
    r, rak = rig(("link", "rak"))
    rak._cad_mode_explicit = True
    task = await r.handler.schedule_retransmit(_flood(), 0, 0, preferred_tx_radio_id="link")
    assert await task
    assert r.air.order == ["link", "rak"]
    assert rak._cad_mode_explicit is True
    assert not r.air.overlapped


@pytest.mark.asyncio
async def test_partial_success_keeps_each_leg_and_uncertainty():
    r, rak = rig(("link", "rak"))
    rak.fail = True
    pkt = _flood()
    task = await r.handler.schedule_retransmit(pkt, 0, 0, preferred_tx_radio_id="link")
    assert await task
    assert pkt._tx_metadata["tx_outcome_uncertain"]
    assert [x["success"] for x in pkt._tx_metadata["ordered_tx_results"]] == [True, False]


@pytest.mark.asyncio
async def test_cancellation_keeps_partial_results():
    import asyncio

    r, rak = rig(("link", "rak"))

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()

    rak.send = cancel
    pkt = _flood()
    task = await r.handler.schedule_retransmit(pkt, 0, 0, preferred_tx_radio_id="link")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [x["success"] for x in pkt._tx_metadata["ordered_tx_results"]] == [True, False]
    assert pkt._tx_metadata["tx_outcome_uncertain"]


@pytest.mark.asyncio
async def test_second_leg_respects_remaining_shared_channel_budget():
    r, rak = rig(("link", "rak"))
    # Rig has a single shared ledger; each leg costs 10 ms, limit is 15 ms.
    ledger = r.handler.airtime_budgets.for_radio("link")
    ledger._ledger.max_airtime_per_minute = 15
    r.handler._egress_radio_airtime = lambda packet, radio_id: 10
    pkt = _flood()
    task = await r.handler.schedule_retransmit(pkt, 0, 10, preferred_tx_radio_id="link")
    assert await task
    assert r.air.order == ["link"]
    assert pkt._tx_metadata["ordered_tx_results"][1]["metadata"]["reason"] == "duty_cycle_exceeded"


@pytest.mark.asyncio
async def test_crossings_scope_double_transmits_only_bridged_copies():
    r, rak = rig(("rak", "link"), scope="crossings")
    # An ordinary repeat on the SFO network goes out once, on the primary.
    plain = _flood()
    assert await (await r.handler.schedule_retransmit(plain, 0, 0, preferred_tx_radio_id="link"))
    assert r.air.order == ["link"]
    # A bridged copy uses the whole group, in its configured order.
    bridged = _flood(payload=b"bridged copy")
    task = await r.handler.schedule_retransmit(
        bridged, 0, 0, preferred_tx_radio_id="link", crossing=True
    )
    assert await task
    assert r.air.order == ["link", "rak", "link"]
    assert len(bridged._tx_metadata["ordered_tx_results"]) == 2


@pytest.mark.asyncio
async def test_gap_pauses_between_legs_without_holding_the_tx_lock(monkeypatch):
    import asyncio

    import repeater.engine as engine

    r, rak = rig(("link", "rak"), gap=[300, 1200])
    monkeypatch.setattr(engine.random, "uniform", lambda lo, hi: (lo + hi) / 2)
    pauses = []
    real_sleep = asyncio.sleep

    async def sleep(seconds, *args, **kwargs):
        if seconds:
            pauses.append((seconds, r.air.order[:], r.handler._tx_lock.locked()))
        await real_sleep(0)

    monkeypatch.setattr(engine.asyncio, "sleep", sleep)
    pkt = _flood()
    task = await r.handler.schedule_retransmit(pkt, 0, 0, preferred_tx_radio_id="link")
    assert await task
    assert r.air.order == ["link", "rak"]
    # One pause of 750 ms, after the first leg went out, with the TX lock free.
    assert pauses == [(0.75, ["link"], False)]


@pytest.mark.asyncio
async def test_no_gap_after_a_failed_first_leg_that_stops_the_group(monkeypatch):
    import repeater.engine as engine

    r, rak = rig(("rak", "link"), gap=[300, 1200])
    rak.fail = True
    calls = []
    monkeypatch.setattr(engine.random, "uniform", lambda lo, hi: calls.append(1) or 0)
    task = await r.handler.schedule_retransmit(_flood(), 0, 0, preferred_tx_radio_id="link")
    assert not await task
    assert calls == []
